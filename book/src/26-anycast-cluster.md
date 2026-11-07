# 企业级大规模部署：anycast 与集群

把一台 Unbound 扩展成「对外只表现为一个服务地址」的一组节点，难点不在 `num-threads` 调到几，而在：客户端的这一次查询和下一次查询可能落到不同节点，节点之间必须能互相承认对方签过的状态。本章只讲解析层怎么横向扩，落到配置、命令、验收清单与回滚点。

以下为教学用示例地址（RFC 5737 的 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`，IPv6 用 `2001:db8::/32`）；域名 `example.net` / `example.org` / `home.arpa.` 与主机名 `edge-01` 等均为示例，不代表任何真实环境。

## 前提与边界

### Unbound 是递归验证器，不是权威服务器

四章统一的口径在这里是 anycast（任播）的前提：Unbound 是递归验证器（recursive resolver），`local-zone` / `local-data` 只提供「非常有限的权威服务」。因此：

- anycast 扩展的是**递归 / 验证 / 转发这一层**，不是权威数据面；
- Unbound 不做 AXFR / IXFR / NOTIFY、不接受 `UPDATE` 报文（见官方 `FEATURES` 与手册），所以「主辅区同步」「辅升主」这些事不属于 Unbound 节点之间的同步，而是权威面的事；
- 本章的「隐藏主服务器」「一主多辅」落在 NSD / BIND / AD DNS 那一侧，Unbound 通过 `stub-zone` / `forward-zone` 指过去即可。

`auth-zone` 是只读根镜像，不是可写权威区（第 6 章已讲），别把它当成 anycast 集群里「共享区数据」的手段。

### 与相邻章节的边界

- 第 7 章讲一台机器上多实例、缓存与观测，本章讲多台机器对一个地址；`num-threads`、libevent、forked operation、per-thread 缓存只是单机性能基线，本章当作每个节点已调好的既定前提一句带过（参数见官方手册对应章节）。
- 第 20 章讲 Kubernetes 里的服务发现与 NodeLocal DNSCache，本章不进容器编排；
- 第 21 章是「一台网关 + 一台云主机」的实验拓扑，本章是它的生产化放大，不重画那张拓扑。

## 官方手册里的 anycast 事实

这一节只写官方手册写明的事实，不掺本书推断。DNS Cookie 机制的规范编号本章不臆断，按名称在 RFC Editor 检索；下列选项与控制命令的**名称**来自官方手册，参数取值见手册页。

### 同一集合必须共享 cookie-secret

官方手册对 `cookie-secret` 的要求非常直白：**同一 anycast 集合的节点必须共享同一个密钥**，才能互相验证 Server Cookie。原因不是配置项洁癖：客户端第一次查询被 BGP/ECMP 送到 `edge-01`，`edge-01` 用它手里的密钥签发一个 Server Cookie；下一次查询可能被送到 `edge-02`，`edge-02` 要能校验这个 Cookie 是不是自己人签的。密钥不一致，跨节点的 Server Cookie 校验就失败，Cookie 带来的放大攻击缓解随之失效。

```conf
server:
    # 方式一：持久化到文件（推荐，便于配置分发与轮换）
    cookie-secret-file: "/etc/unbound/cookie-secret.key"
    # 方式二：内联。生产环境不要把真实值写进公开配置或版本库：
    # cookie-secret: "<128_BIT_HEX_FROM_SECRET_STORE>"
