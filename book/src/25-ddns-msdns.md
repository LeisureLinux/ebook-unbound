# 内网 DDNS 与微软 DNS 对照

内部名字不是凭空出现的：一台机器拿到 DHCP 租约，或者一个服务注册了自己，总得有人把「名字 → 地址」写进某个权威区。Unbound 在这条链路上扮演的角色经常被误读，最常见的一句话是「把 Unbound 的 DDNS 配一下」。这句话没有对应的配置项，也没有对应的功能。先把这个立论钉死，再看微软 DNS 那套真正实现了动态更新的模型，最后给出纯 Unbound 架构里让内网动态名字落地的三条绕行路径。



与相邻章节的边界：第 2 章已经做过主流实现的功能横评，本章不再贴产品对照表；第 6 章给了本地根区镜像的完整配置，本章只在必要处一句交叉引用，不重画那份配置。

## Unbound 不接受动态更新

### FEATURES 文件里的原文口径

NLnet Labs 在 Unbound 仓库的 `doc/FEATURES` 里把能力边界写得很直接：

```text
Big Features: Recursive service. Caching service.
Forwarding and stub zones. Very limited authoritative service.

RFC 1995, 1996, 2136: not authoritative, so no AXFR, IXFR,
NOTIFY or dynamic update services are appropriate.
```

这段话有两层意思，不能只截前半句：

- Unbound 的定位是递归 / 缓存 / 转发 / stub，**权威服务是「非常有限的」（very limited）**，不是「兼作权威服务器」；
- 因为它不是权威服务器，AXFR、IXFR、NOTIFY 与 [RFC 2136] 的 dynamic update 服务「不适用」（not appropriate）—— 这是设计结论，不是「还没实现」的待办项。

于是：Unbound 不处理 `UPDATE` 报文、不做区传送服务端、不发 NOTIFY。官方手册里检索 `DDNS` / `RFC 2136` 命中为零；唯一一处 "dynamic update" 字样是 `num.query.opcode.UPDATE` 统计计数器的说明——它只用来数收到了多少个此类报文，不代表会应答。

**不要把本章后面任何一段称作「Unbound 的 DDNS 配置」。** 后面写的都是把更新交给别人做、Unbound 只负责查到结果，是架构绕行，不是功能开关。

### 「非常有限的权威服务」具体是什么

只剩两样东西：`local-zone` / `local-data` 的静态配置，以及 `unbound-control local_zone` / `local_data` 的运行时增删。先看静态形式：

```conf
# /etc/unbound/unbound.conf.d/10-local-data.conf（教学示例）
server:
    local-zone: "home.arpa." static
    local-data: "gw-01.home.arpa.   IN A    192.0.2.1"
    local-data: "edge-01.home.arpa. IN A    192.0.2.2"
    local-data: "gw-01.home.arpa.   IN AAAA 2001:db8::1"
    include: "/etc/unbound/local-data.generated.conf"
```

- **为什么这么设**：`static` 让本地区直接应答、不向外部递归；`local-data` 是逐条写死的资源记录；`include` 把「可再生成的那部分」单独放一个文件，为路径③留出替换接缝。
- **怎么验证**：`dig @198.51.100.53 gw-01.home.arpa. A +short`
- **失败模式 / 回滚点**：`local-zone` 类型或 `local-data` 拼写错会让 `unbound-checkconf` 直接报错；把 include 指向空文件或上一版本即可回滚，改完 `unbound-control reload` 生效。注意 `local-zone` 匹配发生在递归 / 转发之前，写错区名会静默接管本来该外发的查询（第 24 章讲过这个优先级）。

运行时增删靠控制通道，它改的是内存里的本地区，不是配置文件：

```bash
unbound-control local_zone home.arpa. static
unbound-control local_data gw-01.home.arpa. IN A 192.0.2.1
unbound-control local_data_remove gw-01.home.arpa.
```

- **为什么这么设**：适合临时加一条、临时挡一个名字做验证，不必 reload 整个实例。
- **怎么验证**：`unbound-control list_local_data | grep 'gw-01.home.arpa'`
- **失败模式 / 回滚点**：**不持久化**——进程重启或 `reload` 后运行时增删全部消失；用 `local_data_remove` 逐条撤回，或重启实例回到配置文件定义的状态。控制通道本身要按第 9 章收在受保护网络里，别把它当公开写入接口。

### 不持久化是设计，不是缺陷

