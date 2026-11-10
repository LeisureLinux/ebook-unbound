# WPAD 与 PAC：自动代理发现的实践

一台客户端要决定"这个请求走不走代理"，得先拿到一份能执行的小文件；**WPAD（Web Proxy Auto-Discovery）负责找到这份文件的 URL，PAC（Proxy Auto-Config）负责在文件里给出判断**。这两件事常被混成一个词，混完之后排障几乎总会找错层：DNS 里查不到 `wpad` 名字是发现层的问题，`FindProxyForURL()` 返回了错的分流结果是配置层的问题，两者的日志、验证命令和回滚点完全不同。

先把边界立清楚。本书第五篇的主线是策略网关上的**透明代理**：流量在网关被接管，客户端不需要知道代理存在，也不需要谁来告诉它"去哪里找配置"。WPAD 在这条主线里只有两个位置——给那些必须靠自动配置才能工作的**遗留客户端兼容**，以及把"名字 → 配置 → 流量路径"这条链讲透的**原理科普**。它不该出现在新架构的依赖图里，最后一节会把理由摊开讲。



## 两层：找 URL 与决定怎么走

### WPAD 层

输入是客户端所处的网络上下文（DHCP 应答、DNS 域名、search 域），输出是**一个 URL**，典型形态是 `http://wpad.example.net/wpad.dat`。它不解析这个 URL 里的内容，也不做任何分流判断；它成功与否，只决定客户端手里有没有一个可以取 PAC 的地址。

### PAC 层

输入是 `url` 与 `host`，输出是一串**代理指令字符串**：`DIRECT`、`PROXY 198.51.100.10:3128`，或用分号串起来的回退链 `PROXY 198.51.100.10:3128; DIRECT`。文件本身是一段 JavaScript，入口函数固定为 `FindProxyForURL()`。客户端每发起一个请求就调用一次它，所以 PAC 里每一次 `dnsResolve()` 都是一次额外的域名查询。

| 维度 | WPAD（发现层） | PAC（配置层） |
|---|---|---|
| 输入 | DHCP / DNS / 本地域名后缀 | 请求的 URL 与主机名 |
| 输出 | PAC 文件的 URL | `DIRECT` / `PROXY` / 回退链 |
| 失败后果 | 客户端退回"不配置代理"或沿用旧配置 | 单次请求走错路径或被拒 |
| 标准状态 | 过期的工作组草案（见下节） | Netscape 1996 的事实约定，无正式 RFC |
| 能否替换 | 能用显式 PAC URL 或手工配置绕开 | 能用静态代理设置绕开 |
| 排障入口 | DHCP 包、`dig`、系统代理开关 | `pactester`、浏览器开发者工具 |

**这一分层是全章的骨架**：后面所有"配好了却不生效"的问题，先问一句"是没找到 URL，还是找到了 URL 但文件里的判断不对"，能省掉一半的瞎试。

## 标准状态：一份过期的工作组草案

### draft-ietf-wrec-wpad-01

WPAD **从未成为 RFC**。它唯一成文的定义是 IETF WREC 工作组的 Internet-Draft `draft-ietf-wrec-wpad-01`：**1999-07-29 发布，1999-12 过期**，之后没有推进到 RFC 序列。本书凡提到"WPAD 规范要求"时，指的都是这份**已过期草案**里的条文，不是任何 RFC 的规范性要求——这一点在写文档、做验收、跟厂商对口径时最容易出错。

因此本章不会给 WPAD 编一个 RFC 编号，也不会把草案里的 `MUST` / `SHOULD` 当成 RFC 级别的互操作义务。草案引用的周边协议倒是有正式编号：DHCP 选项见 [RFC 2132]，SLP 见 [RFC 2608] / [RFC 2609]，Well-Known Alias 的约定见 [RFC 2219]。DNS SRV 记录类型与其余拿不准的编号一律**见官方资料**，章内不填。

### 过期的东西为什么还在跑