```

**为什么这么设**：`cookie-secret-file` 把 128 位密钥落到文件，配置分发系统可以把「同一把」密钥幂等地推到每个节点；`cookie-secret` 内联是等价选项，但真实值一旦进版本库就等于泄露。二者都要求全集合一致。

**怎么验证**：`unbound-control status` 逐节点确认实例在线，再比对各节点 `/etc/unbound/cookie-secret.key` 的**校验和是否一致**（`sha256sum /etc/unbound/cookie-secret.key`），并用 `stat -c '%a %U' /etc/unbound/cookie-secret.key` 确认权限（应为 `600`、属主为 unbound）；只比对存在性与权限即可，不要打印真实密钥内容。

**失败模式 / 回滚点**：某个节点漏分发、拿到不同密钥，表现为「客户端换节点后 Cookie 保护消失」。回滚点在分发阶段——在激活新密钥之前，把正确文件重推到全部节点再继续；一旦对外服务，只能按下一节的分阶段流程重新收敛。

> 注意区分：`cookie-secret` 属于 DNS Cookie（报文层的来源校验 / 放大缓解），证书轮换属于 DoT / DoH / DoQ 的**传输加密**，DNSSEC 则是**数据完整性 / 来源认证**。三者不互相替代，不要把这里的密钥滚动和 DNSSEC 信任锚混为一谈。

### cookie-secret-file 与分阶段滚动

官方手册给出的滚动流程是三个 `unbound-control` 命令，顺序不能乱：

| 阶段 | 命令 | 状态 | 目的 |
|---|---|---|---|
| 1 全部 staging | `add_cookie_secret` | 新密钥加入但**不生效** | 先把新密钥摆到所有节点上 |
| 2 全部激活 | `activate_cookie_secret` | 新密钥开始签发 Server Cookie | 统一切换签发方 |
| 3 删旧 | `drop_cookie_secret` | 移除旧密钥 | 收敛到只剩新密钥 |

顺序的约束是：**先全部 staging、再激活；激活满 1 小时后才删旧**。两个理由都来自「anycast 节点不会同一秒更新完」：

- 如果先在一台节点激活、再慢慢推其余节点，客户端完全可能先拿到「新密钥签的 Cookie」，下一跳撞到还在用旧密钥的节点，校验失败；
- 激活之后，仍有一批客户端手里握着**旧密钥签发的** Server Cookie，必须留够窗口让它们自然过期。官方手册给的是 1 小时，别自作主张缩短。

```bash
#!/usr/bin/env bash
# cookie-rotate.sh —— 教学骨架：分阶段滚动 anycast 集合的 DNS Cookie 密钥
# 命令名称与顺序按官方手册；参数细节见 unbound-control 手册页。
set -euo pipefail
NODES=(edge-01 edge-02 edge-03)
NEW_SECRET="<128_BIT_HEX_FROM_SECRET_STORE>"   # 占位，由密钥管理下发，勿入版本库

# 阶段 1：全部节点先把新密钥加入 staging（此时不生效）
for n in "${NODES[@]}"; do
    ssh "ops@${n}" unbound-control add_cookie_secret "\"${NEW_SECRET}\""
done

# 阶段 2：全部节点激活（此后新应答用新密钥签发 Server Cookie）
for n in "${NODES[@]}"; do
    ssh "ops@${n}" unbound-control activate_cookie_secret
done

# 阶段 3：激活满 1 小时、旧 Cookie 已过期，才删旧密钥
sleep 3600
for n in "${NODES[@]}"; do
    ssh "ops@${n}" unbound-control drop_cookie_secret
