# Adhole：DNS 层过滤表的工程实践

把 58 万条域名塞进 `local-zone`，难点不在"怎么写一行配置"，而在两件事：**上游清单格式互不相通，转换错的代价是静默漏放**；以及 **Unbound 在哪一步判定这些名字**。前者决定表是否可信，后者决定表是否真的生效。本章把一套 Adhole 式过滤表从原始清单走到两台解析器上的全过程拆开，重点放在格式转换与 `local-zone` 的优先级。

实验环境仍是一台家庭网关 + 一台境外云主机，以下为教学用示例地址：IPv4 取 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`（[RFC 5737]），IPv6 取 `2001:db8::/32`，域名统一用 `example.net` / `example.org`，内网写通用 `192.168.68.0/24` 与 ULA `fd00::/64`，主机名 `gw-01` / `edge-01`，凭据一律 `<TOKEN>`。本章只做过滤表：第 9 章最小暴露面、第 7 章可观测性、第 12 章 DoH 端点分工都是既成前提，不在这里重贴它们的配置。

## 能力边界：DNS 层只能表达"名字"

浏览器扩展里的 Adblock Plus / uBlock 规则看见的是完整 URL，Unbound 看见的只有 QNAME。这条差异不是"少几个字段"，而是**一整类规则在 DNS 层没有对应物**。写转换器之前必须把这类规则判为跳过，而不是"尽量近似"——近似的后果是把不该挡的域名挡了，或把该挡的域名放行。

| 规则形态 | 例子（教学用虚构域名） | 能否表达 | 处理 |
|---|---|---|---|
| 整域名 | `ads.example.net` | 能 | 转成 `local-zone` |
| 通配子域 | `*.tracker.example.org` | 部分能 | 剥 `*.` 后按父域建区，覆盖其下所有名字 |
| URL 路径 | `example.net/path/ad.js` | **不能** | 跳过；QNAME 里没有路径 |
| 限定来源页 | `\|\|ads.example.net^$domain=news.example.org` | **不能** | 跳过；`$domain=` 依赖被加载页面的域 |
| 元素隐藏 | `##.ad-banner` | **不能** | 跳过；这是 DOM/CSS 层 |
| 脚本注入 | `##+js(...)` | **不能** | 跳过；这是页面脚本层 |
| 白名单例外 | `@@\|\|allow.example.net^` | **不能** | 跳过整条，别把 `@@` 误当允许项 |
| IP / 端口 | `0.0.0.0 ads.example.net` 里的地址 | 地址无意义 | 只取右侧域名 |

`@@` 尤其容易出错：它在浏览器规则里表示"这条不挡"，而 DNS 层没有"按请求上下文放行"的概念。把 `@@` 后的域名收进表，等于把白名单写反了。本项目单独维护 `unblock_domains.txt`，**在最终去重前应用**：先精确删除，再合并去重，这样白名单既不会被 cn/noncn 两份表重新拼回来，也不会因父域通配而漏掉。一句话记住边界：过滤表决定"这个名字要不要给答案"，不决定"这次请求从哪个页面来、URL 里有什么、页面上哪块元素要隐藏"。

## 六类上游格式逐条拆

所有格式最终都归一到同一形态（选 `always_null` 而非 `always_nxdomain`，是让客户端拿到 `0.0.0.0` / `::` 的空答而不是 NXDOMAIN；NXDOMAIN 常引发客户端重试、换搜索域甚至换解析器，语义见 24.6.2）：

```conf
local-zone: "ads.example.net." always_null
```

**为什么这么设**：`include` 展开后等价于文本原样插入，表文件里只能出现 `server:` 段内合法的语句；统一 FQDN 尾点与统一类型，才能用 `unbound-control list_local_zones` 直接核对命中了哪条。**怎么验证**：`unbound-checkconf /etc/unbound/unbound.conf && unbound-control list_local_zones | grep 'ads.example.net'`。**失败模式 / 回滚点**：混用 `always_nxdomain` / `always_refuse` 会让一张表出现三种应答语义，排障时无法用一条 `dig` 判断是谁挡的；回滚到上一版表并 reload 即可。

### HOSTS 格式

```text
0.0.0.0 ads.example.net
127.0.0.1 tracker.example.org
0.0.0.0 ads.example.net # 广告位
```

可提取信息：每行右侧第一个域名 token（一行多个域名则逐个取出）。丢弃项：左侧 IP（`0.0.0.0` 与 `127.0.0.1` 都只是 HOSTS 的本地重定向占位）、`#` 之后的注释。`127.0.0.1 domain` **不能**直译成指向回环的 `local-data`：那会让客户端连自己的 `127.0.0.1`，把"连接被拒绝"伪装成"服务存在"。输出：`local-zone: "ads.example.net." always_null`、`local-zone: "tracker.example.org." always_null`。

### 纯域名 TXT

```text
ads.example.net
tracker.example.org
# 注释行
```

可提取信息：整行本身。丢弃项：`#` / `!` 开头的注释、空行、行尾空白、不含点的裸标签（如 `localhost`）。这是最干净的一类，也是转换器应把其他所有格式**归一化到的目标形态**：先变成"一行一个域名"，再走同一段校验与输出。

### Adblock Plus / uBlock 格式

```text
||ads.example.net^
||tracker.example.org^$domain=news.example.org
||cdn.example.net/path/ad.js^
##.ad-banner
##+js(set-constant)
@@||allow.example.net^
```

| 原始行 | 判定 | 输出 |
|---|---|---|
| `\|\|ads.example.net^` | 纯域名规则，收 | `local-zone: "ads.example.net." always_null` |
| `\|\|tracker.example.org^$domain=news.example.org` | 含 `$domain=`，**跳过整条** | 无 |
| `\|\|cdn.example.net/path/ad.js^` | 含路径，**跳过整条** | 无 |
| `##.ad-banner` / `##+js(...)` | 元素隐藏 / 脚本注入，跳过 | 无 |
| `@@\|\|allow.example.net^` | 白名单例外，跳过 | 无 |