一份 1999 年过期的草案能活到今天，靠的是客户端实现的默认行为，不是标准推进。主流操作系统与浏览器里的"自动检测代理设置"开关，实现的是草案描述的发现流程；只要开关默认勾上，网络里存在一条 `wpad` A 记录或一个 252 选项，客户端就会自己去取 `wpad.dat`。**事实标准不等于标准**：它没有版本协商、没有能力通告、没有失败语义的统一定义，各家实现在细节上分叉，正是后面"缓存与降级"一节里行为不一致的来源。

## 草案 §4.4：五种发现机制与 MUST / SHOULD

草案 §4.4 列了五种发现途径，并用规范性用词分了级。下表按草案原文的分级抄录，注解是本章补的实践说明：

| # | 机制 | 草案分级 | 客户端动作 | 依赖 / 注解 |
|---|---|---|---|---|
| 1 | DHCP option 252 | **MUST** | 从 DHCP 应答里取出 PAC URL | 252 **不在 [RFC 2132] 正式选项表**；落在 IANA 私有/保留编号段 |
| 2 | SLP | **SHOULD** | 按服务类型查 `service: URLs` 属性 | [RFC 2608] / [RFC 2609] |
| 3 | Well-Known Alias `wpad.TGTDOM.` 的 A 记录 | **MUST** | 解析 A 记录，再 HTTP GET `/wpad.dat` | [RFC 2219]；最常被触发的一条 |
| 4 | DNS SRV | **SHOULD** | 查 SRV 记录得到主机与端口 | 记录类型定义见官方资料 |
| 5 | DNS TXT `service: URLs` | **SHOULD** | 读 TXT 中的 URL 列表 | 与 SLP 共用同一属性名 |

再强调一次：这些 `MUST` / `SHOULD` 是**草案内部**的分级，不是 RFC 义务。实现里最常见的组合其实是第 1 条加第 3 条——先问 DHCP，拿不到再拼 `wpad.<域名>` 去查 DNS。

### DHCP option 252 的尴尬位置

252 这个编号不在 [RFC 2132] 的正式 DHCP 选项表里，属于 IANA 编号空间中的私有/保留用途段（具体区间划分见官方资料），靠的是各家实现之间的事实约定。后果有两个：

- 服务端虽然"必须"给，但没有任何标准条文规定值的格式、编码与超长时的处理，不同 DHCP 服务端填法略有差别；
- 客户端若在租约期内才启用自动检测，或第一次应答没带上 252，可以另发 **DHCPINFORM** 单独补取该选项，不必等续租。

所以"我明明配了 252，客户端却没拿到"这类问题，第一现场是 DHCP 报文，不是 DNS 日志。抓包确认 252 是否出现在 OFFER / ACK / DHCPINFORM 的应答里，再往下走。

### SLP 与两条 DNS 附加机制

SLP 是草案里排在 SHOULD 档的服务发现协议，规范见 [RFC 2608] 与 [RFC 2609]。它需要一个可用的服务目录，部署成本明显高于"加一条 A 记录"，因此在小型网络里基本见不到。DNS SRV 与 DNS TXT `service: URLs` 同样只是 SHOULD：前者把主机与端口写进记录，后者直接把 URL 列表放进 TXT。三者都要求客户端主动实现相应查询，而草案过期之后没有推动力去统一实现，实际覆盖率远低于第 1、3 条。

### Well-Known Alias：一次 A 记录加一次明文 HTTP GET

第 3 条是本章实践部分要落地的那条。流程极简：把 `wpad.` 拼在目标域名（草案称 `TGTDOM`）前面，查 **A 记录**，取到地址后向该地址的 80 端口请求 `/wpad.dat`。没有 TLS、没有签名、没有内容校验；整条链的安全性全押在"这个 `wpad` 名字归谁"上。[RFC 2219] 讲的是 Well-Known Alias 这一命名约定本身，不是 WPAD 的安全模型——把两者混读，会误以为这条链有标准背书。

## DNS devolution：会走出组织边界的那一跳

### 逐级剥左侧标签

`TGTDOM` 从客户端的 DNS 域名（或 search 域）来。草案允许的 **devolution（域名退化）**是：查不到就剥掉最左侧一个标签再查，**一直剥到只剩 TLD** 为止，任一步取到 A 记录即停止。