done
```

**为什么这么设**：把「全部节点一致」当成每一步的门禁，而不是「每台各自走完三步」。逐台走完三步会让集合在几分钟里同时存在新旧两种签发方，正是 anycast 最怕的状态。

**怎么验证**：阶段 2 之后 `for n in edge-01 edge-02 edge-03; do ssh "ops@${n}" unbound-control status; done`，并从客户端连续查询、观察 OPT 伪段的 COOKIE 选项（`dig @192.0.2.53 example.net A +short`，带 Cookie 的方法按名称在 `dig` 手册检索），确认跨节点一致。

**失败模式 / 回滚点**：阶段 1、2 出错都可「暂停、补齐、重来」，因为旧密钥还在服务。真正的**不可逆点**是 `drop_cookie_secret`——一旦删早了，仍持旧 Cookie 的客户端会被拒；此时没有「撤销 drop」的捷径，只能重新 `add_cookie_secret` 把旧密钥加回再激活（见官方手册）。所以 1 小时等待与「删前再确认」是硬回滚点。

### 证书轮换：新旧配置并存的窗口

官方手册提到，TLS 证书轮换期间可用 `tls-additional-port` 以及「客户端 magic」一类机制，让**新旧证书（及对应的客户端识别）在轮换窗口内并存**；理由是 anycast 各节点的配置不会在同一秒更新完，必须允许过渡期两种配置都能服务。具体参数、端口取值与 magic 的定义，按名称在官方手册与 RFC Editor 检索，本章不臆断编号与取值。

工程上的推论（**本书归纳**）：证书轮换和 Cookie 密钥滚动是同一种「集合内暂时不一致」问题，都要按「先铺满、再切换、留足过期窗口、最后收敛」的节奏走，不能按「改一台算一台」的节奏走。

## NIST SP 800-81r3 的部署指导

`NIST SP 800-81r3`（*Secure Domain Name System (DNS) Deployment Guide*）给出的是**控制目标**，不是 Unbound 的配置项。本节只列控制目标名称与含义，**不写条款号、不编控制项编号**；编号与条文细节见官方资料，或按名称在 NIST CSRC 检索。

| 控制目标名称 | 含义 | 在本章的落点 |
|---|---|---|
| 部署专用 DNS 服务器以缩小攻击面 | DNS 主机不同置无关服务；不可行时才与 DHCP 等强相关服务同置 | anycast 节点只跑 Unbound + 路由守护进程，不叠别的服务 |
| 每个区至少一个主 + 多个辅 | 权威数据不单点 | 权威面（NSD / AD DNS），Unbound 不做主辅同步 |
| 至少两台权威在不同网段、不同地理位置 | 网络与地理双重分散 | 与路由宣告的故障域设计对齐 |
| 隐藏主服务器（hidden primary） | 主服务器不对公网宣告，只让辅服务器取区 | 权威面单独设计，不并入 anycast VIP |
| 存根 / 客户端至少配两个递归器 | 客户端侧不要只写一个解析器地址 | 客户端配置层面，见下文对单地址客户端的讨论 |
| anycast 可隐藏单节点故障 | 单个节点宕掉时，路由把流量送到其余节点，客户端不感知 | 本章工程骨架的核心，尤其对**只配一个地址的客户端**重要 |
| 有书面的辅升主流程 | 主不可用时，如何把辅提升为主，要事先写清谁做、怎么做 | 权威面预案，配演练记录 |
| 组件故障告警 | 组件级故障要能被发现，不能只靠用户报障 | `unbound-control` 集中观测 + 路由告警 |
| 对齐 NIST SP 800-53 SC-22 的地理分离讨论 | 至少两台权威分处地理分离的子网 | 只做名称层面的对齐讨论，条款细节见官方资料 |

两点值得展开：

其一，指南把「客户端至少配两个递归器」和「anycast 可隐藏单节点故障」放在一起讲，是因为现实里大量存根解析器（stub resolver）**只配了一个地址**。对这类客户端，anycast 是唯一能让「换一台服务器」这件事不依赖客户端改配置的办法——路由层把同一个地址送到健康的节点，客户端毫无感知。代价是：客户端没有第二个地址可退，因此 anycast 集合本身的健康检查和撤路由必须更可靠。

其二，地理分离讨论对齐到 `NIST SP 800-53` 的 `SC-22`。本章只引用其名称，不写条款号；读者在官方资料里按名称检索即可。本书的立场是：这些控制目标描述的是**目标**，anycast 是达成其中「隐藏单节点故障」的一种手段，不是替代「一主多辅」「异地权威」的捷径。

## 工程骨架（本书归纳）

以下做法是本书对常见部署的**归纳**，不是 Unbound 官方规定，也不是 NIST 条款：anycast 服务地址绑 loopback + BGP/OSPF 宣告、健康检查失败即撤路由、缓存不共享带来的冷缓存抖动、配置与过滤表分发的幂等与版本化、`unbound-control` 集中观测、DNS Cookie 密钥滚动、证书轮换、灰度与回滚、权威面与递归面分开。骨架刻意只用文档地址与示例主机名，便于照抄到实验环境再替换。

### 服务地址绑 loopback

每台节点把服务 VIP 绑在 `lo`，而不是某块物理网卡：网卡抖动不影响 VIP 存在，路由守护进程宣告的是这个 /32、/128 主机路由。

```bash
# 每个 anycast 节点执行（教学用示例地址）
ip addr add 192.0.2.53/32 dev lo
ip addr add 2001:db8::53/128 dev lo
```

**为什么这么设**：VIP 与物理接口解绑，接口 flap 不会让本机服务地址消失；上层宣告的只是「到这个主机路由」的可达性。

**怎么验证**：`ip -4 addr show dev lo | grep 192.0.2.53` 与 `ip -6 addr show dev lo | grep 2001:db8::53`。

**失败模式 / 回滚点**：地址被误配成 /24 会与其他节点冲突；回滚为 `ip addr del 192.0.2.53/32 dev lo`，并确认路由已撤销后再删。

### 路由宣告伪配置

```text
# 路由宣告伪配置（示意，非任何厂商真实语法；具体命令见所用路由守护进程官方资料）
# 节点 underlay：198.51.100.11 (edge-01)、198.51.100.12 (edge-02)
# 服务 VIP：192.0.2.53/32、2001:db8::53/128（各自绑在 loopback）
# 集中观测端：203.0.113.10