把上面两条合起来看，Unbound 的本地区是「启动时从文件读、运行中可临时改、重启即回到文件」的模型。它天然缺少动态更新协议里最要紧的两件事：多客户端并发写入的鉴权，以及写入结果的持久落盘。所以内网名字要「动」起来，只有两条路：让别的进程去写权威区（路径①、②），或者让别的进程去写那个被 include 的文件（路径③）。配置管理——版本化文件 + 原子 `install` + reload——在这三种方案里都是兜底，不是可选项。

`unbound-control` 提供的 `local_zone` / `local_data` 名字里带 "local"，是因为它们服务于本地区，不是因为它们是 [RFC 2136] 的实现。把这两组命令当成「DDNS 接口」用，第一次重启就会丢数据。

## 微软 DNS：真正实现了 [RFC 2136] 的那套模型

这一节是事实对照，说明「动态更新」在成熟实现里长什么样，好让后面三条绕行路径知道自己省掉了什么。资料来源是 Microsoft Learn 的《Dynamic DNS Update in Windows and Windows Server》及同系列文档，具体条目按标题检索。

### UPDATE 的原子性与 prerequisite

[RFC 2136] 的 `UPDATE` 报文不是「改一条记录」的远程调用，而是**对一个区的一次原子事务**：一个报文里可以同时带删除、添加、以及 prerequisite（前置条件）。prerequisite 形如「这个名字必须不存在」「这个名字必须已有这条记录」，服务器先逐条检查前置条件，任一不满足就整包拒绝，一个字节都不改。

这一点决定了它和「脚本改文件再 reload」的根本差别：

| 维度 | [RFC 2136] UPDATE | 静态文件生成 |
|---|---|---|
| 一致性 | 单报文原子提交，前置条件失败则整体拒绝 | 文件级全量替换，靠生成脚本自己保证 |
| 并发写入 | 服务器串行化处理，带鉴权 | 多写者需外部加锁 |
| 生效点 | 服务器应答即生效 | reload 之后生效 |
| 部分失败 | 不存在 | 可能写出半截文件（须原子 `install`） |

后面路径①的 `nsupdate` 骨架正是靠这个原子性拿到「失败即未修改」的回滚语义；路径③没有这层保护，只能自己造哨兵和备份。

### 更新必须落到主服务器

标准主辅模型里，`UPDATE` 只能发给该区的**主服务器**（primary），由它改区数据再经区传送扩散到辅服务器。AD 集成区改变了这一点：区数据存在 Active Directory 里并可复制到多台域控制器，因此更新可以发给**任意一台持有该区副本的 DC**，由 AD 复制机制统一收敛。

这条差异直接影响规划：如果内部区是文件型主区，就必须保证客户端和 DHCP 都能稳定到达那一台主服务器，它是一个明确单点；换成 AD 集成区，写入点变成多台，但代价是引入了 AD 复制延迟与 DC 定位。

### 安全动态更新只对 AD 集成区可用

三种状态的边界要分清楚，最常见的误判是把「开了动态更新」等同于「开了安全动态更新」：

- **文件型主区**：默认不开放动态更新；可以手动改成允许，但没有 AD 身份体系可依托，只能靠「允许任何更新」或按地址粗粒度放行。
- **AD 集成区**：集成之后默认就是**「仅安全更新」（secure only）**；也可以放宽为「非安全 + 安全」或「不允许」，但那是显式降级，不是默认行为。
- **安全动态更新只对 AD 集成区可用**——没有 AD，就没有这套基于 Kerberos 的客户端身份与 ACL。

换句话说，「把区转成 AD 集成」这一步同时改了两件事：写入点从单点变成多点，默认策略从「不开放」变成「仅安全更新」。回滚时要一起考虑，别只导回 zone 文件。

### TKEY → TSIG → ACL → 签名应答

微软文档给出的安全更新时序（Windows 侧只用 Kerberos，不用其他 TKEY 模式）：

```text
1. 客户端先尝试一次不带签名的 UPDATE
2. 服务器按「仅安全更新」拒绝
3. 客户端与服务器做 TKEY 协商，建立安全上下文（Windows 只用 Kerberos）
4. 用协商出的密钥以 TSIG 签名正式 UPDATE 报文
5. 服务器按 ACL 校验「这个身份能不能改这个名字」，通过后写入 AD
6. 服务器返回同样带签名的应答
```

相关标准是 [RFC 3645]（GSS-TSIG），它把 GSS-API 与 TSIG 接起来；TKEY 与 TSIG 本身的规范按名称在 RFC Editor 检索，本章不臆断编号。第 3 步与第 5 步是安全性的两个支点：**前者证明「你是谁」，后者决定「你能改哪些名字」**。缺了 ACL，仅仅「能签名」就等于任何域用户都能覆盖别人的记录。