```text
客户端所属域：corp.example.net
TGTDOM 初值：corp.example.net.

  第 1 跳   wpad.corp.example.net.   -> 组织内 DNS，可控
  第 2 跳   wpad.example.net.        -> 组织内 DNS，可控
  第 3 跳   wpad.<公共后缀>.          -> 公共命名空间，不可控
  命中任一跳的 A 记录即停止，向该地址 HTTP GET /wpad.dat
```

问题就在第 3 跳：`example.net` 这一层在真实部署里往往已经不是组织可写的区；再往外剥到 TLD，名字彻底落到公共命名空间。**devolution 的设计假定"越往上越靠近组织自己"，实际恰好相反**——剥标签是在往组织的控制边界外面走。

### `wpad.<公共后缀>` 与 W-Bad 类攻击

历史上确实有人把公共后缀下的 `wpad` 名字注册下来并指向自己的服务器；客户端一旦 devolution 越界，就会把 `wpad.dat` 的**内容归属**交给一个组织外的注册者。这类问题在文献里被归到 **W-Bad / WPAD 名称冲突攻击**一类：它不需要攻破 DNS，不需要伪造应答，只需要等客户端自己走完最后一跳，然后在返回的 PAC 里把感兴趣的目标指向攻击者控制的代理。具体事件年份、编号与命名细节**见官方资料**，本章不填不确定的数字。

这里的关键不是"某个后缀被抢注过"，而是模型本身：**配置内容的可信度，由一个可能被外部持有的名字决定**。DNSSEC 也无法救这一格——它保证应答没被篡改、来源可认证，但不保证"这个合法持有者就是你的网络管理员"。这与全书反复区分的两件事一致：DNSSEC 管数据完整性与来源认证，DoT / DoH / DoQ 管传输加密与链路隐私，谁都不管**命名归属的授权**。

### 三条缓解，按优先级排

1. **用内网保留域，并把所有派生名字本地接管。** 客户端域名后缀收敛到自己能完全控制的区；Unbound 上用 `local-zone` + `local-data` 把 `wpad.<内网域>` 钉死，同时把 devolution 可能落到的外层候选名字也在本地判死，让"最后一跳"根本不出网。
2. **显式关闭 WPAD。** 对不需要它的客户端，直接关掉系统/浏览器里的自动检测开关，比在 DNS 里围堵更彻底：没有发现流程，就没有 URL，也就没有取 PAC 这一步。开关位置见后文，具体路径随版本变化，以官方资料为准。
3. **不要依赖 search 域。** 别把"客户端会拼出什么名字"寄托在 DHCP 下发的域名后缀上；后缀是输入，不是策略。需要固定行为时给显式 PAC URL 或静态代理配置。

三者的共同点是把**决定权从名字搬回配置**。第 1 条仍在 WPAD 框架内打补丁，第 2、3 条是直接绕开 WPAD——对遗留客户端之外的机器，后者才是本书推荐的取向。

## 现代化尝试：draft-joshco-wpadng-02

2024 年出现过 `draft-joshco-wpadng-02`：一份**个人草案**（非工作组立项），**同样已过期**。它更像一份"如果重做一遍该改什么"的设计笔记，值得读的是它点出的四个方向：

- 用 **DNS-SD** 替代广播式 / 猜测式的服务发现，让发现结果有明确的服务实例语义；
- 补上 **DHCPv6** 侧的配置途径，1999 年的草案只考虑了 IPv4 的 DHCP 选项；
- 引入 **`urn:wpadng:none`**，显式表达"本网络没有代理配置"这一状态，避免"找不到就猜、猜不到就沿用旧值"的含糊地带；
- **去掉 devolution**，从根上切断上一节那个越出组织边界的最后一跳。

这四个方向恰好对应本章前三节的每一处别扭：发现语义含糊、v6 缺席、失败状态未定义、名字归属失控。但它是过期个人草案，**没有任何实现承诺或互操作保证**，本书不落地、不写配置、也不建议拿它做选型依据。它的用处是当一面镜子：一份想修好 WPAD 的草案，要动的地方全在"自动发现"这件事本身。

## PAC 层：`FindProxyForURL()` 与四个常用函数