router bgp <LOCAL_AS>
  neighbor 198.51.100.1 remote-as <UPSTREAM_AS>
  address-family ipv4 unicast
    network 192.0.2.53/32        # 仅当 anycast-healthcheck.sh 通过时宣告
  address-family ipv6 unicast
    network 2001:db8::53/128     # 同上，条件宣告
# 条件：健康检查失败 -> 撤销对应 network 宣告（即「撤路由」）
# 同一 VIP 被多台节点宣告，上游按 BGP/IGP 选路把流量分摊到健康节点
```

**为什么这么设**：宣告与**健康状态绑定**，而不是「进程起来就一直宣告」，这样单节点故障能自动从路由表消失，正是 anycast 隐藏故障的机制。

**怎么验证**：在上游设备查看 `192.0.2.53/32` 是否只在健康检查通过的节点上出现（命令按所用守护进程检索），客户端侧 `dig @192.0.2.53 example.net A +short +time=1 +tries=1` 应稳定返回。

**失败模式 / 回滚点**：条件宣告没接上健康检查 → 故障节点仍在表里 → 流量黑洞。回滚点是手工撤销该 `network` 宣告，先让流量绕开，再修脚本。

### 健康检查脚本

```bash
#!/usr/bin/env bash
# anycast-healthcheck.sh —— 教学骨架：健康检查失败即撤路由
# 撤/宣告路由的落地命令取决于路由守护进程（FRR / BIRD 等），此处用钩子函数占位。
set -euo pipefail
VIP4="192.0.2.53"
VIP6="2001:db8::53"
SECRET_FILE="/etc/unbound/cookie-secret.key"

withdraw_anycast_route() {
    logger -t anycast-health "withdraw ${VIP4} ${VIP6}"   # 伪实现：替换为真实撤宣告命令
}
announce_anycast_route() {
    logger -t anycast-health "announce ${VIP4} ${VIP6}"   # 伪实现：替换为真实宣告命令
}

# 1) 本地控制通道与进程存活
if ! unbound-control status >/dev/null 2>&1; then
    withdraw_anycast_route; exit 1
fi
# 2) 本机 VIP 上真的能解析（不能只看进程在不在）
if ! dig "@${VIP4}" example.net A +short +time=1 +tries=1 >/dev/null 2>&1; then
    withdraw_anycast_route; exit 1
fi
# 3) 共享密钥文件存在且非空（缺了会导致跨节点 Server Cookie 校验失败）
if [[ ! -s "${SECRET_FILE}" ]]; then
    withdraw_anycast_route; exit 1
fi
announce_anycast_route
```

**为什么这么设**：三层检查分别挡住「进程死了」「进程活着但不解析」「密钥漏分发」三类黑洞；只查进程存活是最常见的假阳性来源。

**怎么验证**：临时停掉 Unbound 后运行脚本，`logger` 里出现 `withdraw`（`journalctl -t anycast-health -n 5`）；恢复后重新出现 `announce`。

**失败模式 / 回滚点**：判定过严（一次 `dig` 抖动就撤）会造成频繁切流、放大冷缓存抖动；回滚为放宽成「连续 N 次失败才撤」，并保留手动 `announce_anycast_route` 入口。

### 单节点组装配置

```conf
server:
    interface: 192.0.2.53
    interface: 2001:db8::53
    # 同一 anycast 集合共享同一把密钥（见上一节）
    cookie-secret-file: "/etc/unbound/cookie-secret.key"
    # 过滤表 / local-data 由配置分发按版本 include，不手工改
    include: "/etc/unbound/conf.d/filter-v2026-11.conf"
    num-threads: 4            # 单机基线，见第 7 章；本章不展开
    access-control: 198.51.100.0/24 allow
    access-control: 2001:db8::/32 allow

remote-control:
    control-enable: yes       # 供 unbound-control 集中观测；控制端口不要对公网开放（第 9 章）

stub-zone:
    name: "home.arpa."
    stub-addr: 198.51.100.53  # 指向内部权威面，不是 Unbound 自己
