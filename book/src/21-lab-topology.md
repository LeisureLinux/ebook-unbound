# 综合实验：一台网关 + 一台云主机

> **以下为教学用示例地址，均属 RFC 5737 与 2001:db8::/32 文档段**：IPv4 用 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`（[RFC 5737]），IPv6 用 `2001:db8::/32`（[RFC 3849]），ULA 用 `fd00:53::/48`（[RFC 4193]）。`gw-01`、`cloud-01`、`edge-01`、`resolver.example.net`、`dns.example.org` 均为教学代号，不对应任何真实环境。所有 token 一律写作 `<TOKEN>`。

## 拓扑总览：把前四篇拼成一条链

这台实验环境只由两台机器构成：家里的一台网关 `gw-01`，以及一台境外云主机 `cloud-01`。它把第一篇的角色划分、第二篇的多实例配置、第三篇的加固控制项、第四篇的 RFC 依据串成一条可复现的查询路径。

```text
 LAN 客户端（手机 / 笔记本 / Pod）
   │  DNS 指向 fd00:53::1 / fd00:53::2（RDNSS，第 19 章）
   │  或 :53 被 nft tproxy 劫持（第 9 章）
   ▼
┌─────────────────────────── gw-01（家庭网关）───────────────────────────┐
│  LAN: fd00:53::1/64, fd00:53::2/64, 192.0.2.1/24                       │
│                                                                         │
│  [1] nft 策略层：LAN/WG 的 :53 → tproxy；本机解析器出向 53 按 uid 豁免    │
│       │                                                                  │
│       ▼                                                                  │
│  [2] 策略 DNS 实例 A（分流判断，转发器角色，第 5 章）                      │
│       ├─ 国内名字 ──→ 实例 B（:5357 本地转发器 / 明文 53 受控上游）        │
│       └─ 其余名字 ──→ 实例 C（加密上游客户端，第 11、14 章）              │
│                          │                                              │
│  [3] 本地根镜像（auth-zone "."，第 6 章）供实例 D 直接迭代                 │
│  [4] 实例 D（递归 + DNSSEC 验证器，第 10 章）                             │
│                                                                         │
│  [5] 加密隧道（WireGuard，MTU 1280）── 出向 DoT :853                     │
└───────────────────────────────┬───────────────────────────────────────┘
                                │  198.51.100.0/24 文档段上的教学隧道端点
                                ▼
┌────────────────────── cloud-01（云主机）──────────────────────────────┐
│  [6] 递归验证器 Unbound（真正的验证 / QMIN / serve-stale / 负缓存控制）   │
│       │  DoT 上游（fail-closed，第 11、14 章）                           │
│       ▼                                                                 │
│  根 → TLD → 权威服务器（第 3、6 章；第四篇 RFC 依据）                     │
└───────────────────────────────────────────────────────────────────────┘
```

**为什么这么设**：把每个控制项钉在唯一一个组件上（引导归 RA、分流归 A、验证归 `cloud-01`），任何一次故障都能按 21.5 的五个观测点切成互不重叠的一段，而不是在两台机器之间来回猜。

**怎么验证**：`ss -lunp -t | grep -E ':(53|853|5357|5358|5359)\b'` 的输出应与 21.4 监听表逐行一致。

**失败模式 / 回滚点**：把验证与转发堆到同一个实例会重现第 22 章案例 ④；回滚点见 21.7 的分层快照表，顺序先救 DNS 可达性再救策略。

先明确各角色的边界，避免把两台机器上的实例混成一个「大 Unbound」：

| 组件 | 角色（第一篇术语） | 唯一职责 | 不能做什么 |
|---|---|---|---|
| 实例 A | 转发器 | 按域名/标签分流，决定往哪条链路走 | 不做验证、不配信任锚（第 22 章案例 ④） |
| 实例 B | 转发器 | 国内名字的单上游受控转发 | 不配第二个上游（第 22 章案例 ①） |
| 实例 C | 转发器 | 加密上游客户端，fail-closed | 不允许静默明文降级（第 22 章案例 ②） |
| 实例 D | 递归解析器 + 验证器 | 本地根镜像迭代 + DNSSEC 验证 | 与 A 的转发器身份二选一 |
| `cloud-01` Unbound | 递归解析器 + 验证器 | 出向递归、DoT 上游、QMIN、serve-stale | 不负责 LAN 的 DNS 引导 |

「策略 DNS」在本章特指实例 A 做的那件事：**按查询名与来源标签把查询分给不同的下游实例**，它不是 policy routing 意义上的路由表操作，也不解释应答内容。分流规则、监听表、失败注入点见 21.3–21.5。

与第 20 章的关系也顺带说清：如果客户端是集群里的 Pod，把上图的「实例 A + CoreDNS」换成一个组合——CoreDNS 只答 `svc.example.internal`，其余 `forward` 到本图的 `cloud-01`。第 20 章的三种拓扑在这里对应「集群外中央验证器 + DoT 上游」那种。

## 客户端 DNS 指向：引导层只做一件事

客户端侧不需要逐台配置。第 19 章已经把机制写透：网关自己发 RA，在 RDNSS 选项里广告自己的 ULA 地址，并用 `RDNSS lifetime=0` 压掉光猫广告的 DNS。**光猫里不配任何 DNS**。

```text
LAN 客户端最终应看到的 DNS：
  fd00:53::1        # 实例 A 的监听地址
  fd00:53::2        # 备用/第二个实例，仅当确实部署了第二个实例
  （不应出现第三条，尤其不应出现光猫的链路本地地址或 GUA）