### 函数与坑

PAC 来自 **Netscape 1996** 的代理自动配置约定，入口函数固定为 `FindProxyForURL(url, host)`，**没有正式 RFC**。它是一段受限的 JavaScript 环境：没有 DOM、没有网络 API，只有几个由宿主提供的辅助函数。

| 函数 | 作用 | 本章要留意的坑 |
|---|---|---|
| `dnsResolve(host)` | 把主机名解析成点分 IPv4 字符串，失败返回 `null` | 每次调用都是一次额外 DNS 查询；它的解析路径未必与系统解析器一致 |
| `isInNet(ip, addr, mask)` | 判断 IP 是否落在点分十进制网段内 | 面向 IPv4 字面量设计；IPv6 判断 historically 弱，别依赖 |
| `shExpMatch(str, pattern)` | shell 通配符匹配 | 只做字符串匹配，不校验 DNS 层级，也不是任何形式的来源认证 |
| `myIpAddress()` | 返回本机一个 IPv4 地址 | 多网卡、双栈、VPN 并存时返回值不确定 |

**IPv6 支持弱**不是一句带过的历史注脚，而是会直接改变分流结果：`dnsResolve()` 多数实现只返回 A 记录，`myIpAddress()` 只给一个 IPv4，`isInNet()` 的掩码写法也围着 IPv4 转。于是纯 IPv6 目标常常解析不到可用的点分地址，静默落到 `FindProxyForURL()` 的默认分支。写 PAC 时应当**显式处理解析失败**，而不是假设每个 `host` 都能变成 IPv4。

### 完整可复制的 wpad.dat

下面这份是教学用的 DIRECT / PROXY 分流示例，地址全部来自文档地址段，域名只有 `example.net` / `example.org`；`198.51.100.10:3128` 是示例代理端点，`192.0.2.10` 是提供 PAC 的站点地址。

```javascript
function FindProxyForURL(url, host) {
    var DIRECT = "DIRECT";
    var PROXY  = "PROXY 198.51.100.10:3128";

    // 1) 内网保留域与本机名：直连，避免把管理流量绕进代理
    if (shExpMatch(host, "*.example.net") || shExpMatch(host, "example.net") ||
        shExpMatch(host, "*.example.org") || shExpMatch(host, "example.org") ||
        shExpMatch(host, "localhost")) {
        return DIRECT;
    }

    // 2) 名字 -> 地址；解析失败不硬送代理，显式处理 IPv6 弱支持的后果
    var ip = dnsResolve(host);
    if (ip === null) {
        return DIRECT;
    }

    // 3) 落在文档地址段内的目标直连
    if (isInNet(ip, "192.0.2.0",   "255.255.255.0") ||
        isInNet(ip, "198.51.100.0", "255.255.255.0") ||
        isInNet(ip, "203.0.113.0", "255.255.255.0")) {
        return DIRECT;
    }

    // 4) myIpAddress() 仅作条件分流演示：本机在示例网段时尝试代理
    if (isInNet(myIpAddress(), "192.0.2.0", "255.255.255.0")) {
        return PROXY + "; " + DIRECT;
    }

    return DIRECT;
}
```

**为什么这么设**：四个函数各出现一次，便于逐个验证；`dnsResolve()` 返回 `null` 时直接 `DIRECT`，是为了让"解析不到就不猜"成为显式策略，而不是掉进默认分支；文档地址段一律直连，保证示例环境里不会出现代理回环。

**怎么验证**：`pactester -p ./wpad.dat -u https://example.org/index.html -c 192.0.2.20`（参数以工具官方资料为准），期望输出 `DIRECT`；把示例域名换成清单外的名字再跑一次，检查是否按预期落到代理分支。

**失败模式 / 回滚点**：第 4 步的 `PROXY ...; DIRECT` 是**教学演示的回退链，不是生产写法**——代理不可用时它会静默直连，正是后文"缓存与降级"批评的 fail-open 行为。生产里应把它改成单一 `PROXY` 并接受失败，或干脆不用 PAC。回滚只需保留上一版 `wpad.dat`，按下一节的原子替换流程换回。