对照 Unbound：`local_data` 既没有 TKEY 也没有 ACL，它唯一的「权限」是能不能连上控制端口。这也是为什么运行时增删只适合调试、不适合做生产写入通道。

### 配套机制清单

动态更新不是孤立功能，微软 DNS 围绕它配了一整套东西：

- **DHCP 代注册**：客户端不自己发 UPDATE，由 DHCP 服务器在租约事件里代写 A / AAAA / PTR。
- **`DnsUpdateProxy` 组**：让 DHCP 代注册的记录不被当成「某台 DHCP 服务器私有」，否则后续真机自己来更新时会被 owner 检查挡掉。
- **`DHCID` 记录与 Name Protection**：用 `DHCID` 把名字和租约绑定，Name Protection 防止一个客户端抢注另一个客户端已有的名字（尤其 PTR）。
- **aging / scavenging（老化与清理）**：**默认关闭**；启用后 no-refresh 与 refresh 两个间隔**各 7 天**。默认关的意思是「陈旧记录会一直留着」，不是「自动清理已打开」。
- **`_msdcs` 与 DC locator**：域控定位专用的子域与 SRV 记录集，是 AD 自己最依赖的一组动态名字。
- **GlobalNames 区**：为不带后缀的单标签名字提供一套受控解析。

路径①若接的是 AD DNS，这套东西仍然由 AD 侧负责；Unbound 的 `stub-zone` 只是把查询送过去，不参与其中任何一环。

### 运维上真实会踩的坑

| 坑 | 现象 | 处理 / 回滚点 |
|---|---|---|
| 时钟偏差 > 5 分钟 | Kerberos 认证失败 → TKEY 协商失败 → 更新被拒，客户端名字不出现 | 先对齐域内时间源；排查时把时间同步列为第一检查项 |
| DHCP 用了高权限凭据 | 记录 owner 变成服务账户，真机后续无法自行更新或删除 | 改用**低权限专用凭据**；已写错的记录按 owner 批量清理后重建 |
| 反向区漏建 | 正向 A 记录正常、PTR 全无，反向校验与部分服务发现失败 | 正向区上线时同步建反向区并纳入巡检清单 |
| 误以为 scavenging 默认开 | 陈旧记录长期堆积，同一名字指向已释放地址 | 显式确认老化开关与两个 7 天间隔，先小范围试运行 |
| 区类型选错 | 想用「仅安全更新」却发现是文件型主区 | 区类型转换是变更动作，回滚点留在转换前导出的 zone 文件 |

前两行在路径①里同样适用：TSIG 共享密钥不依赖时钟，但 GSS-TSIG 依赖 Kerberos，一旦把脚本从 TSIG 切到 GSS-TSIG，时间同步就从「建议」变成「硬前提」。

### 社区观点，不是官方规定

NLnet Labs 邮件列表 2014-06 的 `[Unbound-users] Unbound vs MS Resolver` 讨论里有一种常见建议：AD 假设 DNS 服务器能接受 AD 认证的动态更新，所以域控与成员机最好指向微软 DNS；Unbound 放在外侧做**对外递归验证层**，把 AD 域用 `stub-zone` / `forward-zone` 指过去。

**这是社区观点，不是 NLnet Labs 的官方规定，也不是微软的强制要求。** 它描述的是一种减少摩擦的部署习惯：把「谁负责写内部区」和「谁负责解析外部名字」拆开，避免让一个递归验证器去承担它不做的写入职责。是否照做取决于现有 AD 拓扑，不属于协议约束。把它写成「官方要求域控必须指向微软 DNS」是错的。

## 三条绕行路径

> **以下三方案是本书对工程做法的自定义归纳，不是任何软件的官方功能列表，也不是「Unbound 的 DDNS」。** 共同前提是：Unbound 只做递归 / 验证 / 转发，写入动作全部发生在它之外。

### 路径① 外部权威 + DHCP 脚本写 nsupdate

租约事件触发脚本，脚本用 `nsupdate` 把记录写进一个真正的权威区；Unbound 用 `stub-zone` 指向那个权威。已有 BIND / NSD / AD DNS 的环境最省事。

`nsupdate` 教学骨架（TSIG 密钥放在权限受限的文件里，共享秘密用占位符）：