"跳过整条"是关键：不要把 `cdn.example.net` 从带路径的规则里抠出来收进表，那会把整个 CDN 域挡掉；`$domain=` 的规则若只取左侧域名，就变成"对所有来源页都挡"，比原规则严格得多。

### Unbound 原生格式

```text
local-zone: "ads.example.net." always_null
local-zone: "tracker.example.org." always_nxdomain
```

可提取信息：引号里的区名。丢弃项：原类型（统一改写为 `always_null`）、`local-zone:` 关键字、多余空格。上游已给 `always_null` 也要**重新过一遍校验**：这类清单常混着别类型，混用会破坏单一应答语义。改写同时强制补齐 FQDN 尾点，避免 `example.net` 与 `example.net.` 在树里被当成两个东西。

### 威胁情报 TXT

```text
# 恶意 C2 Indicator，教学示例
https://c2.example.org/beacon
http://198.51.100.7/payload
c2.example.org
```

可提取信息：URL 里的主机名。丢弃项：`http://` / `https://` 前缀、尾 `/` 及其后的路径、端口、查询串、纯 IP 行、注释。输出：`local-zone: "c2.example.org." always_null`。纯 IP 的 IOC 属于防火墙 / 代理层，不属于 DNS 过滤表：拼一条 `local-zone: "198.51.100.7." always_null` 是个永远查不到的名字，纯浪费表项。去协议后要先去尾 `/` 再取主机名，顺序反了会把斜杠留在域名里。

### OISD 的 unbound 输出

OISD 等项目直接提供 unbound 格式清单，形态与 24.2.4 相同。可提取信息：引号内区名；丢弃项：文件头 `#` 注释与元数据行、非 `local-zone` 的杂项行。走同一个解析器即可，但**单独记一个来源标签**——`domain-state.tsv` 的 `category` 要能区分"这条来自哪个上游"，回滚时才能只撤一份清单。具体上游 URL 与清单内容会变动，教学脚本不依赖任何真实清单地址；接入时把下载好的本地文件放进 `sources/` 即可。

### 归一化六步

所有格式最后汇到这六步，顺序不能随意交换：① **去 `||` 与 `^`**（只处理 ABP 边界符号）；② **剥离 `*.`**（`*.tracker.example.org` → `tracker.example.org`，这是**降级为父域**，会覆盖其下所有子域；若上游本意只匹配一级子域，DNS 层无法精确表达，只能接受更宽的拦截）；③ **去 `http(s)://`**；④ **去尾 `/`**（先截断路径，只剩主机名）；⑤ **去 `www.`**（减少重复表项，但这是可逆性最弱的一步——`www.` 与裸域若由不同运营方托管，合并会让裸域也被挡，必须能从白名单回滚）；⑥ **小写化、去尾点、只保留含点且长度 > 2 的整域名**（每个标签只含字母数字与连字符、不以连字符开头/结尾，通不过的行计入跳过计数而不是静默丢）。`"含点"` 排除 `localhost` 一类裸标签，`"长度 > 2"` 排除 `a.b` 这类短串；两者只是粗筛，转换器不判断公共后缀，因为那需要额外的后缀列表，不在本书已核实范围内。

### 源格式 → 可提取信息 → 丢弃项 → 输出行 对照表

| 源格式 | 可提取信息 | 丢弃项 | 输出行 |
|---|---|---|---|
| HOSTS `0.0.0.0 domain` | 右侧整域名 | 左侧 IP、`#` 注释 | `local-zone: "ads.example.net." always_null` |
| HOSTS `127.0.0.1 domain` | 右侧整域名 | 左侧回环地址（不转 `local-data`） | `local-zone: "tracker.example.org." always_null` |
| 纯域名 TXT | 整行域名 | 注释、空行、裸标签 | `local-zone: "ads.example.net." always_null` |
| ABP `\|\|domain.tld^` | `\|\|` 与 `^` 之间的主机名 | `\|\|`、`^` | `local-zone: "ads.example.net." always_null` |
| ABP 带 `$domain=` | 无（整条不可用） | `$domain=` 及左侧域名一并跳过 | 无输出 |
| ABP 带路径 / `##` / `##+js` / `@@` | 无（整条不可用） | 整行 | 无输出 |
| Unbound `local-zone: "..." always_null` | 引号内区名 | 原类型、关键字、多余空格 | 统一改写为 `always_null` |
| 威胁情报 URL | URL 中的主机名 | 协议、路径、端口、IP 行 | `local-zone: "c2.example.org." always_null` |
| OISD unbound 输出 | 引号内区名 | 文件头注释、非 `local-zone` 行 | `local-zone: "..." always_null` |

## 可复制的转换骨架

教学骨架：只读本地 `sources/`，不访问网络，仅依赖 Python 标准库，输出归一化域名列表。