## 落地：把 wpad.dat 放到一个可控的 URL 上

### nginx 提供 /wpad.dat

```conf
# /etc/nginx/conf.d/wpad.conf
server {
    listen 80;
    server_name wpad.example.net;

    location = /wpad.dat {
        default_type application/x-ns-proxy-autoconfig;
        alias /srv/wpad/wpad.dat;
        add_header Cache-Control "no-store, max-age=0" always;
        access_log /var/log/nginx/wpad-access.log;
    }

    location / {
        return 404;
    }
}
```

**为什么这么设**：`default_type` 直接给出 `application/x-ns-proxy-autoconfig`，不必往 `types` 里加映射；`location =` 精确匹配，避免别的路径误命中；`no-store` 压掉客户端与中间件的缓存；单独一份 access log 便于回答"到底哪些客户端来取过"。**这里用 80 端口不是建议**，而是因为 Well-Known Alias 流程默认就是明文 HTTP GET，改成只允许 HTTPS 会让一部分遗留客户端直接取不到——这个妥协本身就是后文"不推荐"的理由之一。

**怎么验证**：`curl -I http://wpad.example.net/wpad.dat`，应答里应有 `200`、`Content-Type: application/x-ns-proxy-autoconfig` 与 `Cache-Control: no-store, max-age=0`。

**失败模式 / 回滚点**：`default_type` 被上层 `types` 覆盖时客户端拿到 `text/plain`，部分实现会拒绝执行；`alias` 路径写错则 404，客户端表现为"自动检测开着但没有代理"。回滚：`nginx -t` 通过后再 `nginx -s reload`，并保留旧 `wpad.dat` 副本；最坏情况下注释掉该 `server` 块，让发现流程拿不到 URL。

### Unbound 把名字收在本地

```conf
# /etc/unbound/unbound.conf.d/wpad.conf
server:
    local-zone: "example.net." static
    local-data: "wpad.example.net. 300 IN A 192.0.2.10"
    local-data: "wpad.example.net. 300 IN AAAA 2001:db8::10"

    # 演示：把不该外泄的 devolution 候选名字在本地判死
    # 此处必须换成你自己客户端会派生出的那些名字，照抄示例没有意义
    local-zone: "devolution-candidate.example.org." always_nxdomain
```

**为什么这么设**：`local-zone` 让 `example.net.` 成为本地接管的区，`local-data` 把 `wpad.example.net.` 同时钉到 IPv4 与 IPv6 文档地址；这样第 3 条发现机制解析出的地址始终指向自己那台 nginx，`wpad` 名字的归属不外包给公共命名空间。`always_nxdomain` 那一行演示的是"把外层落点也在本地判死"的思路——**它是占位示例**，名字写成一个明显不存在的标签以免被照抄；真实环境里应把客户端 devolution 会派生出的候选名字逐一枚举后本地接管。

**怎么验证**：`unbound-checkconf && unbound-control reload && dig @127.0.0.1 wpad.example.net A +short`，期望 `192.0.2.10`；再跑 `dig @127.0.0.1 wpad.example.net AAAA +short`，期望 `2001:db8::10`；最后查 `dig @127.0.0.1 wpad.devolution-candidate.example.org A +short`，期望无应答，说明外层落点没有出网。

**失败模式 / 回滚点**：只写 `local-data` 不写 `local-zone`，Unbound 会为这些数据隐含建一个 `transparent` 区，域内**其他**名字仍会走递归——devolution 的外层落点照样可能出网，这是最隐蔽的一种"配了等于没配"。反过来，`static` 区里没有本地数据的名字会被判无应答，若 `example.net.` 下还有别的业务名，会一起断掉；回滚把它改成 `transparent` 让未匹配名字落回递归。`local-zone` 各类型的完整语义见第 24 章，此处不展开。

### 生成脚本骨架：域名清单 → PAC 规则

手工维护 `shExpMatch` 列表迟早会漏项。骨架思路是：一份纯文本域名清单，生成一个只含直连规则的 `wpad.dat` 新文件，校验通过后再安装。