```bash
#!/usr/bin/env bash
# /usr/local/sbin/ddns-lease.sh —— 由 DHCP 租约事件调用，教学骨架
set -euo pipefail

ZONE="home.arpa."
SERVER="192.0.2.53"                # 权威服务器，教学用 RFC 5737 地址
KEYFILE="/etc/ddns/home-arpa.key"  # key "ddns-key" { algorithm hmac-sha256; secret "***"; };
NAME="gw-01.home.arpa."
ADDR="192.0.2.1"
TTL=300

/usr/bin/nsupdate -k "$KEYFILE" <<EOF
server $SERVER
zone $ZONE
update delete $NAME A
update add $NAME $TTL A $ADDR
send
EOF
```

- **为什么这么设**：先 `delete` 再 `add` 保证同一名字不留旧值；`zone` 明确目标区避免脚本打错区；`-k` 走密钥文件而不是把秘密写进命令行（命令行会进进程列表）；短 TTL 让租约变化能较快扩散。需要 AD 身份验证时改用 GSS-TSIG 模式，按 [RFC 3645] 与所用客户端文档检索，别把 TSIG 与 Kerberos 混成一种东西。
- **怎么验证**：`dig @192.0.2.53 gw-01.home.arpa. A +short`
- **失败模式 / 回滚点**：密钥错 / 时钟偏差 / ACL 不放行 → `nsupdate` 报 `update failed: REFUSED` 或 TSIG 校验错，此时区数据**未被修改**（原子性帮了忙）；脚本要记录租约旧值，回滚时用同一骨架把 `ADDR` 换回旧地址重放一次。密钥文件权限不是 `600` 就别上线。

Unbound 侧只需要一条指向权威的 stub：

```conf
# /etc/unbound/unbound.conf.d/20-internal-stub.conf（教学示例）
stub-zone:
    name: "home.arpa."
    stub-addr: 192.0.2.53
    stub-addr: 2001:db8::53
    stub-first: no
```

- **为什么这么设**：`stub-zone` 让 Unbound 对 `home.arpa.` 直接问权威、不走公网根；`stub-first: no` 是官方默认值，表示 stub 失败时**不**回退到常规递归，避免内部名字在权威故障时漏到公网。它与 `forward-zone` 的差别在于收到的是权威应答而非转发结果。
- **怎么验证**：`dig @198.51.100.53 gw-01.home.arpa. A +short`，与直连权威的结果比对，应一致。
- **失败模式 / 回滚点**：权威不可达时该区立即解析失败（不会自动回公网），这是刻意的 fail-closed；把这段 `stub-zone` 注释掉并 `unbound-control reload` 即回滚，但回滚后 `home.arpa.` 会落到公网递归、多半得到 NXDOMAIN——所以更稳妥的回滚是修权威，不是删 stub。

### 路径② NSD 权威 + Unbound 递归

同一台或另一台机器上跑 NSD 管内部区，Unbound 仍用 `stub-zone` / `forward-zone` 指过去；更新走 NSD 的 zonefile 生成，或由外部 `nsupdate` 写 NSD 的区。**明确一句：`auth-zone` 是只读的本地根区镜像，不是可写权威区**，配置与用途见第 6 章，本章不再贴那段配置。这条路径与①的差别只在「权威是自己人、且和递归器同机或近邻」，写入责任依旧不在 Unbound。

选它时要注意一个工程边界：权威与递归同机时，两者都监听 53 端口需要分地址或分端口；若还用了第 24 章那类 :53 劫持规则，本机解析器进程的出向 53 必须按 uid 豁免，否则递归查询会被自己的规则吃掉。

### 路径③ 静态生成 local-data include

DHCP 租约表 / 资产清单 → 生成 `local-data` 行 → 原子 `install` → reload。没有实时 UPDATE，但零额外组件。第 23 章 WPAD 的 `wpad.<内网域>`（本章写作 `wpad.home.arpa.`）就是这类静态产物的例子。