```

**为什么这么设**：引导路径上只放 ULA（`fd00:53::/48`，第 19 章），不出现会随运营商轮换的 GUA，也不留给光猫广告的 DNS 任何位置。

**怎么验证**：`adb shell dumpsys connectivity | grep -i -A3 DnsAddresses`，期望只列出这两条。

**失败模式 / 回滚点**：出现第三条说明 RDNSS 的 `lifetime=0` 撤销未被接受，回到第 19 章的退路；回滚用 `/etc/ra-steer/ra.conf.bak`。

对不理 RA 的存根解析器（写死 :53 的物联网设备、某些容器），用第 9 章的 nftables :53 劫持兜底。但必须记住第 22 章案例 ⑥ 那条边界：**本机解析器进程（unbound、nsd）的出向 53 要按 uid 豁免**，否则自己的递归查询被自己的 hijack-dns 吃掉，表现为根区镜像永远不更新。

验收（客户端侧，命令可直接复制）：

```bash
# Android
adb shell dumpsys connectivity | grep -i -A3 DnsAddresses
# Linux
resolvectl dns lan0 2>/dev/null || cat /etc/resolv.conf
# 抓 RA 核对 hop limit 255 / SLLAO / RIO / RDNSS
tcpdump -i lan0 -n -vv 'icmp6 && ip6[40] == 134' -c 2
# 走通一次查询
dig @fd00:53::1 +short dns.example.org A
```

**为什么这么设**：把「引导正确性」和「解析正确性」分成两次验收，前者只看客户端的 DNS 列表与 RA 报文，后者才看应答。

**怎么验证**：`adb shell dumpsys connectivity | grep -i -A3 DnsAddresses` 只列出自己的两条 ULA；`dig @fd00:53::1 +short dns.example.org A` 返回答案。

**失败模式 / 回滚点**：出现第三条 DNS 说明 RDNSS 撤销没被接受，回到第 19 章的退路；`dig` 超时但列表正确，说明问题在网关内部（跳到 21.3 的 nft 计数器），不要在客户端上反复改 `/etc/resolv.conf`。回滚点：停掉 RA 发送进程，用第 19 章的 `/etc/ra-steer/ra.conf.bak` 恢复。

## 分流规则：策略 DNS 只留一个出口

分流表是拓扑级的拼装，具体 `unbound.conf` 的完整配置见第 5、7 章，这里不重复贴配置块。

| 判定顺序 | 匹配条件 | 交给 | 依据 |
|---|---|---|---|
| 1 | 来源 uid 是本机解析器（unbound / nsd） | 直接放行出向 53，不进 tproxy | 第 22 章案例 ⑥ |
| 2 | 查询名命中本地权威区（`example.internal`、`_dns.resolver.arpa.`） | 本地应答 / always_nodata | 第 13 章 DDR 压制 |
| 3 | 命中「国内名字」清单 | 实例 B（单上游受控转发器） | 第 5 章单上游竞速教训 |
| 4 | 其余全部 | 实例 C → 加密隧道 → `cloud-01` | 第 11、14 章 fail-closed |
| 5 | 以上任一步 SERVFAIL | 返回 SERVFAIL，不回退明文 | 第 22 章案例 ② |

三条拓扑级纪律：

- **每个转发器只留一个上游。** 给转发器配第二个 `forward-addr`，Unbound 是「并发竞速取最快」而非主备，查询会被送到不该去的解析器，过滤表失效（第 22 章案例 ①）。切换交给外部巡检脚本改配置 + reload。
- **fail-closed 不是一句口号。** `forward-first: no`、加密上游失败后两波都失败就 SERVFAIL；允许回退到明文 53 会让整条查询流在无人察觉的情况下裸奔（第 22 章案例 ②）。
- **分流清单本身要能回滚。** 清单用单独文件（例如 `/etc/unbound/policy-domains.list`）加载，改动前 `cp -a` 一份，回滚只还原这一个文件，不动实例配置。

```bash
# 拓扑级分流可视性：看每个实例各自收到的查询数
unbound-control -c /etc/unbound/instance-a.conf stats_noreset | grep -E 'num.query|num.cache'
unbound-control -c /etc/unbound/instance-b.conf stats_noreset | grep -E 'num.query|num.cache'
unbound-control -c /etc/unbound/instance-c.conf stats_noreset | grep -E 'num.query|num.cache'
```

**为什么这么设**：三个实例的查询计数能直接证明「分流是否真的按表走」，比在日志里 grep 域名更可靠，也不会把查询内容写进日志（呼应第 7 章 dnstap 保留期那条）。

**怎么验证**：连续查一个国内名字和一个外部名字，A 的 `num.query` 增 2，B 与 C 各增 1。

**失败模式 / 回滚点**：若 A 增 2 而 B、C 都是 0，多半是 A 自己缓存了答案或 tproxy 没把包交进来；按 21.6 的顺序先看 nftables 计数器。回滚点：`/etc/unbound/policy-domains.list.bak` 与三个实例的配置快照。

## 各实例监听表

监听表是整个拓扑最容易写错的一张表：地址重叠、端口撞车、把 ULA 绑到会轮换的 GUA 上，都会造成「部分客户端能用、部分不能」。以下只列拓扑级的地址与端口分工，**完整的 `server:` / `forward-zone:` 配置见第 5、6、7 章**。

| 实例 | 监听地址 | 端口 | 角色 | 上游 / 出向 |
|---|---|---|---|---|
| A 策略 DNS | `fd00:53::1`、`192.0.2.1` | 53 | 转发器（分流） | B / C |
| A' 备用引导 | `fd00:53::2` | 53 | 转发器（同 A 配置，仅地址不同） | B / C |
| B 国内分流 | `127.0.0.1`、`::1` | 5357 | 转发器 | 单上游，受控明文 53 |
| C 加密客户端 | `127.0.0.1`、`::1` | 5358 | 转发器 | WG 隧道 → `cloud-01` DoT :853 |
| D 本地递归 | `127.0.0.1`、`::1` | 5359 | 递归解析器 + 验证器 | 本地根镜像 → 权威（第 6 章） |
| `cloud-01` Unbound | 教学隧道地址 `198.51.100.2`、ULA `fd00:53::53` | 853 (DoT) / 53 (仅隧道内) | 递归解析器 + 验证器 | 外部 DoT 上游 → 根/TLD/权威 |

三点约束：

1. **RDNSS 里只出现 ULA**（`fd00:53::1` / `fd00:53::2`），不出现 `2001:db8::/32` 这类会轮换的 GUA，见第 19 章。
2. **B / C / D 只监听 loopback**，不暴露到 LAN；LAN 上唯一的 :53 入口是 A（加上 nft tproxy 的兜底）。
3. **`cloud-01` 的 53 只在隧道内可达**，公网侧只留 :853；否则它就成了一台开放解析器（第 9 章）。

```bash
# 监听表核对：一次列出本机所有 53/853/535x 监听
ss -lunp -t | grep -E ':(53|853|5357|5358|5359)\b' || true
# ULA 是否真的绑在 LAN 口（不是 GUA）
ip -6 addr show dev lan0 scope global
```

**为什么这么设**：用一张命令就能核对「谁在哪个地址上听」，避免逐个实例翻配置文件；`ip -6 addr` 同时验证 ULA 与 GUA 的分工。

**怎么验证**：`ss -lunp` 输出与上表逐行一致；`ip -6 addr show dev lan0` 里 `fd00:53::1/64` 存在。

**失败模式 / 回滚点**：两个实例绑同一 `127.0.0.1:5357` 时后启动者失败，日志里是 `bind: Address already in use`，现象却是「分流一半失效」。回滚点：四个实例的配置文件快照 + `systemctl stop` 顺序表；改端口前先 `ss -lunp` 确认没有残留进程。

## 失败注入与观测点

「可复现」的关键不是配置写得多全，而是能**主动把某一层打断，并看到预期中的降级行为**。下面这张表就是本实验的注入矩阵，每项都对应前面某一章的结论。

| # | 注入动作 | 预期可观测结果 | 关联章节 / 案例 |
|---|---|---|---|
| 1 | 停掉 `cloud-01` 的 Unbound | C 侧 DoT 失败 → A 返回 SERVFAIL；**不出现明文 53 出向** | 第 5、14 章；案例 ② |
| 2 | 上游权威短暂返回 NXDOMAIN 后恢复 | 应答在 SOA minimum 期内仍是 NXDOMAIN；逐层刷缓存后恢复 | 第 22 章案例 ⑤ |
| 3 | 把某条路径 MTU 改到 1280 以下 | `edns-buffer-size: 1232` 仍不分片；若误设 1472 则出现超时→TCP 重试 | 第 13、17 章；案例 ③ |
| 4 | 断开 `cloud-01` 出向，但保留已缓存记录 | `serve-expired` 返回过期缓存应答，客户端仍有答案 | 第 7 章；注意 `serve-expired-client-timeout` 单位是毫秒 |
| 5 | 打开 `qname-minimisation-strict: yes` | 违反 [RFC 8020] 的 CDN 区 CNAME 整片解析失败；关掉后仍要刷每层缓存 | 第 13 章；案例 ③ |
| 6 | 在 A 上加第二个 `forward-addr` | 查询开始出现在不该去的解析器上，过滤表失效 | 第 5 章；案例 ① |
| 7 | 去掉 nft 的 uid 豁免 | 根区镜像停止更新、AXFR 失败、递归查询被自己劫持 | 第 9 章；案例 ⑥ |
| 8 | 在 `output-tproxy` 链尾打标不排除 WG | WG 握手响应到不了对端，隧道起不来 | 第 14 章；案例 ⑦ |

注入点的物理位置也要钉死，否则「断了但没断对」会浪费大量时间：

```text
观测点 O1：LAN 客户端 → A 之间（tcpdump，看 DNS 列表与 QNAME）
观测点 O2：A → B/C 之间（各实例 stats / dnstap，看分流）
观测点 O3：C → WG 隧道口（tcpdump + nft 计数器，看 mark 与加密封装）
观测点 O4：cloud-01 隧道口（tcpdump :853，看 DoT 与 EDNS 尺寸）
观测点 O5：cloud-01 → 权威之间（dig +dnssec、QMIN 层数、根区 priming）
```

**为什么这么设**：五个观测点把链路切成互不重叠的四段，任何一次故障都能用「哪一段的计数器不动了」定位，而不是靠猜。

**怎么验证**：注入 #1 时，O3 的隧道计数器仍在（C 在重试），O4 无 :853 新流，O1 收到 SERVFAIL；三者齐备才说明 fail-closed 真的生效。

**失败模式 / 回滚点**：注入测试会污染缓存，测完必须按 #2 的顺序逐层刷；所有注入前先 `nft list ruleset > /var/tmp/nft-before-lab.txt`、`cp -a /etc/unbound /var/tmp/unbound-before-lab/`。**排查顺序：先看 nftables，再怀疑别的**——注入 #7、#8 的现象都极易被误判成上游故障。

## 验收命令清单

以下命令按「由近及远、由数据面到语义」排列，全部使用文档段地址与教学域名，可直接复制。它们只检查拓扑是否拼装正确，不重复前面章节的完整配置。

### 1) DNSSEC 与信任锚

```bash
# 好签名域：应答应带 ad 标志（验证器真验证时）
dig @fd00:53::1 +dnssec signed.dns.example.org A
# 坏签名域：应返回 SERVFAIL，而不是照样给答案
dig @fd00:53::1 +dnssec badsig.dns.example.org A
# 验证器侧确认信任锚已 priming（不应有 failed to prime 日志）
unbound-control -c /etc/unbound/instance-d.conf status | grep -i anchor || true
journalctl -u unbound@instance-d --since '10 min ago' | grep -i 'prime\|bogus' || true
```

**为什么这么设**：第 10 章的三种状态（无锚透传 / permissive 记日志放行 / 真验证）用一好一坏两个域就能一次判完；`dig +dnssec` 的 `ad` 标志是「这一跳真的验证了」的证据，而 SERVFAIL 是坏签名被拒的证据。

**怎么验证**：好签名域 `flags:` 含 `ad`；坏签名域 `status: SERVFAIL`。

**失败模式 / 回滚点**：坏签名域返回答案说明信任锚没生效或处于 permissive；**不要在纯转发器（实例 A）上配 `auto-trust-anchor-file`**——信任锚需要自己向根 priming DNSKEY，而 `forward-zone "."` 把一切交给上游，只会得到持续的 `failed to prime trust anchor` 与间歇性全量 SERVFAIL（第 22 章案例 ④）。回滚点：`/var/tmp/unbound-before-lab/` 快照。

### 2) 坏 MTU 路径

```bash
# 在隧道口把 MTU 压到 1280（IPv6 最小 MTU / WireGuard 典型值）
ip link set dev wg0 mtu 1280
# 用大应答触发分片判定；1232 应一次过，1472 会超时后重试 TCP
dig @fd00:53::1 +bufsize=4096 +dnssec dns.example.org DNSKEY | tail -5
# 观察是否出现 TCP 回退
tcpdump -ni wg0 -c 20 'port 53 or port 853' | grep -i -E 'tcp|frag' || true
# 出向通告尺寸：edns-buffer-size（官方默认 1232），max-udp-size 只管入向
unbound-control -c /etc/unbound/instance-d.conf get_option edns-buffer-size
```

**为什么这么设**：把 MTU 与 EDNS 尺寸放在同一组命令里，能直接区分「分片被丢」和「上游超时」这两种长得一模一样的现象。`edns-buffer-size: 1232` 是 DNS Flag Day 2020 的官方默认，`1472` 在 MTU 1280 与 PPPoE 1492 上都会分片。

**怎么验证**：`unbound-control ... get_option edns-buffer-size` 显示 1232（若该 `get_option` 不受支持，直接查配置文件）；抓包里无 IPv6 分片头、无 53/TCP 重试。

**失败模式 / 回滚点**：误设 1472 后不要靠调 `max-udp-size` 补救——它只管入向，出向要靠 `edns-buffer-size`。回滚：`ip link set dev wg0 mtu <原值>`，原值存在 `/var/tmp/nft-before-lab.txt` 旁边的 `ip link show > /var/tmp/link-before-lab.txt` 快照里。

### 3) 上游断开时的过期缓存应答

```bash
# 先预热一个记录
dig @fd00:53::1 +short api.dns.example.org A
# 模拟上游断开（教学：在 cloud-01 侧临时停服务，不真的改公网）
ssh cloud-01 'systemctl stop unbound-upstream' || true
# 立刻再查：应仍得到答案（serve-expired），但可能带过期标记
dig @fd00:53::1 +short api.dns.example.org A
# 注意单位：serve-expired-client-timeout 默认 1800 = 1.8 秒（毫秒），不是 1800 秒
unbound-control -c /etc/unbound/instance-d.conf get_option serve-expired-client-timeout || true
ssh cloud-01 'systemctl start unbound-upstream' || true
```

**为什么这么设**：把「上游断开」和「应答仍在」放在同一个 shell 片段里，直接证明 serve-stale 生效；同时把那个最容易读错的单位写在命令旁边。

**怎么验证**：第二次 `dig` 仍返回地址；`journalctl` 里能看到 `serve expired` 一类记录。

**失败模式 / 回滚点**：若第二次 `dig` 变成 SERVFAIL，检查 `serve-expired: yes` 与 `serve-expired-ttl` 是否被上层（A/C 的 `cache`）提前截断——**链路上每一层的缓存策略都要一致**。回滚：恢复 `cloud-01` 服务并逐层刷缓存。

### 4) nft 计数器与劫持边界

```bash
# 先看规则与计数器（排查顺序：先看 nftables，再怀疑别的）
nft list ruleset | sed -n '1,120p'
nft list chain inet mangle hijack-dns 2>/dev/null || true
nft list chain inet mangle output-tproxy 2>/dev/null || true
# 清零点后发一次查询，看每条规则的 pkts 增量
nft reset counters
dig @fd00:53::1 +short dns.example.org A
nft list ruleset | grep -i 'counter\|packets' | head -20
# uid 豁免是否命中（本机解析器出向 53 不应被 tproxy）
nft list ruleset | grep -i 'skuid' | head -10
```

**为什么这么设**：计数器把「包到底走了哪条链」变成可数的数字，比看日志里的现象更直接；`skuid` 一行专门验证案例 ⑥ 的豁免，链尾 `meta mark set 0x2332` 一行专门验证案例 ⑦ 的 WG 回包是否被误打标。

**怎么验证**：LAN 查询使 hijack-dns 计数增加；本机解析器出向 53 的计数不增加；WG 的 `udp sport 51820` 在 `output-tproxy` 链首有显式 accept。

**失败模式 / 回滚点**：`meta skuid 0` 匹配不到内核发出的 WG 握手响应包，必须按 `udp sport` 显式 accept；改规则前 `nft list ruleset > /etc/nftables.conf.bak`，回滚 `nft -f /etc/nftables.conf.bak`。

### 5) 一张表收口

| 验收项 | 命令（简写） | 通过标准 |
|---|---|---|
| 客户端 DNS 列表 | `adb shell dumpsys connectivity \| grep DnsAddresses` | 只两条 ULA |
| 分流生效 | 三个实例 `stats_noreset` | A+2，B/C 各 +1 |
| DNSSEC 好签名 | `dig +dnssec signed.dns.example.org A` | `ad` 标志 |
| DNSSEC 坏签名 | `dig +dnssec badsig.dns.example.org A` | SERVFAIL |
| 坏 MTU | `dig +bufsize=4096 ... DNSKEY` + 抓包 | 无分片、无 TCP 重试 |
| 上游断开 | `dig +short api.dns.example.org A` 两次 | 第二次仍返回（serve-stale） |
| nft 劫持边界 | `nft list ruleset` + `nft reset counters` | uid 豁免命中，WG 出包未被打标 |
| 隧道加密传输 | `tcpdump -ni wg0 'port 853'` | 只有 :853，无明文 53 出公网 |

## 回滚点汇总与实验纪律

| 层 | 快照 | 回滚动作 | 优先级 |
|---|---|---|---|
| 客户端引导（RA / RDNSS） | `/etc/ra-steer/ra.conf.bak` | 停发送进程、还原配置 | 2 |
| nftables（劫持 / tproxy / mark） | `/etc/nftables.conf.bak` | `nft -f` 还原 | 1 |
| 分流清单 | `policy-domains.list.bak` | 单文件还原 + reload | 1 |
| 四个 Unbound 实例 | `/var/tmp/unbound-before-lab/` | 逐实例还原配置并重启 | 2 |
| 隧道 MTU / 链路 | `ip link show > /var/tmp/link-before-lab.txt` | `ip link set dev wg0 mtu <原值>` | 3 |
| `cloud-01` 上游服务 | `systemctl` 状态快照 | `systemctl start` | 3 |
| 各层缓存 | `dump_cache` 输出（可选） | NodeLocal/CoreDNS/Unbound 逐层 `flush_negative` / `flush_zone` | 4 |

四条实验纪律：

1. **先看 nftables，再怀疑别的**。根区镜像不更新、AXFR 失败、WG 握手不通、上游「看起来全挂」——这四个现象里至少三个的根因在规则表，不在 Unbound 配置。
2. **回滚先救 DNS 可达性，再救策略，最后救缓存**，因为所有远程排障手段都依赖 DNS 能用。
3. **改动前设一个十分钟的自动回滚定时器**（`systemd-run --on-active=10m ...`），确认无误再取消，防止配置错误把自己关在门外。
4. **每次注入测试后逐层刷缓存**，否则前一层缓存的 NXDOMAIN 会让你误判「修复无效」（第 22 章案例 ③、⑤）。

本章只给拓扑级的拼装、监听表、注入矩阵与交叉引用。各实例的完整 `unbound.conf`、DoT 服务端/客户端配置、DNSSEC 信任锚操作、nftables 规则全文、RA 引导脚本，分别见第 5、6、7、9、10、11、13、14、19 章及第四篇对应 RFC 章；故障的教学化展开集中在第 22 章。

## 本章要点

1. 拓扑是一条链：LAN 客户端 → （RDNSS 引导 / nft 劫持）→ 策略 DNS 实例 A → B/C 分流 → 加密隧道 → `cloud-01` 递归验证器 → 权威；本地根镜像与实例 D 提供不依赖上游的迭代路径。
2. 客户端引导只靠网关自己发 RA 广告 ULA RDNSS，**光猫里不配任何 DNS**；监听表里 LAN 上唯一的 :53 入口是 A，B/C/D 只听 loopback，ULA 不随 GUA 轮换。
3. 分流表必须单上游、fail-closed；策略 DNS 用各实例的 `stats_noreset` 计数验证，而不是靠日志猜。
4. 五个观测点 O1–O5 把链路切成四段，八项失败注入逐项验证降级行为；**排查顺序：先看 nftables，再怀疑别的**。
5. 验收命令覆盖 DNSSEC 好/坏签名、坏 MTU 下的 EDNS 1232、上游断开时的 serve-stale、nft 计数器与 uid 豁免，以及「公网侧只有 :853」。
6. 每次注入后按层刷缓存；回滚按「DNS 可达性 → 策略 → 缓存」排序，改动前留快照与自动回滚定时器。

## 参考资料

- RFC 5737, *IPv4 Address Blocks Reserved for Documentation*：<https://www.rfc-editor.org/rfc/rfc5737>
- RFC 3849, *IPv6 Address Prefix Reserved for Documentation*：<https://www.rfc-editor.org/rfc/rfc3849>
- RFC 4193, *Unique Local IPv6 Unicast Addresses*：<https://www.rfc-editor.org/rfc/rfc4193>
- Unbound 官方手册（配置选项与 `unbound-control`）：<https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html>
- Unbound `unbound-control` 手册：<https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html>
- nftables 官方 wiki（计数器与规则集管理）：<https://wiki.nftables.org/wiki-nftables/index.php/Main_Page>
- Kubernetes 官方文档 *Using NodeLocal DNSCache in Kubernetes clusters*（第 20 章拓扑 C 的对应）：<https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/>
- DNS Flag Day 2020（EDNS 缓冲区尺寸建议）：<https://dnsflagday.net/2020/>