```python
#!/usr/bin/env python3
"""域名清单 -> PAC 规则（骨架）。示例地址仅为文档地址段。"""
from pathlib import Path

LIST = Path("/srv/wpad/domains.txt")      # 每行一个域名，# 开头为注释
OUT = Path("/srv/wpad/wpad.dat.new")     # 临时产物，校验后再 install
PROXY = "PROXY 198.51.100.10:3128"
DIRECT_SUFFIXES = ("example.net", "example.org")

def read_domains(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip().lower().rstrip(".")
        if not name or name.startswith("#"):
            continue
        if "." not in name or len(name) <= 2:
            continue                    # 只收含点的整域名
        yield name

def render(names):
    patterns = list(dict.fromkeys(list(DIRECT_SUFFIXES) + list(names)))
    rules = "\n".join(
        f'        shExpMatch(host, "*.{d}") || shExpMatch(host, "{d}") ||'
        for d in patterns
    )
    return f"""// 由 {LIST.name} 生成，请勿手工编辑
function FindProxyForURL(url, host) {{
    var direct = "DIRECT";
    if (
{rules}
        false) {{
        return direct;
    }}
    return "{PROXY}";
}}
"""

if __name__ == "__main__":
    OUT.write_text(render(read_domains(LIST)), encoding="utf-8")
    print(f"written: {OUT}")
```

**为什么这么设**：输出到 `.new` 而不是线上文件，把"生成"与"生效"分开；清单里先过滤注释、空行、裸标签和短名，避免把脏数据拼进规则；末尾的 `false` 让拼接出来的条件链在任何清单长度下都语法合法。

**怎么验证**：`python3 /srv/wpad/gen-pac.py && pactester -p /srv/wpad/wpad.dat.new -u https://example.org/`，确认输出 `DIRECT`；确认无误再 `install -m 0644 /srv/wpad/wpad.dat.new /srv/wpad/wpad.dat`。

**失败模式 / 回滚点**：清单里混入带路径或通配符的条目会生成永远不匹配的规则；空的 `domains.txt` 会生成"所有请求都走代理"的文件，等于把全量流量推给示例端点。回滚点就是上一版 `wpad.dat` 与 `.new` 并存：只要不执行 `install`，线上文件没有任何变化。原子替换与完整性校验的完整工程做法见第 24 章。

## 校验、开关与排障

### pactester

`pactester`（pacparser 项目提供的命令行工具）在本地执行 PAC，不需要真的开浏览器、也不需要真的有代理在跑，适合把"文件逻辑对不对"和"发现流程对不对"切成两半验证。常用姿势是固定 `-u` 给一个 URL、必要时用 `-c` 指定客户端 IP，观察返回串：

```bash
pactester -p /srv/wpad/wpad.dat -u https://example.org/index.html -c 192.0.2.20
# 期望：DIRECT

pactester -p /srv/wpad/wpad.dat -u https://example.org/index.html -c 203.0.113.20
# 期望：按第 4 步条件分流的结果，用于确认 myIpAddress 分支
```

返回 `DIRECT` 不代表发现层成功，只代表**这份文件**对这组输入给出直连。工具的具体参数与安装方式**见项目官方资料**，本章不写死版本号。

### 浏览器与系统开关

| 位置 | 相关开关（名称随版本变化） | 关闭方式 |
|---|---|---|
| 系统网络设置 | "自动检测代理设置" / "自动代理发现" | 取消勾选；企业环境用配置描述文件或组策略统一下发 |
| 浏览器连接设置 | "使用系统代理设置" / "自动检测此网络的代理设置" | 改为"不使用代理"或填显式 PAC URL |
| 命令行与库 | `http_proxy` / `https_proxy` 等环境变量 | 显式清空；这类客户端通常不读 WPAD |

**别把"关掉"当成一次性动作**：新接入的终端、重装后的系统、临时借用的设备都会带着默认勾选回来。可行做法是把"自动检测关闭"写进终端基线，并在网关侧保留显式关闭的兜底（第 2 条缓解）。具体开关名、组策略项与配置描述文件字段**见官方资料**，不同版本差异较大。

### 日志与排障三板斧

按"发现层 → 传输层 → 配置层"的顺序查，不要跳步：