```bash
#!/usr/bin/env bash
# /usr/local/sbin/gen-local-data.sh —— 租约/资产清单 → include 文件，教学骨架
set -euo pipefail

SRC="/var/lib/ddns/leases.tsv"     # 列：name<TAB>addr
ZONE="home.arpa."
OUT="/etc/unbound/local-data.generated.conf"
BACKUP="${OUT}.prev"
TMP="$(mktemp "${OUT}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

{
    printf '# generated by gen-local-data.sh; do not edit\n'
    awk -F'\t' -v zone="$ZONE" 'NF==2 && $1 ~ /^[a-zA-Z0-9-]+$/ {
        printf "local-data: \"%s.%s  300 IN A  %s\"\n", $1, zone, $2
    }' "$SRC"
    printf '# EOF\n'                # 完整性哨兵：最后一行必须存在
} > "$TMP"

test -s "$TMP"                                         # 非空
tail -n 1 "$TMP" | grep -qx '# EOF'                    # 未被截断
if [ -f "$OUT" ]; then cp -p "$OUT" "$BACKUP"; fi      # 保留上一版本，回滚点
install -m 0644 -o root -g root "$TMP" "$OUT"           # 原子替换

if ! /usr/sbin/unbound-checkconf /etc/unbound/unbound.conf >/dev/null; then
    install -m 0644 -o root -g root "$BACKUP" "$OUT"    # 立即回滚
    exit 1
fi
/usr/sbin/unbound-control reload
```

- **为什么这么设**：先写临时文件、校验完整性与配置语法、最后 `install` 原子替换，避免 reload 读到半截文件；`# EOF` 哨兵让「生成被截断」可被一条命令检出；`awk` 里过滤名字字符，防止清单里的怪字符注入配置；`unbound-checkconf` 放在 `install` 之后、`reload` 之前，失败就把 `.prev` 装回去。
- **怎么验证**：`tail -n 1 /etc/unbound/local-data.generated.conf && dig @198.51.100.53 gw-01.home.arpa. A +short`
- **失败模式 / 回滚点**：清单格式错 → `awk` 少产记录，表现为个别名字消失；生成被截断 → 哨兵检查失败，旧文件**保持不变**，这是设计好的自动回滚；`unbound-checkconf` 不过 → 脚本立即还原 `.prev` 并退出，不 reload。人工回滚只需把上一版本 `install` 回去再 reload，所以每次生成都保留带版本号的副本。

### 三方案对照表

| 维度 | ① 外部权威 + `nsupdate` | ② NSD 权威 + Unbound | ③ 静态生成 `local-data` |
|---|---|---|---|
| 实时性 | 租约事件即写，秒级（受 TTL 与缓存影响） | 取决于 zonefile 生成或 `nsupdate` 频率 | 无实时性，等于生成 + reload 的周期 |
| 认证 | TSIG 共享密钥 / GSS-TSIG（[RFC 3645]） | 取决于权威侧配置，可沿用 TSIG | 无协议级认证，靠文件权限与生成机管控 |
| 额外组件 | 权威服务器、DHCP 脚本、密钥分发 | NSD（或同类权威）+ 生成 / 更新通道 | 无（只需 cron / systemd timer 与脚本） |
| 故障域 | 权威不可达 → 内部区整区解析失败 | 同左，但权威与递归可同机，链路更短 | 生成机故障只影响「新名字」，旧文件仍可解析 |
| 适用规模 | 中大型、已有权威体系或 AD | 中小规模、希望自管内区 | 小规模、名字变化不频繁（第 23 章 WPAD 类） |
| 回滚点 | 重放 `nsupdate` 写回旧值；密钥 / ACL 变更前快照 | 回滚 zonefile 版本并 reload | `install` 上一版本生成文件 + reload |

三行里最容易被低估的是「故障域」：①和②把内部名字的可用性绑到了权威服务器的可用性上，③把新名字的可用性绑到了生成机，但**已生成的名字活在本地文件里**。小规模场景偏爱③，不是因为它更先进，而是因为它的失败模式最温和。

### 验证命令与回滚点总表

```bash
# 权威侧：确认写入生效
dig @192.0.2.53 gw-01.home.arpa. A +short
dig @192.0.2.53 1.2.0.192.in-addr.arpa. PTR +short   # 教学用 RFC 5737 地址的反查名

# 递归侧：确认 Unbound 确实问的是权威而不是公网
dig @198.51.100.53 gw-01.home.arpa. A +short
dig @198.51.100.53 home.arpa. SOA +noall +answer

# 配置侧：改配置前先过语法
unbound-checkconf
unbound-control reload
unbound-control list_local_data | grep 'home.arpa'
unbound-control stats | grep 'num.query.opcode.UPDATE'
```