```python
#!/usr/bin/env python3
# normalize.py —— 本地清单 → 一行一个域名；不依赖真实清单 URL
import re, sys
from pathlib import Path

SKIP_PREFIX = ("##", "@@", "!", "#")
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

def extract(line: str) -> str | None:
    """抽出候选主机名；DNS 层表达不了的规则整条返回 None。"""
    s = line.strip()
    if not s or s.startswith(SKIP_PREFIX):
        return None
    m = re.match(r'^local-zone:\s*"([^"]+)"', s, re.I)
    if m:
        return m.group(1)                    # Unbound 原生 / OISD
    if "##" in s or "+js" in s or "$" in s:  # 元素隐藏、脚本、$domain=
        return None
    if s.startswith("||"):
        s = s[2:].split("^", 1)[0]
    s = re.sub(r"^https?://", "", s, flags=re.I)
    if "/" in s:                           # URL 路径，整条跳过
        return None
    s = s.split("?", 1)[0].split(":", 1)[0]
    parts = s.split()                      # HOSTS：取右侧第一个域名
    if len(parts) >= 2 and IPV4.match(parts[0]):
        s = parts[1]
    s = s.split("#", 1)[0].strip().lower().rstrip(".")
    if s.startswith("*."):
        s = s[2:]
    if s.startswith("www."):
        s = s[4:]
    return s or None

def valid(host: str) -> bool:
    """只保留含点且长度 > 2 的整域名。"""
    if "." not in host or len(host) <= 2 or IPV4.match(host):
        return False
    return all(LABEL.match(l) for l in host.split("."))

def main() -> int:
    skipped, seen = 0, set()
    for p in sorted(Path("sources").glob("*.txt")):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            h = extract(line)
            if h is None or not valid(h):
                skipped += 1
            else:
                seen.add(h)
    allow = {l.strip().lower() for l in
             Path("unblock_domains.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")}
    for h in sorted(seen - allow):        # 白名单在最终去重前应用
        print(h)
    print(f"# skipped={skipped} kept={len(seen - allow)}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

**为什么这么设**：`extract()` 把六类格式压成一条归一化流水线，`$` 与 `/` 检查放在拆 `||` / `^` **之前或紧随其后**，保证带 `$domain=` 与带路径的规则整条返回 `None` 而不是被抠出主机名；`valid()` 只做与 DNS 相关的最少校验；`seen` 是集合，去重一次完成，避免 cn/noncn 各自去重后再合并时重复；纯 IP 的 IOC 计入 `skipped`，报告里能看见"丢了多少、为什么丢"。**怎么验证**：`mkdir -p sources && printf '0.0.0.0 ads.example.net\n||tracker.example.org^\n##.banner\n' > sources/demo.txt && : > unblock_domains.txt && python3 normalize.py` —— 应只输出 `ads.example.net` 与 `tracker.example.org`，stderr 里 `skipped` 至少为 1。**失败模式 / 回滚点**：常见错是把"跳过"写成"提取"，例如只取 `cdn.example.net` 而丢掉路径；`valid()` 会放行这种错误，所以路径判断必须在 `extract()` 里。骨架只读 `sources/`、只写 stdout，不落盘，删掉输入文件即完全复原。

配套的 sed 骨架用于把已有 unbound 清单刷成统一类型（正式流程走上面的 Python）：

```bash
# 类型改写 + 补齐尾点 + 去重；输出只在 /tmp
sed -n 's/^local-zone:[[:space:]]*"\([^"]*\)"[[:space:]].*/local-zone: "\1." always_null/p' \
    sources/oisd-unbound.txt | sed 's/\.\."/."/' | sort -u > /tmp/adhole-normalized.conf