```bash
curl -I http://wpad.example.net/wpad.dat
# 看 HTTP 状态、Content-Type、Cache-Control；404 或 text/plain 都是配置层前置失败

dig @127.0.0.1 wpad.example.net A +short
dig @127.0.0.1 wpad.example.net AAAA +short
# 看名字是否被本地区接管；返回空或落到公共地址，说明 devolution 未被收住

grep 'wpad.dat' /var/log/nginx/wpad-access.log
# 看客户端到底来取过没有；没有这一行，问题在发现层而非 PAC 内容
```

三者对应三种完全不同的故障：`curl` 失败是服务端配置，`dig` 偏离预期是名字归属，`grep` 无记录是客户端根本没发起发现。浏览器开发者工具用于最后一步：在请求列表里确认代理连接实际连向哪个地址，以及 PAC 是否被重新取过（缓存命中时不会再次出现在日志里）。若怀疑 DHCP 252，则回到报文层面抓 OFFER / ACK / DHCPINFORM，这一层 `dig` 帮不上忙。

排障时最容易被误导的是**缓存**：改完 `wpad.dat` 而客户端毫无变化，多半不是"没生效"，而是它手里还拿着上一版。先清浏览器与系统的代理配置缓存、或用全新的无痕会话复测，再判断文件本身有没有问题。

## 为什么不推荐它

前几节把 WPAD 配到了能跑，这一节说明为什么本书不把它写进新架构。四条理由都是结构性的，不是"配置不够仔细"能补掉的。

### 默认开启，等于没有决策点

自动检测往往默认勾选。运维没有主动选择"启用 WPAD"，网络里出现一条 `wpad` A 记录或一个 252 选项，一批客户端的出向路径就变了。透明代理主线里，路径由网关策略决定、变更有配置与审计；这里路径由**客户端的一次名字查询**决定，变更点分散在 DHCP、DNS 和每台终端的开关上，很难给出一个"谁在什么时候改了它"的答案。

### 名称劫持：可信度押在名字归属上

从 `wpad.<公共后缀>` 到 W-Bad 类攻击，问题不是某个后缀没注册好，而是**配置内容的归属由 DNS 名字的归属决定**。devolution 又把这个归属边界推到组织之外。内网保留域 + 本地接管能把边界拉回来，但没改变"靠猜名字拿配置"这个模型；一旦有客户端绕过本地解析器、或带了别的 search 域，边界立刻重新打开。DNSSEC 也补不上这一格：它证明应答来自合法持有者，不证明合法持有者是你的管理员。

### 明文 HTTP 取 PAC

取 `wpad.dat` 的那一跳是明文 HTTP，链路上改内容不需要破解任何东西。PAC 不是数据，是**决定后续所有连接走向的程序**：改一行 `FindProxyForURL()`，就能把指定目标导向别处。业务连接上的 TLS 只保护业务连接，不保护"取配置"这一步；这与第 12 章讨论的加密解析是两码事，不要混为一谈。给 `wpad.dat` 上 HTTPS 也只能覆盖愿意配合的客户端，遗留实现仍会走 80。

### 缓存与降级，和 fail-closed 正面冲突

PAC 在浏览器与系统层都有缓存，`no-store` 只能约束自己这台 nginx 的应答，管不到下游各自的实现；改了文件，客户端可能一段时间内仍执行旧规则。更麻烦的是失败语义：发现不到 URL 时，有的客户端退回"无代理"，有的沿用上次配置；PAC 里写 `PROXY ...; DIRECT` 时，代理挂掉会静默直连。**同一个故障，不同客户端给出不同结果**，而且都是 fail-open。第三篇反复强调的 `forward-first: no`、严格档 fail-closed，在这里没有任何对应物——协议层就没定义"失败时该怎么办"。

### 收口

把四条并起来看：WPAD 把一个本该由网关集中决策的策略，拆成"名字归属 + 明文取件 + 客户端缓存 + 各家失败语义"四段，每段都能单独改变结果。透明代理不需要客户端配合，也不存在"谁来告诉它去哪里"这个问题；本书第五篇的网关策略、按 uid 豁免本机解析器出向查询那一套，才是主线。