- **为什么这么设**：权威侧和递归侧要**分别**验，两边结果一致才能证明 stub 指对了；`SOA` 用来确认应答来自内部区；`num.query.opcode.UPDATE` 若非零，说明有人把 `UPDATE` 报文打到了 Unbound——它不会应答，这属于链路配置错误，应把写入端改指权威。
- **怎么验证**：`unbound-checkconf && dig @198.51.100.53 gw-01.home.arpa. A +short`
- **失败模式 / 回滚点**：负缓存会把一次 NXDOMAIN 按 SOA minimum 缓存住（第 4、7 章的坑），修好权威后客户端仍可能失败，必要时刷链路上每个实例的缓存；三条路径的通用回滚顺序都是「先停写入端 → 恢复上一版本数据 / 文件 → reload → 两侧 `dig` 比对」。

一个容易忽略的收尾：无论选哪条路径，内部区名都该用 `home.arpa.` 这类不可公网解析的名字；若把内部名字挂在可公开解析的域下，就得额外维护内外两套答案，排障复杂度会明显上升（第 28 章会展开这个故障域问题）。

## 本章要点

- Unbound **不支持 [RFC 2136] 动态更新**：官方 `doc/FEATURES` 写明「not authoritative, so no AXFR, IXFR, NOTIFY or dynamic update services are appropriate」；它只有「非常有限的权威服务」，不存在「Unbound 的 DDNS 配置」这种东西。
- 「非常有限的权威服务」= `local-zone` / `local-data` 静态配置 + `unbound-control local_zone` / `local_data` 运行时增删；**运行时改动不持久化**，重启或 reload 即回到配置文件，必须靠配置管理兜底。
- 微软 DNS 走的是完整 [RFC 2136] 模型：原子 UPDATE + prerequisite、更新落到主服务器（AD 集成区可发给任意持副本的 DC）、**安全动态更新只对 AD 集成区可用且集成后默认「仅安全更新」**、文件型主区默认不开放；流程是 TKEY（Windows 只用 Kerberos）→ TSIG 签名 UPDATE → 按 ACL 写 AD → 签名应答，相关 [RFC 3645] GSS-TSIG。
- 配套要点与坑：`DnsUpdateProxy` / `DHCID` + Name Protection / aging-scavenging **默认关**、no-refresh 与 refresh 各 7 天 / `_msdcs` 与 DC locator / GlobalNames；时钟偏差 > 5 分钟 Kerberos 失败、DHCP 须用低权限专用凭据否则 owner 错乱、反向区容易漏建。2014-06 邮件列表的「域控指向微软 DNS、Unbound 作对外递归验证层」只是**社区观点**。
- 纯 Unbound 架构的三条绕行路径：①外部权威 + DHCP 脚本 `nsupdate` + `stub-zone`；②NSD 权威 + Unbound 递归（`auth-zone` 是只读根镜像，不是可写权威区，见第 6 章）；③静态生成 `local-data` include（第 23 章 `wpad.<内网域>` 即此类）。三者差别集中在实时性、认证、额外组件与回滚点。

## 参考资料

1. `https://github.com/NLnetLabs/unbound` — NLnet Labs / Unbound 官方仓库，仓库内 `doc/FEATURES`（"Very limited authoritative service" 与 RFC 1995 / 1996 / 2136 的原文口径）。
2. `https://unbound.docs.nlnetlabs.nl/en/latest/` — Unbound 官方在线手册入口（`unbound.conf` 的 `local-zone` / `local-data` / `stub-zone` / `auth-zone`，`unbound-control` 的 `local_zone` / `local_data` / `list_local_data`，以及 `num.query.opcode.UPDATE`，按名称检索）。
3. `https://nlnetlabs.nl/documentation/unbound/` — NLnet Labs：Unbound 文档与资料页。
4. `https://www.rfc-editor.org/rfc/rfc2136` — [RFC 2136] *Dynamic Updates in the Domain Name System (DNS UPDATE)*：UPDATE 的原子性、prerequisite 与应答语义。
5. `https://www.rfc-editor.org/rfc/rfc3645` — [RFC 3645]：GSS-TSIG，安全动态更新里 GSS-API 与 TSIG 的衔接。
6. `https://learn.microsoft.com/` — Microsoft Learn，按标题检索《Dynamic DNS Update in Windows and Windows Server》及 secure dynamic update、`DnsUpdateProxy`、DHCID / Name Protection、aging and scavenging、GlobalNames、DC locator / `_msdcs` 等条目。
7. `https://lists.nlnetlabs.nl/` — NLnet Labs 邮件列表归档入口，`[Unbound-users]` 按主题 `Unbound vs MS Resolver` 与 2014-06 月份检索（本章仅作社区观点引用）。
8. `https://www.rfc-editor.org/rfc/rfc5737` — [RFC 5737]：文档用 IPv4 地址块（本章全部示例地址依据）。