```

**为什么这么设**：把「服务地址 / 共享密钥 / 过滤表版本 / 观测通道 / 内部权威指向」收在一个文件里，配置分发只需幂等地推这一个版本；`stub-zone` 明确划分递归面与权威面。

**怎么验证**：`unbound-checkconf /etc/unbound/unbound.conf` 与 `unbound-control status`。

**失败模式 / 回滚点**：版本号写错或 include 文件缺失 → reload 失败、节点被健康检查撤路由；回滚为 pin 回上一个版本目录并重放分发（见「幂等与版本化」）。

### 缓存不共享：冷缓存抖动

每个节点的缓存是**各自进程内**的（per-thread 缓存也不跨进程、更不跨主机）。撤掉一台节点的路由后，流量落到另一台节点，那台节点对这批查询是**冷缓存**：上游递归量突然上升、延迟抬高、命中率要等 TTL 爬一段时间才恢复。这是 anycast 最容易被忽略的代价（**本书归纳**）。

缓解办法：新节点上线先预热再宣告路由；开启过期缓存应答（serve-stale）撑过回源抖动；避免健康检查抖动造成频繁切流；灰度期间只切一小部分流量观察命中率。注意别把「缓存不共享」误当成配置 bug——它是无状态横向扩的固有属性。

### 配置与过滤表分发：幂等 + 版本化

`unbound.conf`、`cookie-secret.key`、`local-data` / 过滤表 include 都由配置分发系统推送，要求两点（**本书归纳**）：

- **幂等**：同一版本重复推送结果一致，不产生重复 include、不改文件权限；用临时文件 + 完整性校验 + `install` 原子落盘（第 24 章的原子生成思路可复用）。
- **版本化**：每次推送带版本标签，节点上报「当前版本」，集中端能一眼看出哪台漂移了。

分发顺序也要管：先铺 `cookie-secret` 的 staging（上一节阶段 1），再推配置，最后才激活。第 25 章的静态 `local-data` 生成、第 24 章的过滤表生成，都是这个分发面的输入，不在本章重复。

### unbound-control 集中观测

每个节点开 `unbound-control`（走本地控制通道或受保护的 TLS，不对公网暴露），集中端定期拉 `unbound-control stats`，比对各节点 QPS、SERVFAIL 率、缓存命中率。切流之后若某节点命中率骤降、SERVFAIL 抬升，基本就是冷缓存抖动或配置漂移的信号。组件故障告警（NIST 控制目标之一）就挂在这些指标上，而不是靠用户报障。

### 灰度与回滚

一次只动一台节点：推版本 → 观察指标 → 再动下一台。Cookie 密钥滚动、证书轮换、过滤表更新都按这个节奏。回滚点按「不可逆程度」排序：配置版本可 pin 回上一版；路由可重新宣告；**Cookie 密钥在 `drop_cookie_secret` 之前**都可安全回退，之后只能重新加回旧密钥。每条变更都要写清「在哪一步之前可以无损回滚」。

### 权威面与递归面分开

把两个面拆开是本章的收口（**本书归纳**）：

- 权威面（NSD / BIND / AD DNS）：一主多辅、隐藏主服务器、异地分布、书面的辅升主流程；它的数据同步走区传送，与 Unbound 无关。
- 递归面（本章的 anycast 集合）：无状态、可横向扩、共享 `cookie-secret-file`、各自缓存、靠路由宣告伸缩。
- 两者用 `stub-zone` / `forward-zone` 连接；别把权威区塞进 anycast VIP，也别让 Unbound 承担「主辅同步」。

这样，权威面的地理分离（对齐 `SC-22` 讨论）与递归面的 anycast 高可用，是两个独立可演练的故障域。

## 验收清单

| 项目 | 判定命令 | 通过标准 |
|---|---|---|
| VIP 绑在 loopback | `ip -4 addr show dev lo \| grep 192.0.2.53` | 出现 `/32`，非 `/24` |
| 本机 VIP 可解析 | `dig @192.0.2.53 example.net A +short +time=1 +tries=1` | 稳定返回 A 记录 |
| 密钥全集合一致 | `sha256sum /etc/unbound/cookie-secret.key` 逐节点比对 | 校验和一致、权限 `600` |
| 条件宣告生效 | 上游查看 `192.0.2.53/32` 与 `2001:db8::53/128` | 仅在健康检查通过的节点出现 |
| 撤路由生效 | 手动停健康检查后查上游路由表 | 对应主机路由消失 |
| 跨节点 Server Cookie | 连续查询并观察 OPT 伪段 COOKIE 选项 | 换节点后仍被接受 |
| 证书并存窗口 | 轮换期对两种端点分别握手（方法按官方手册检索） | 新旧端点均可服务 |
| 观测汇聚 | 集中端拉各节点 `unbound-control stats` | 全节点都有数据、无漂移 |
| 两平面分离 | `unbound-checkconf` + 检查 `stub-zone` 指向 | 权威面不在 anycast VIP 内 |
| 回滚演练 | 按下一节逐项演练并留记录 | 有书面回滚点与演练记录 |

## 失败模式与回滚点

| 失败模式 | 现象 | 回滚点 |
|---|---|---|
| 密钥漏分发 / 不一致 | 换节点后 Server Cookie 校验失败、Cookie 保护消失 | 在 `activate_cookie_secret` 之前补齐 staging 重来 |
| 过早 `drop_cookie_secret` | 持旧 Cookie 的客户端被拒 | 只在激活满 1 小时后删；删后只能重新 `add_cookie_secret` 加回旧密钥 |
| 健康检查假阴性 | 误撤路由，其余节点冷缓存抖动 | 放宽为连续 N 次失败才撤，手动 `announce_anycast_route` |
| 健康检查假阳性 | 进程活着但不解析，VIP 成黑洞 | 脚本保留真实 `dig` 校验，补密钥文件存在性检查 |
| 路由未撤干净 | 流量仍打到故障节点 | 上游确认 withdraw，检查条件宣告是否接上健康检查 |
| 证书轮换不同步 | 部分节点新证书、部分旧证书 | pin 回旧证书版本，逐节点重推，留并存窗口 |
| 配置 / 过滤表版本漂移 | 同一名字在不同节点答案不一致 | pin 上一版本、幂等重放分发 |
| 冷缓存抖动 | 切流后上游 QPS 放大、延迟上升 | 预热 + serve-stale + 降低切流频率 |
| 控制端口暴露 | 观测通道变成攻击面 | 收回 `access-control`，控制通道只走受保护网络（第 9 章） |

## 本章要点

- anycast 扩的是 Unbound 的**递归 / 验证层**，不是权威数据面；隐藏主服务器、一主多辅、辅升主属于权威面，Unbound 不做 AXFR / 主辅同步。
- 官方手册要求同一 anycast 集合**共享 `cookie-secret`**，用 `cookie-secret-file` 持久化；滚动必须按 `add_cookie_secret` → `activate_cookie_secret` → `drop_cookie_secret`，先全部 staging、再激活、激活满 1 小时才删旧。
- 证书轮换允许新旧配置并存，因为节点不会同一秒更新完；具体取值按名称在官方手册 / RFC Editor 检索，不臆断编号。
- `NIST SP 800-81r3` 给的是控制目标名称（专用服务器、异地权威、隐藏主、双递归器、anycast 隐藏单节点故障、书面辅升主、组件告警、对齐 `SC-22`），本章只写名称不编条款号。
- 工程上（**本书归纳**）：VIP 绑 loopback + 条件路由宣告、健康检查失败即撤路由、缓存不共享的冷缓存抖动、配置分发幂等版本化、`unbound-control` 集中观测、灰度与明确回滚点、权威面与递归面分开。

## 参考资料

1. `https://unbound.docs.nlnetlabs.nl/en/latest/` — Unbound 官方在线手册入口（含 `unbound.conf`、`unbound-control` 手册页；`cookie-secret`、`cookie-secret-file`、`add_cookie_secret` / `activate_cookie_secret` / `drop_cookie_secret`、`tls-additional-port` 按名称检索）。
2. `https://nlnetlabs.nl/documentation/unbound/` — NLnet Labs：Unbound 文档与资料页。
3. `https://www.rfc-editor.org/rfc/rfc5737` — [RFC 5737]：文档用 IPv4 地址块（本章示例地址依据）。
4. `https://www.rfc-editor.org/rfc/rfc1034` — [RFC 1034]：域名概念与设施（主 / 辅区、区传送的背景）。
5. `https://csrc.nist.gov/` — NIST CSRC：`NIST SP 800-81r3`《Secure Domain Name System (DNS) Deployment Guide》与 `NIST SP 800-53`（按名称检索，`SC-22` 地理分离讨论）。
6. `https://www.rfc-editor.org/` — RFC Editor：DNS Cookie / anycast 相关规范按名称检索（本章不臆断规范编号）。