因此本章的定位不变：**遗留客户端兼容**与**原理科普**。前者是为少数只会读自动配置的旧终端留一个受控的兼容口——内网保留域、本地接管、显式关闭其余终端、单独日志；后者是用它把"名字 → 配置 → 流量路径"这条链演示清楚。新架构的依赖图里不该有 WPAD；需要自动下发配置时，用范围明确、可审计、失败时行为确定的机制，而不是一次 `wpad.dat` 的猜测。

## 本章要点

- **两层必须分开**：WPAD 只负责找到 PAC 的 URL（DHCP 252、DNS、devolution），PAC 的 `FindProxyForURL()` 才决定 `DIRECT` / `PROXY`；"没找到 URL"与"文件判断错了"的日志、命令与回滚点完全不同。
- **标准状态要写准**：WPAD 从未成为 RFC，唯一定义是 WREC 工作组草案 `draft-ietf-wrec-wpad-01`（1999-07-29 发布、1999-12 过期）；§4.4 的 MUST / SHOULD 是草案内部分级，252 不在 [RFC 2132] 正式选项表，PAC 本身也没有正式 RFC。
- **devolution 会越出组织边界**：逐级剥左标签直到 TLD，最后一跳落到公共命名空间，于是 `wpad.<公共后缀>` 的注册者可以决定 PAC 内容（W-Bad 类攻击）；缓解靠内网保留域 + 本地接管、显式关闭自动检测、不依赖 search 域。
- **落地三件套**：nginx 以 `application/x-ns-proxy-autoconfig` 提供 `/wpad.dat`，Unbound 用 `local-zone` + `local-data` 把 `wpad.example.net` 钉在文档地址段，域名清单生成脚本产出 `.new` 后经 `pactester` 校验再 `install`；排障按 `curl -I` → `dig` → access log → 开发者工具逐层推进。
- **不推荐作为新架构依赖**：默认开启、名称劫持、明文 HTTP 取 PAC、缓存与各家 fail-open 降级，都是结构性缺陷；本书主线是网关侧的透明代理，WPAD 只用于遗留客户端兼容与原理科普。
- 2024 年的 `draft-joshco-wpadng-02` 是已过期的个人草案，提出 DNS-SD、DHCPv6、`urn:wpadng:none` 与去掉 devolution；只作设计方向的记录，不落地、不做选型依据。

## 参考资料

- IETF Datatracker，`draft-ietf-wrec-wpad-01`（WPAD 唯一成文定义）：https://datatracker.ietf.org/doc/html/draft-ietf-wrec-wpad-01
- IETF Datatracker，`draft-joshco-wpadng-02`（2024 个人草案）：https://datatracker.ietf.org/doc/html/draft-joshco-wpadng-02
- RFC Editor，[RFC 2132] *DHCP Options and BOOTP Vendor Extensions*：https://www.rfc-editor.org/rfc/rfc2132.txt
- RFC Editor，[RFC 2608] *Service Location Protocol, Version 2*：https://www.rfc-editor.org/rfc/rfc2608.txt
- RFC Editor，[RFC 2219]（Well-Known Alias 的 DNS 别名约定）：https://www.rfc-editor.org/rfc/rfc2219.txt
- RFC Editor，[RFC 5737] *IPv4 Address Blocks Reserved for Documentation*：https://www.rfc-editor.org/rfc/rfc5737.txt
- NLnet Labs，Unbound 官方文档（`local-zone` / `local-data` 语义以此为准）：https://unbound.docs.nlnetlabs.nl/en/latest/
- nginx 官方文档（`default_type`、`alias`、`add_header` 指令以此为准）：https://nginx.org/en/docs/

[RFC 2609] 与章内提到的 DNS SRV 记录类型原文，均在 RFC Editor 按编号索引，完整链接由第四篇统一 RFC 索引章收录；`pactester` 的安装与参数、浏览器与系统代理开关的具体名称与路径、W-Bad 类攻击的事件年份与编号，一律**见官方资料**。若 Datatracker 草案链接发生重定向，按草案名在 IETF Datatracker 检索即可。