```

**为什么这么设**：`\([^"]*\)` 捕获引号内区名，输出统一补 FQDN 尾点，第二道 `sed` 修掉原本已带点时的双点，`sort -u` 是重复定义的最后防线（24.6.5）。**怎么验证**：`grep -c 'always_null' /tmp/adhole-normalized.conf ; grep -vc 'always_null' /tmp/adhole-normalized.conf` —— 第二条应为 0。**失败模式 / 回滚点**：上游若用单引号或不带引号的区名会静默丢行、计数却不报错；输出只在 `/tmp`，`install` 前不影响运行中配置，正式流程以 Python 骨架为准。

## 输出三份表与 domain-state.tsv

| 文件 | 规模 | 内容 |
|---|---|---|
| `adhole-cn.conf` | 约 6k 条 | 归类为 cn 的域名 |
| `adhole-noncn.conf` | 约 27MB | 归类为 noncn 的域名 |
| `adhole-full.conf` | 约 28MB / 58 万条 | cn + noncn 合并去重后的完整表 |
| `domain-state.tsv` | 与表项同量级 | 状态与分类依据 |

`adhole-full.conf` 是两台解析器实际 `include` 的那一份；cn / noncn 分开保留，是为了按来源灰度与单独回滚。三份都写成 24.2 开头那种单一行形态，不设表头。`unbound-checkconf` 能查语法，但**不校验语义重复**：同名区出现两次它照样通过；若表文件里混进 `local-zone: "example.net." always_null`，会把本章验收用的示例域一起挡掉。回滚：保留上一版 `adhole-full.conf`，`install` 回退后 reload（24.9）。

`domain-state.tsv` 五个字段固定，以制表符分隔：

```text
domain	category	last_seen	last_checked	ip
ads.example.net	noncn	2026-11-07	2026-11-07	192.0.2.7
tracker.example.org	cn	2026-11-07	2026-11-07	198.51.100.9
```

`domain` 是归一化后的整域名，与表项一一对应作主键；`category` 为 `cn` / `noncn` / 来源标签，决定写入哪份表、回滚时撤哪份清单；`last_seen` 是该域名最近一次出现在上游清单的日期，用于老化下线；`last_checked` 是最近一次主动解析校验的日期——**出现 ≠ 仍可解析**，两者必须分开；`ip` 是解析得到的 A 记录地址，既是分类依据也是误伤排查线索。上面的日期与地址均为教学示例，实际值由生成时的解析结果写入。

**为什么这么设**：五个字段把"表项是什么、为什么归这一类、上一次证据何时取"分开存，`unbound-checkconf` 只认表文件、不读 TSV，因此 TSV 的格式错误不会影响解析、却会让回滚与误伤排查失去依据；固定制表符分隔是为了能用 `awk -F'\t'` 直接查。

**怎么验证**：`unbound-checkconf /etc/unbound/unbound.conf && grep -c '^local-zone:' /etc/unbound/adhole-full.conf`（应接近 58 万），再用 `unbound-control list_local_zones | grep -c always_null` 核对生效条目数。**失败模式 / 回滚点**：`domain-state.tsv` 是生成物，每次生成保留带日期副本（24.11），回退到上一版即可恢复分类。

## 分类靠解析 A 记录，不靠域名后缀猜

`category` 的判定流程是：**对每个候选域名做一次 A 记录解析，拿到应答里的 IP，再对照本地 `geoip-cn.json` 判断该地址是否属于 cn 网段**。命中则 `cn`，否则 `noncn`；解析失败（NXDOMAIN / SERVFAIL / 超时）的条目记入单独状态，下一轮 `last_checked` 再试，不要凭后缀猜一个分类塞进去。

为什么要绕这一圈：域名后缀是**注册管理**信息，IP 归属是**托管位置**信息，两者没有必然关系。以 `.cn` 结尾的域名可以托管在境外，以通用后缀结尾的域名可以托管在境内 CDN；某些服务还会按解析来源返回不同 IP，后缀完全不携带这一层信息。过滤表要分的是"这一批域名由哪份清单负责、回滚时撤哪一份"，按后缀猜会让分类与实际托管脱节，灰度时看不出影响面。

三个约束：① 解析必须走**受控的递归解析器**（第 4–6 章的实例），不要用本机存根解析器随手 `getaddrinfo`——后者可能被搜索域、mDNS、`/etc/hosts` 污染；② `geoip-cn.json` 按网段前缀匹配，自身要有版本与更新记录以保证比对可复现，具体数据来源与格式见官方资料，本章不假定任何特定提供方；③ 只重解析 `last_checked` 超阈值的条目，58 万条全量每天重查既慢又给上游制造无意义负载。

**怎么验证**：`awk -F'\t' 'NR>1 && ($5 == "" || $4 == "") {print}' domain-state.tsv | head` 应无输出；再抽检若干 `cn` 条目的 `ip` 是否落在 `geoip-cn.json` 的 cn 段。**失败模式 / 回滚点**：上游临时 NXDOMAIN 会把域名误判成"无分类"，若转换器直接丢弃就产生静默漏放；正确做法是保留条目、标记待复查、本轮沿用上一次 `category`。回滚：回退上一版 `domain-state.tsv`。

## `local-zone` 与 `forward-zone` 的优先级（本章核心）

一个看似简单、最容易配错的问题：**被写进过滤表的名字，会不会先被送到上游 `forward-zone`？** 不会。

### 查询处理顺序

```text
① 收包 → access-control（谁可以问；最具体网段匹配，无匹配即 refuse）
② local-zone 树查找（按最具体的区名匹配；tag / view 在此层参与）
③ 命中且类型要求本地应答 → 直接生成权威应答，返回客户端
④ 未命中 / 类型为 transparent 类 → 查缓存
⑤ 缓存未命中 → RPZ QNAME 触发器（在 local-zone 之后、auth-zone 之前）
⑥ auth-zone / forward-zone / stub-zone / 递归 → 出向查询
⑦ 应答回来 → 缓存 → 返回客户端
```

官方手册明确写了三条次序：权威区在 `for-downstream: yes`（默认）时"在 local-zones 之后、cache 之前处理"；RPZ 的 QNAME 触发器"在任意 local-zone 之后、任意 auth-zone 之前应用"；`auth-zone` 的 `for-upstream` 阶段在缓存查找之后、出网之前。合起来就是上面的顺序。

结论：**`local-zone` 匹配发生在递归与转发之前**。`always_null` 在 ② 命中后于 ③ 直接返回 `0.0.0.0` / `::`，查询根本不会抵达 ⑥ 的 `forward-zone`。过滤表不是一个"应答回来后再改写"的钩子，而是查询路径上的提前出口，所以它能零上游查询地生效。由此得分工：**`forward-zone` 只对未被本地区接管的名字生效**。写 `forward-zone: "."` 把一切交给单一上游（第 5 章的单上游约束）并不意味着过滤表失效；恰恰相反，命中表的名字在到达这个 `forward-zone` 之前就被终结了。反过来，既没进表、又被 `transparent` 类区放过的名字才会真正走到上游。

### `always_*` 与 `static` / `transparent` 的语义表

`local-zone` 第二个参数决定"命中后若无 `local-data` 精确匹配，给什么应答"：

| 类型 | 命中后行为（无 `local-data` 精确匹配时） | `local-data` | 适用场景 |
|---|---|---|---|
| `always_null` | 对区内每个名字无条件返回 `0.0.0.0` / `::` | 忽略 | **过滤表默认类型**；空答对客户端最友好 |
| `always_nodata` | 无条件 NOERROR/NODATA（名字存在、该类型无数据） | 忽略 | 只想让 A/AAAA 查不到、又不想给 NXDOMAIN |
| `always_nxdomain` | 无条件 NXDOMAIN | 忽略 | 明确宣告"此域不存在"，可能触发重试 |
| `always_transparent` | 按正常流程向上解析 | 忽略 | 临时关掉这个区的本地接管 |
| `always_refuse` | 返回 REFUSED，连该区名的 DS 查询也挡 | 忽略 | 强阻断；手册提示会打断 DNSSEC 信任链 |
| `always_deny` | 静默丢包 | 忽略 | 不推荐：DNS 不擅长按策略丢包，引发重试 |
| `static` | 有 `local-data` 就答，否则 NODATA / NXDOMAIN（带 SOA） | 使用 | 本地权威小Zone，名字固定 |
| `transparent` | 有 `local-data` 就答；**不同名字**正常解析；同名不同类型回 NODATA | 使用 | 只覆盖少数名字、其余照常上网 |
| `typetransparent` | 有 `local-data` 就答；不同名字**或同名不同类型**都正常解析 | 使用 | 只覆盖 A，不希望 AAAA 被 NODATA 连带影响 |

最后两行的差异在双栈环境里是实打实的：用 `transparent` 给 `gw.example.net.` 写了 A 记录，客户端问 AAAA 会收到 NODATA；用 `typetransparent` 则正常向上解析 AAAA。`always_*` 前缀的含义是**忽略 `local-data`**；手册对 `always_null` 的原文是"对区内每个名字总返回 `0.0.0.0` 或 `::0`，类似带零数据的 redirect，忽略区里的 local data，用于某些屏蔽清单"——这正是本章选它作表项类型的依据。

### `local-data` 与 `local-zone` 配合

`local-data` 提供精确的资源记录，`local-zone` 决定"没精确匹配时怎么办"。两条边界：若只写 `local-data` 而不写覆盖它的 `local-zone`，手册说明 Unbound 会自动创建 `transparent` 区，所以**"没写区"不等于"没接管"**，而是以最宽松的类型接管；`always_null` / `always_nodata` / `always_refuse` 等忽略 local data，在这些区里配 `local-data` 是无效的。想让某个被屏蔽域返回特定地址，应改用 `redirect` 或 `static` + `local-data` 并把它从过滤表移除。常见需求"挡掉广告域但 `sso.example.net` 要能解析"的正确做法，不是在 `always_null` 区里塞 `local-data`，而是把该域放进 `unblock_domains.txt`（24.1），或给父域用 `typetransparent` 配例外；前者更简单可审计，是本项目默认路径。

### `access-control` 与 `local-zone` 的先后

`access-control` 在 ①、`local-zone` 在 ②，因此**访问控制先于本地接管**：被 `deny` / `refuse` 的源地址连本地应答都拿不到，谈不上"命中过滤表"。这解释了一个常见疑惑——从没放行的网段 `dig` 一个被屏蔽域，看到的是 REFUSED 而不是 `0.0.0.0`，别据此判断过滤表没生效。手册给出的 `deny_non_local` / `refuse_non_local` 只允许查询权威 `local-data`、不给递归，适合"只想让某网段拿到本地静态应答"；普通 `allow` 对非递归查询也允许访问 `local-data`。`access-control` 按**最具体网段**匹配且与语句顺序无关，这与 `include` 顺序的规则正相反，两者不要混记。完整基线见第 9 章。

### `include` 顺序与重复定义

`include:` 可出现在配置任意位置，语义是"把被包含文件的文本像复制粘贴一样插到这一点"，也支持通配符。三条工程结论：① **段上下文会延续**——`include` 写在 `server:` 段中间，表文件里的裸 `local-zone:` 行才属于 `server:`；写在 `forward-zone:` 段之后会被解析进错误的段。稳妥做法是用 `include-toplevel:`（手册说明它会先关闭当前段、强制被包含文件自带段声明），或确保 `include` 位于 `server:` 段内。② **`chroot` 下路径必须可见**，相对路径依赖启动目录 / `directory:` 设置，配合 AppArmor 还要求落在白名单内（24.9）；第 7 章已给过"三处同时满足"的约束，这里不重复。③ **同名区重复定义不应依赖"谁生效"**：手册对 `unbound-control local_zone` 的说明是"若该区已存在，类型被改为给定参数"，即后写入者覆盖先前类型；但把 58 万行表的正确性寄托在覆盖顺序上是脆弱的，`unbound-checkconf` 不报重复、日志也不一定显眼。正确做法是生成器里用集合去重、`sort -u` 收尾，保证每份表内每个区名只出现一次；跨文件重复（例如 `adhole-full.conf` 与手工例外文件同时定义同一域）用 `list_local_zones` 核对实际生效类型。一句话：include 顺序决定"哪些语句在哪个段、哪份表先被读到"，但**业务上的覆盖关系由生成器保证，别靠配置顺序碰运气**。

### `auth-zone` 与 `local-zone` 的边界

`auth-zone` 是从文件加载的权威区副本，两个可同时开启的阶段：`for-downstream: yes`（默认）在 local-zone 之后、cache 之前，让 Unbound 像权威服务器一样直接回答下游；`for-upstream: yes`（默认）在缓存查找之后、出网之前，供递归过程查本地副本。它与 `local-zone` 位置相邻但职责不同：**`local-zone` 是策略（该不该答、答什么），`auth-zone` 是数据（一整份区文件内容）**。手册另说明：需要带引荐、通配、CNAME/DNAME、DNSSEC 权威服务的复杂权威数据，应当用 `stub-zone` 而不是 `local-zone`。所以第 6 章的本地根区镜像用 `auth-zone`，本章的过滤表只承担"按名字给空答"，别因为"都叫 zone"就混用。`forward-zone` 与 `auth-zone` 的区别也清楚：前者把查询交给另一台**递归**服务器，后者提供本地区文件内容；二者都在 ⑥ 附近参与，而 `local-zone` 在 ② 就可能终结查询。

### RPZ 与 `local-zone` 的区别

RPZ（Response Policy Zone，响应策略区）把策略写成标准 DNS 区文件格式，可通过区传送从外部来源加载，不同解析器实现可共用；`local-zone` 是 Unbound 专有语法。官方手册给出的次序是 **RPZ QNAME 触发器在任意 `local-zone` 之后、任意 `auth-zone` 之前应用**，且 RPZ 依赖 `respip` 模块、需在 `module-config` 中加载。因此同时配了二者的实例里 `local-zone` 先判，被 `always_null` 终结的名字不会再经过 RPZ。RPZ 的优势是标准化、可传送、能按应答 IP / 客户端 IP 等更多触发器做策略，代价是多一层模块与区文件管理；本项目选 `local-zone` 大表，理由是生成链路全在本地、可原子替换、可按来源灰度。触发器精确匹配语义、动作类型与加载顺序请以官方手册 RPZ 章节为准，本章不展开。

### 用 `dig` 证明"命中 local-zone 时上游零查询"

```bash
# ① 表是否已加载
unbound-control list_local_zones | grep 'ads.example.net'
# ② 记录统计基线
unbound-control stats_noreset > /tmp/adhole-stats-before.txt
# ③ 只查被屏蔽的名字，连查 5 次
for i in 1 2 3 4 5; do dig @192.0.2.1 ads.example.net A +short; done
# ④ 对比出向计数
unbound-control stats_noreset > /tmp/adhole-stats-after.txt
diff /tmp/adhole-stats-before.txt /tmp/adhole-stats-after.txt | grep -E 'cachemiss|queries|recursivereplies'
# ⑤ 对照组：查未进表的名字
dig @192.0.2.1 open.example.org A +short
unbound-control stats_noreset | grep -E 'total.num.cachemiss|total.num.queries'
```

**为什么这么设**：`always_null` 生成的是本地权威应答，不进入递归路径，因此 `total.num.cachemiss` 与 `total.num.recursivereplies` 不应因 ③ 增长；`total.num.queries` 会增长，因为查询确实被收下了——这正是要区分的两个量。手册说明 `recursivereplies` 是"发给需要递归处理的查询的应答数"，缓存应答不计入，所以它是比"延迟"更硬的证据。⑤ 的对照组证明统计本身是活的、不是计数器没动。**怎么验证**：③ 的五次应答都应是 `0.0.0.0`；④ 的 `cachemiss` 增量为 0；⑤ 之后 `cachemiss` 至少 +1。**失败模式 / 回滚点**：若实例之前查过这个被屏蔽名字、缓存里有条目，`cachemiss` 本来就不会涨，容易得出假阳性，所以要先确认是冷查询（多实例链路上逐层刷缓存的要求见第 7 章，这里不重复配置）；另一个坑是 `dig` 打到了别的实例，用 `unbound-control -c <配置>` 或核对 `@` 后的地址与端口。这组命令只读统计、不改配置，唯一副作用是 `dig` 本身产生的查询记录。

补充定性验证：在受控上游侧临时观察这几个名字的查询是否出现；dnstap 的采集与保留期配置在第 7 章，本章只把它当**证据来源**，不重复其配置细节，本书统一的全量查询日志保留 30 天并配到期删除在这里同样适用。

## 原子生成：临时文件 + 校验 + `install`

58 万行的表绝不能"边下载边覆盖正在被读的文件"。完整性校验的最低要求是：**最后一行必须是合法的 `local-zone` 行**。这一条能抓住绝大多数"下载中断 / 磁盘写满 / 管道被 `head` 截断"的不完整表——截断在行边界时 `unbound-checkconf` 语法仍合法，却会静默漏放大批域名。

```bash
#!/usr/bin/env bash
# build-adhole.sh —— 输入是本地 sources/ 与 unblock_domains.txt，不访问网络
set -euo pipefail
OUT=/etc/unbound/adhole-full.conf
TMP=$(mktemp /etc/unbound/.adhole-full.XXXXXX)
trap 'rm -f "$TMP" /tmp/adhole-names.txt' EXIT

python3 normalize.py > /tmp/adhole-names.txt
awk '{printf "local-zone: \"%s.\" always_null\n", $0}' /tmp/adhole-names.txt > "$TMP"

# 完整性校验：非空、行数下限、最后一行必须是合法 local-zone
[[ -s "$TMP" ]]
[[ $(wc -l < "$TMP") -ge 500000 ]]
tail -n 1 "$TMP" | grep -Eq '^local-zone: "[a-z0-9.-]+\." always_null$'

install -m 0644 -o root -g root "$TMP" "$OUT"
unbound-checkconf /etc/unbound/unbound.conf
unbound-control reload || systemctl restart unbound
echo "installed $(grep -c '^local-zone:' "$OUT") zones"
```

**为什么这么设**：`mktemp` 把临时文件放在同一文件系统，`install` 的落位才可能是原子替换；`set -euo pipefail` 让任何一步失败都中止，绝不把半成品推到 `OUT`；`trap` 保证异常退出时清掉临时文件。`tail -n 1 | grep -Eq` 是本章刻意要求的那道校验，它验证的是"文件被完整写完"这个 `unbound-checkconf` 不负责的性质；`install -m 0644 -o root -g root` 明确权限与属主，配合 24.8 的 AppArmor 白名单。**怎么验证**：`ls -l "$OUT" ; tail -n 1 "$OUT" ; unbound-control list_local_zones | grep -c always_null`，三处形态与数量应一致；再跑 24.6.8 的 `dig` 组。**失败模式 / 回滚点**：`wc -l` 的 500000 是本项目 58 万条口径的宽松阈值，上游规模变化时会误报"表变小"，此时应人工确认而不是直接放宽；`unbound-checkconf` 只查语法、不查重复定义与语义。回滚：保留上一版 `adhole-full.conf`（24.11），`install` 回退旧文件后 reload；手册列出 `interface` 等少数选项 reload 不生效，`local-zone` 本身可 reload，但混在其他变更里时 restart 更稳。

## 大表的内存代价、加载时间与 AppArmor

`local-zone` 大表**全量常驻内存**：Unbound 把区名组织成一棵树，每个节点要存标签、类型、可选 tag / local-data 指针，再加分配器开销。磁盘上约 28MB，进内存后远不止 28MB——一行约五十字节的文本变成结构体、树指针与对齐填充，量级通常按数倍估算；具体数值取决于版本、编译选项与是否启用 tag/view，**不要照搬任何固定倍数，以本机基线为准**。三个可观测代价：启动 / reload 时解析 58 万行建树，耗时明显长于小配置；手册说明 `fast_reload` 期间需**约两倍**配置内存（新旧两份并存），峰值可能触发 OOM；稳态下与缓存争内存，缓存尺寸需相应下调。

**怎么验证**：`unbound-control stats_noreset | grep -E 'mem|queries' ; ps -o rss= -p "$(pidof unbound)"` —— 改动前后各取一次基线，记录 RSS 增量与启动耗时；给大表留出的内存要从缓存预算里扣，不能两套都按满配（缓存尺寸与线程/slabs 规划见第 7 章）。另有一个隐蔽成本：每次查询都要在这棵树里做最具体匹配，它与缓存命中、RPZ 判定、日志采集一起构成每查询固定开销；加载大表前后各测一次命中数与 `recursion.time.median`，才能把"变慢"归因到具体环节。**失败模式 / 回滚点**：最危险的不是慢，而是 `fast_reload` 的双倍内存峰值触发 OOM，表现为进程被杀、反复重启；内存紧张时宁可低峰期 restart（restart 不需要同时持有新旧两份配置）。回滚：回退上一版表或先只加载 `adhole-cn.conf`（约 6k 条）观察，再逐步放开。

第 9 章已把 AppArmor 基线讲透：**profile 只允许 Unbound 读 `/etc/unbound/**`**，把表文件放到 `/var/lib/unbound/` 或 `/tmp` 会被 DENIED，而 `unbound-checkconf` 照样通过——它不检查内核的强制访问控制判决。本章补三条：临时文件必须在 `/etc/unbound/` 下（`mktemp /etc/unbound/.adhole-full.XXXXXX`）；`install` 后属主 / 权限要与 profile 一致，profile 允许的读路径与 Unix 权限是两道独立的门；排查顺序固定为 `unbound-checkconf` → `journalctl -k | grep DENIED` → 文件路径 → include 顺序，不要先怀疑上游或表内容。**怎么验证**：`unbound-checkconf /etc/unbound/unbound.conf ; journalctl -k --since '10 min ago' | grep -i denied || true ; aa-status | grep unbound` —— 语法通过、无 DENIED、进程处于 enforce 模式，三者齐备才算加载成功。**失败模式 / 回滚点**：reload 后 `list_local_zones` 为空且日志无报错，第一嫌疑是 DENIED 被 audit 级别吞掉，第二嫌疑是 `include` 写在错误的段里（24.6.5）；把表移回 `/etc/unbound/**` 并重启，不要为此放宽 profile，那等于把第 9 章的最小暴露面拆了。

## 定时同步到另一台解析器

两台解析器（家庭网关 `gw-01` 与云主机 `edge-01`）用同一张表，靠定时同步保证口径一致。

```bash
#!/usr/bin/env bash
# sync-adhole.sh —— 在生成端执行；目标主机名与密钥均为教学示例
set -euo pipefail
SRC=/etc/unbound/adhole-full.conf
STATE=domain-state.tsv
tail -n 1 "$SRC" | grep -Eq '^local-zone: "[a-z0-9.-]+\." always_null$'
# 先推到目标端的暂存路径，校验通过后才覆盖 include 指向的路径
scp -q "$SRC" "edge-01:/etc/unbound/.adhole-full.incoming"
scp -q "$STATE" "edge-01:/etc/unbound/domain-state.tsv"
ssh edge-01 'tail -n 1 /etc/unbound/.adhole-full.incoming | grep -Eq "^local-zone: .* always_null$" && \
             install -m 0644 /etc/unbound/.adhole-full.incoming /etc/unbound/adhole-full.conf && \
             rm -f /etc/unbound/.adhole-full.incoming && \
             unbound-checkconf /etc/unbound/unbound.conf && \
             systemctl restart unbound'
```

**为什么这么设**：**被覆盖的 `include` 路径必须先讲清**——目标端主配置里 `include: "/etc/unbound/adhole-full.conf"` 是唯一入口，`scp` 先落到 `.adhole-full.incoming` 暂存，校验通过后由 `install` 覆盖的正是这个路径；`edge-01` 上不要另有一份手工维护的同名文件，也不要让 `include` 用通配符把备份（`adhole-full.conf.bak.*`）一起读进来，否则出现 24.6.5 的重复定义。同步的是**生成物**而不是 `sources/`，口径才不会因两端脚本版本不同而漂移；`domain-state.tsv` 一并同步，让目标端具备同样的误伤排查线索。目标端先在暂存路径上查最后一行，再 `install` 覆盖 `include` 指向的目标、固定权限，`unbound-checkconf` 验语法，最后 **restart**（`fast_reload` 大表时的双倍内存峰值见 24.8，同步窗口宁可短时重启）；顺序反了会留下属主错误或半截文件，且 `restart` 才能让新表稳定生效。凭据用 `<TOKEN>` 或等价密钥认证，不写明文口令；定时用 systemd timer 或 cron 均可，间隔与上游清单更新节奏一致。**怎么验证**：`ssh edge-01 'unbound-control list_local_zones | grep -c always_null' ; unbound-control list_local_zones | grep -c always_null` 两端计数应一致，再各自跑 24.6.8 的被屏蔽名字，都应答 `0.0.0.0`。**失败模式 / 回滚点**：网络中断会让 `scp` 写出半截文件，所以先推暂存路径、校验通过后才 `install` 到被 `include` 的路径；两端版本不一致时先让目标端回退上一版表（24.11），不要在线修补。回滚：目标端 `install` 旧备份并 restart；若只有一端失败，暂停同步任务，避免下一轮把好端也覆盖。

## 误伤排查、白名单回滚点与保留期

过滤表最常见的生产事故不是"没挡住"，而是"挡错了"。排查四步：① **`dig` 定性**——`dig @192.0.2.1 sso.example.net A +short` 若返回 `0.0.0.0`，先确认是表在起作用而不是源站真挂了，再用 `unbound-control list_local_zones | grep sso.example.net` 查是精确区还是父域区命中；② **dnstap 找证据**——看这次查询的 QNAME、类型、客户端地址与应答，确认哪一层给的空答，采集配置与 30 天保留期在第 7 章，本章只把它当查询流证据来源、不重复配置；③ **定位来源**——`grep sso.example.net domain-state.tsv` 看 `category` 与 `ip`，再回查 `sources/` 里是哪份清单引入了它（24.2.6 的来源标签在这里派上用场）；④ **白名单回滚**——把域名加进 `unblock_domains.txt`，重跑 24.3 生成器与 24.7 原子落位。因为白名单在**最终去重前**应用，重生成后它会从三份表里一起消失，不会被 cn/noncn 合并拼回来。

```bash
# 误伤应急：写白名单并重生成（教学示例）
printf 'sso.example.net\n' >> unblock_domains.txt
bash build-adhole.sh
dig @192.0.2.1 sso.example.net A +short
```

**为什么这么设**：应急改动只落在 `unblock_domains.txt` 一个文件，重生成走与日常完全相同的流水线，不引入"手工改一行表文件"这类无法复现的状态；`dig` 复核放最后，避免"白名单写了但没生效"被当成已修复。**怎么验证**：`grep sso.example.net /etc/unbound/adhole-full.conf` 应无输出，`unbound-control list_local_zones | grep sso.example.net` 应无输出，`dig` 应返回真实应答。**失败模式 / 回滚点**：reload 后链路上其他实例可能仍留有旧应答，要按第 7 章逐层确认缓存；父域级拦截（`*.` 降级或 `www.` 归一化）造成的误伤，加精确白名单未必够，需要把父域一并加入或改回更窄的清单。回滚点两处：`unblock_domains.txt` 去掉该行即恢复拦截，`adhole-full.conf` 回退上一版即整体撤销本轮更新。

### 保留期与版本化

| 产物 | 保留策略 | 理由 |
|---|---|---|
| `adhole-*.conf` 历史版本 | 按日期保留有限份（如最近 7 天 / 若干版），到期删除 | 回滚点要够用，但不能成为无期限的域名画像存档 |
| `domain-state.tsv` 历史版本 | 同上；`last_seen` / `last_checked` 用于老化下线 | 长期不再出现的条目应退出表 |
| dnstap 全量查询日志 | **30 天**，配到期删除 | 与第 7 章一致；查询流副本最敏感 |
| `sources/` 上游清单快照 | 与表版本对应保留 | 生成可复现，才谈得上审计 |

版本化做法：`install` 前把当前 `OUT` 复制成带日期的备份，备份文件本身**不要被 `include` 的通配符扫到**（24.9）。每次生成写一份 manifest，记录表行数、跳过计数、三份表规模、`geoip-cn.json` 版本与生成时间；误伤排查先看 manifest，能立刻判断是不是这一轮引入的。`last_seen` 的老化要保守：连续多轮未出现在上游清单才从表中移除，移除同样走正常重生成流程并保留旧版本以便回滚。不要因为怕漏放就永远不删条目——表越大，24.8 的内存与加载代价越高，误伤面也越宽。

## 与第三篇的三处呼应

- **第 9 章最小暴露面**：过滤表是"答什么"的策略，`access-control` 是"谁可以问"的门，门在策略之前（24.6.4）。表文件放在 `/etc/unbound/**` 内是 AppArmor 白名单的硬要求；任何为图方便放宽 profile 的做法都等于把最小暴露面拆掉。
- **第 7 章可观测性**：dnstap 是自己的查询流副本，本章只用它做误伤证据与"上游零查询"的定性验证，采集开关、六类消息与 30 天保留期一律沿用第 7 章；大表加载前后的 RSS、命中数与 `recursion.time.median` 基线也沿用该章方法。
- **第 12 章 DoH 端点与过滤表的关系**：DoH 只是把 DNS 报文装进 HTTPS，**不改变解析器内部的查询处理顺序**。第 12 章的分工是 dnsdist / nginx 做前端、Unbound 只监听回环做递归验证；只要客户端最终落到这台 Unbound，`local-zone` 就在递归/转发之前生效，过滤表照常工作。反过来，若浏览器绕过本地解析器直连第三方 DoH 端点，过滤表完全看不见这次查询——那属于第 13 章的 DDR 压制与 canary 域要解决的问题，不是把表加大能补救的。这里也再次体现全书一贯区分：DoH / DoT 是**传输加密**，DNSSEC 是**数据完整性与来源认证**，过滤表是**本地策略**，三者互不替代。

## 本章要点

- **DNS 层只能按名字拦截**：URL 路径、`$domain=`、元素隐藏 `##`、脚本 `##+js`、白名单 `@@` 都表达不了，必须整条跳过而不是近似提取；`unblock_domains.txt` 在最终去重前应用，避免白名单被 cn/noncn 合并拼回来。
- 六类上游格式（HOSTS 两种写法、纯域名 TXT、ABP `||domain.tld^`、Unbound 原生、威胁情报 TXT、OISD unbound 输出）统一归一为 `local-zone: "domain." always_null`；顺序是去 `||`/`^` → 剥 `*.` → 去 `http(s)://` → 去尾 `/` → 去 `www.` → 只保留含点且长度 > 2 的整域名。`always_null` 忽略 `local-data`，例外要走白名单。
- **`local-zone` 匹配发生在递归与转发之前**：`always_null` 在访问控制之后直接返回 `0.0.0.0` / `::`，不会再走 `forward-zone`；`forward-zone` 只对未被本地区接管的名字生效。用 `stats_noreset` 的 `cachemiss` / `recursivereplies` 前后对比加未命中对照组，可以证明"命中时上游零查询"。
- `always_nodata` / `always_transparent` / `always_refuse` / `always_deny` 都忽略 `local-data`；`static` 无匹配给 NODATA/NXDOMAIN，`transparent` 与 `typetransparent` 的区别在"同名不同类型"是否继续向上解析。`include` 是文本插入、同名重复别指望覆盖顺序，靠生成器集合去重；RPZ QNAME 触发器在 `local-zone` 之后、`auth-zone` 之前。
- 三份表（cn 约 6k 条、noncn 约 27MB、full 约 28MB / 58 万条）与 `domain-state.tsv` 五字段配套；分类靠**解析 A 记录并对照 `geoip-cn.json`**，不靠域名后缀猜，因为后缀是注册管理信息、IP 才是托管位置信息。
- 工程闭环：临时文件 + 校验最后一行合法 `local-zone` + `install` 原子落位；AppArmor 只许读 `/etc/unbound/**`；58 万行表全量常驻内存、`fast_reload` 有约两倍配置内存的峰值；同步时讲清被覆盖的 `include` 路径；误伤走 `dig` → dnstap → 白名单重生成；历史表、状态、dnstap 各有保留期（查询流 30 天）与版本化回滚点。

## 参考资料

- Unbound 官方手册，`unbound.conf(5)`（`local-zone` 各类型、`local-data`、`include:` / `include-toplevel:`、`access-control`、`forward-zone`、`auth-zone`）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，Response Policy Zones 章节（RPZ 触发器次序、`respip` 模块、与 `local-zone` 的关系）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/filtering/rpz.html
- Unbound 官方手册，`unbound-control(8)`（`list_local_zones`、`local_zones` / `local_zones_remove`、`stats_noreset`、`fast_reload` 的内存说明）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- Unbound 官方手册，`unbound-checkconf(8)`（检查范围与用法）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-checkconf.html
- Unbound 官方手册，Performance Tuning（缓存内存、线程与 slabs 的规划）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/core/performance.html
- NLnet Labs，Unbound 项目文档入口（版本相关的默认区清单、AppArmor 与模块配置说明）：https://nlnetlabs.nl/documentation/unbound/
- RFC 5737，*IPv4 Address Blocks Reserved for Documentation*（本章示例地址段）：https://www.rfc-editor.org/rfc/rfc5737
- RFC 1034，*Domain Names — Concepts and Facilities*（区、权威应答与递归的基本概念）：https://www.rfc-editor.org/rfc/rfc1034
