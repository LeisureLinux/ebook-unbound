# 第二篇 按角色配置 Unbound {.unnumbered}

# 缓存解析器：最小可用配置

一台家庭网关 + 一台境外云主机的实验环境里，第一件要跑起来的东西不是加密传输，也不是过滤表，而是一台能被 `dig` 问通、能被 `unbound-control stats` 看见的缓存解析器（caching resolver）。本章的所有地址都是教学用示例地址：IPv4 取 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`（[RFC 5737]），IPv6 取 `2001:db8::/32`（[RFC 3849]），内网统一写成 `192.168.68.0/24` 与 ULA `fd00::/64`，不对应任何真实拓扑。

这一章只处理"缓存解析器"这一个角色：它自己做递归、自己维护缓存、对下游表现为一台可用的递归解析器。转发器（forwarder）在第 5 章，本地根区镜像在第 6 章，DNSSEC 验证在第三篇第 10 章，QNAME 最小化（QNAME minimisation，QMIN）的参数在第三篇第 13 章，都不在这里展开。

## 十行起步

先给一台什么都还没配的机器写出最小可用配置。下面这十行是完整文件，不是片段（注释行不计入十行）：

```yaml
# /etc/unbound/unbound.conf.d/10-minimal.conf —— 教学用示例地址
server:
  interface: 127.0.0.1
  interface: ::1
  access-control: 127.0.0.0/8 allow
  access-control: ::1/128 allow
  do-ip4: yes
  do-ip6: yes
  do-udp: yes
  do-tcp: yes
  verbosity: 1
```

**为什么这么设**：`interface` 决定监听哪些地址，官方手册说明"若一条都不给，默认只监听 localhost"；这里显式写出来，是为了后面把它换成 LAN 地址时不会误以为默认就是全网监听。`access-control` 的默认动作是 `refuse`（无匹配时拒绝），且按最长前缀匹配、与语句顺序无关，所以必须显式放行回环，否则连本机 `dig` 都拿不到答案。`do-ip4` / `do-ip6` 同时管"是否应答"与"是否向互联网发出"两个方向；`do-ip6: no` 关掉的是 IPv6 传输，不影响 DNS 报文里照常携带 AAAA 记录内容。`do-udp` / `do-tcp` 都留 `yes`：UDP 是常态，TCP 是 EDNS 报文过大、区域传送与重试时的退路，关掉 TCP 会在大应答场景里直接失败。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf.d/10-minimal.conf && echo OK
```

**失败模式 / 回滚点**：`unbound-checkconf` 只做语法与路径检查，不会告诉你 `access-control` 是否放行、端口是否被占用。若它报错，改回上一版文件即可，配置未被加载，不影响正在运行的实例。若它通过了但 `dig` 拿到 `REFUSED`，问题在 `access-control` 而不是语法，先把源文件地址 `dig -b` 指定成 `127.0.0.1` 再测。

启动与第一次问答：

```bash
systemctl enable --now unbound
dig @127.0.0.1 example.org A +short
```

**为什么这么设**：先用回环地址验证，是为了把"Unbound 本身是否工作"和"防火墙 / 接口地址是否正确"两件事分开。任何一步失败都先回到这一条最小命令，不要在十几个参数里乱猜。

**怎么验证**：`dig` 输出里 `status: NOERROR` 且 `flags:` 带 `ra`（Recursion Available），说明这台实例确实以递归解析器身份应答。若 `flags` 里没有 `ra`，说明问到的不是本实例。

**失败模式 / 回滚点**：`systemctl start` 失败时看 `journalctl -u unbound -n 50`，最常见是 53 端口被 `systemd-resolved` 或其他实例占住。最小配置没有动 `port`，回滚就是 `systemctl stop unbound`，无状态可丢失（缓存为空）。

## 把监听面从回环扩到 LAN

```yaml
# /etc/unbound/unbound.conf.d/20-listen.conf —— 教学用示例地址
server:
  interface: 192.0.2.1
  interface: 2001:db8::1
  interface: 127.0.0.1
  interface: ::1
  access-control: 192.0.2.0/24 allow
  access-control: 2001:db8::/64 allow
  access-control: 127.0.0.0/8 allow
  access-control: ::1/128 allow
  do-ip4: yes
  do-ip6: yes
  do-udp: yes
  do-tcp: yes
```

**为什么这么设**：`interface` 可以重复给多条；`@port` 后缀（如 `192.0.2.1@5353`）用于同一主机多实例，第 7 章专门讲。注意官方手册明确写：**`interface` 的变更在 `kill -HUP` reload 时不生效，只有 restart 才生效**。所以本章凡是改 `interface`，都必须走 `systemctl restart`，`reload` 会留下"配置已改但不监听"的假象。`access-control` 只写允许网段、依赖默认 `refuse` 兜底，比"先 allow 全网再 deny 几条"安全；动作还有 `deny`（静默丢包）、`refuse`（回 REFUSED）、`allow_setrd`、`allow_snoop`、`allow_cookie`、`deny_non_local`、`refuse_non_local`，最小配置只用 `allow`。

`do-ip4` / `do-ip6` 在这里保持 `yes` 是刻意选择：一台双栈网关若关掉 `do-ip6`，Unbound 就不会向互联网的 IPv6 权威服务器发查询，只剩 IPv4 出向；反过来，如果链路上 IPv6 只有 ULA、没有可用出向，就该关掉 `do-ip6` 而保留 AAAA 内容的解析能力，避免每根查询都先等一次 IPv6 超时。这两件事要分开判断，不要因为"客户端没有 IPv6"就顺手关掉 AAAA。

**怎么验证**：

```bash
ss -lunp | awk '$5 ~ /:53$/ {print}'
dig @192.0.2.1 example.org A +short
dig @2001:db8::1 example.org AAAA +short
```

**失败模式 / 回滚点**：`ss` 里看不到新地址，先怀疑是 reload 没生效（上面那条限制），再怀疑地址尚未配到网卡上。回滚：删掉新增 `interface` 行并 `systemctl restart unbound`，回环仍可用。若 `dig` 从 LAN 侧拿到 `REFUSED`，用 `dig -b 192.0.2.1` 指定源地址复核 `access-control` 的最长前缀匹配。

## 降权、chroot 与信息隐藏

```yaml
# /etc/unbound/unbound.conf.d/30-hardening.conf
server:
  username: unbound
  chroot: ""
  hide-identity: yes
  hide-version: yes
  identity: ""
  version: ""
```

**为什么这么设**：`username` 的默认值就是 `unbound`，语义是"绑定端口之后降权"；显式写出来是为了让审计的人一眼看到降权意图，也为了配合第 6 章按 uid 做 nftables 豁免——那里需要知道进程到底跑在哪个 uid 下。`username: ""` 表示不降权，只用于排障，不要留在生产配置里。

`chroot` 需要谨慎：一旦启用，配置文件里所有相对路径都在 chroot 内解释，`include` 相对路径、`pidfile`、`logfile`、密钥文件、根区 zonefile 都要重新对齐；AppArmor / SELinux 的路径白名单也要跟着改（第 7 章会讲过滤表大表与 AppArmor 的冲突）。本章给 `chroot: ""` 是明确选择"暂不启用"，把复杂度留给真正需要它的部署；不要半开半关——写了 chroot 却没把 `tls-cert-bundle`、`auto-trust-anchor-file` 等文件的路径搬进去，是启动即失败的经典原因。

`hide-identity: yes` 让 `id.server` / `hostname.bind` 这类 CHAOS 类查询回 `REFUSED`，`hide-version: yes` 对 `version.server` / `version.bind` 同样处理。两者默认都是 `no`，`identity: ""` 时会返回主机名，`version: ""` 时会返回真实版本串。这是减少暴露面，不是访问控制；真正挡住外部查询的是 `access-control` 与 `interface`。

**怎么验证**：

```bash
dig @127.0.0.1 version.server TXT CHAOS +short
dig @127.0.0.1 id.server TXT CHAOS +short
ps -o user=,pid=,comm= -C unbound
```

**失败模式 / 回滚点**：前两条应为空或 REFUSED；若返回了版本串，说明 `hide-version` 没被加载（检查 include 顺序、是否 reload 了错的实例）。`ps` 应显示 `unbound` 用户；若仍是 `root`，检查是否 `username: ""` 被后面的 include 覆盖。回滚：把 `chroot` 恢复为空串并 restart，降权与隐藏两项可以独立保留。

## EDNS 缓冲区：1232，不要 1472

```yaml
# /etc/unbound/unbound.conf.d/40-edns.conf
server:
  edns-buffer-size: 1232
  max-udp-size: 1232
```

**为什么这么设**：这两个参数方向不同，最容易记反。

| 参数 | 管的方向 | 官方默认值 | 作用 |
|---|---|---|---|
| `edns-buffer-size` | 出向（发给对等方的 UDP 报文里通告的 EDNS 重组缓冲区） | `1232` | 决定上级权威服务器最多能回多大的 UDP 应答 |
| `max-udp-size` | 入向（本实例回给下游客户端的最大 UDP 应答） | `1232` | 限制自己的应答大小，不适用 TCP |

`1232` 是 DNS Flag Day 2020 的推荐值，Unbound 官方手册把它同时作为两个选项的默认值。它的来历是 IPv6 最小 MTU 1280：1280 减去 40 字节 IPv6 首部、8 字节 UDP 首部，得到 1232。于是在 MTU 1280 的链路上（WireGuard、部分 IPv6 隧道）一个 1232 字节的 DNS 应答恰好是不分片的单包。若改成 `1472`，在 MTU 1280 上必然分片；在 PPPoE 的 1492 MTU 上同样会踩到分片边界（1472 是按以太网 1500 减 28 算出来的，PPPoE 少了 8 字节）。IP 分片在不少家宽 / 云链路上被丢弃，结果不是立刻报错，而是"UDP 等超时 → 重试 TCP"，表现为偶发的解析慢、DNSSEC 验证超时、大应答域名随机 SERVFAIL。

**关键区分**：`max-udp-size` 只管入向。上游被你的大缓冲区撑爆的场景，要靠 `edns-buffer-size` 收敛，光调 `max-udp-size` 没用。另外官方手册提醒 `edns-buffer-size` 不要设得高于 `msg-buffer-size`（该值默认 65552），设成 512 虽然能绕过最严苛的 PMTU 问题，但会制造过量 TCP 回退，属于过度矫正。

**怎么验证**：

```bash
dig @127.0.0.1 example.org DNSKEY +dnssec +bufsize=1232 | grep -E 'status|EDNS|UDPsize'
dig @127.0.0.1 +bufsize=4096 example.org A +short
```

第一条看 `EDNS: version: 0, flags:; udp: 1232` 一类输出；第二条验证客户端即便请求 4096，本实例也不会回一个超过 `max-udp-size` 的 UDP 包。更直接的链路级证据是 `tcpdump -ni any 'udp port 53 and (ip[6:2] & 0x1fff) != 0'` 抓分片，正常应无输出。

**失败模式 / 回滚点**：把 `edns-buffer-size` 改成 1472 后若出现"部分域名偶发 2 秒延迟后才有答案"，先怀疑分片被丢，而不是怀疑上游。回滚就是改回 1232 并 reload（这两个参数不需要改 `interface`，reload 生效）。注意 `dig +bufsize=` 是测试工具侧的覆盖，不要拿它当配置值来源。

## 线程与缓存尺寸

```yaml
# /etc/unbound/unbound.conf.d/50-cache.conf
server:
  num-threads: 2
  msg-cache-size: 64m
  rrset-cache-size: 128m
  prefetch: yes
  prefetch-key: yes
  so-rcvbuf: 4m
```

**为什么这么设**：

`num-threads` 默认 1（无线程），官方性能调优页建议设成 CPU 核数。家庭网关 / 小机房通常 2–4 核足够；盲目设成 32 只会让 `msg-cache-slabs` 等自动按 2 的幂调整、锁争用与内存开销一起上升。配合 `so-reuseport: yes` 在支持的内核上能改善多核 UDP 分发，用 `unbound-control status` 可看它是否真的激活。

`msg-cache-size` 与 `rrset-cache-size` 默认都是 `4m`。官方性能页给出的经验值是 **rrset 约为 msg 的两倍**（示例为 `msg-cache-size: 50m` / `rrset-cache-size: 100m`），因为 rrset 缓存存放 RRset 等底层对象、被多条消息共享。这里取 `64m` / `128m` 遵循同一比例，适合一台兼做过滤与网关的机器；官方同时提醒 malloc 开销会让实际内存占用达到配置值的 2 到 2.5 倍，所以别把两项之和直接当成内存预算。缓存尺寸不是越大越好：它决定命中率上限，也决定重启后要重新预热多久。

`prefetch: yes`（默认 `no`）让消息缓存中 TTL 剩余不足 10% 的条目在命中时触发预取，热门条目不会过期；代价是约 10% 的额外流量与负载。`prefetch-key: yes`（默认 `no`）在验证过程中遇到 DS 记录就提前取 DNSKEY，降低首包延迟、多花一点 CPU。这两项都属于"用一点出向流量换稳定延迟"，在没有可用上游的链路上别开。

`so-rcvbuf` 默认 `0`（用系统值），官方建议忙服务器试 `4m`：它设置 53 端口入向 UDP 的 `SO_RCVBUF`，用来吸收短时突发、避免丢包。Linux 上会受 `net.core.rmem_max` 限制，Unbound 需要 root 权限才能绕过；另一个方向 `so-sndbuf` 默认 `4m`，管出向应答的发送缓冲。丢包是否真的发生，看 `netstat -su` 的接收错误计数，不要凭猜测调这个数。

**怎么验证**：

```bash
unbound-control stats_noreset | grep -E 'total.num.cachehits|total.num.cachemiss|num.prefetch|mem.cache'
nproc
sysctl net.core.rmem_max
```

`stats_noreset` 与 `stats` 的区别是"只看不清零"，做基线测量时用它，避免自己把计数读没了（第 7 章展开）。若 `so-rcvbuf: 4m` 未生效，`sysctl net.core.rmem_max` 的值就是线索。

**失败模式 / 回滚点**：`num-threads` 与缓存尺寸都需要 restart 才能完全生效；reload 后看到数字没变不要急着改第二遍。缓存开太大导致内存吃紧时，优先降 `msg-cache-size`，保持 1:2 比例回滚。配置里若把缓存设成 0，`prefetch` / `prefetch-key` 都失去意义，官方手册对此有明确说明。

## serve-expired 全家：先刷新，超时才回过期缓存

术语统一：本书把"用过期缓存应答"统一叫**过期缓存应答（serve-stale）**，配置项则按原样写 `serve-expired*`。这一组参数在 Unbound 里有两种模式，理解它们靠 `serve-expired-client-timeout`：

```yaml
# /etc/unbound/unbound.conf.d/60-serve-expired.conf
server:
  serve-expired: yes
  serve-expired-ttl: 86400
  serve-expired-ttl-reset: yes
  serve-expired-reply-ttl: 30
  serve-expired-client-timeout: 1800
```

**为什么这么设**：

- `serve-expired: yes`（默认 `no`）是总开关，允许用超过 TTL 的旧记录应答。
- `serve-expired-ttl: 86400`（单位**秒**，默认 86400）限制记录过期后还能被服务多久，0 表示不设上限。官方手册援引 [RFC 8767] 的建议区间是 86400（1 天）到 259200（3 天）。
- `serve-expired-ttl-reset: yes`（默认 `no`）在刷新失败后把过期记录的 TTL 重置为 `serve-expired-ttl`，让"一直有人问"的记录能持续被应答。
- `serve-expired-reply-ttl: 30`（单位**秒**，默认 30）是回给客户端的 TTL，且被原始 TTL 封顶；官方手册在启用 client-timeout 时明确建议取 30，依据 [RFC 8767]。给 30 秒的含义是"客户端可以短暂复用，但别长期缓存这个降级答案"。
- **`serve-expired-client-timeout: 1800` 单位是毫秒**，即 **1.8 秒**，不是 1800 秒，也不是 1.8 分钟。语义是：先正常尝试解析，超过 1800 毫秒还没结果，才暂停解析、回过期缓存，之后再继续后台刷新。这是 [RFC 8767] 模式的开关（值大于 0 即启用），默认 1800，Unbound 1.23.0 起 RFC 模式成为默认行为。
- 把它设成 `0` 会切到 Unbound 早期的 `serve-expired` 模式：命中过期记录就**立即**回给客户端，同时后台继续刷新。命中率更高，但客户端更容易拿到较旧的数据；官方性能页建议这种模式下务必配 `prefetch: yes`。

本书选 1800（RFC 模式）：把过期缓存当"上游故障时的最后一道兜底"，而不是平时的加速手段。还要记住它与负缓存（negative caching）的相互作用：上游一次瞬时 NXDOMAIN 会被按 SOA minimum 缓存（常见 3600 秒），这不是 `serve-expired` 能救的；修好权威后客户端仍失败，需要刷缓存（第 7 章）。

**怎么验证**：

```bash
unbound-control stats_noreset | grep -E 'num.expired|num.queries_timed_out|num.recursivereplies'
dig @127.0.0.1 example.org A +short
```

`num.expired` 是"用过期缓存应答了多少次"的计数。做故障演练时，把上游链路临时断开，再问一个刚过 TTL 的名字，应看到 `num.expired` 增长且 `dig` 在约 1.8 秒后返回答案（带 30 秒 TTL）；恢复链路后该计数应停止增长。

**失败模式 / 回滚点**：把 1800 误读成秒、写成 `1800` 以为给了半小时缓冲，实际是 1.8 秒；反过来若写成 `1800000` 想表达 1.8 秒也会让客户端等半小时。回滚：`serve-expired: no` 并 reload 即可关闭整组行为，缓存本身不受影响。另一个坑是"上游慢但没全断"时，1.8 秒到点就回旧数据，业务上若要求强一致性，应改用 `serve-expired-client-timeout: 0` 之外的方式——直接关闭 `serve-expired`，不要靠调大超时硬扛。

## 完整闭环：checkconf → 启动 → dig → stats

把前面各段合起来，形成一次可复现的上线闭环。下面这段脚本假设配置放在 `unbound.conf.d/`、由主配置 `include` 进去；每台机器上路径可能不同，按实际发行版调整。

```bash
#!/bin/sh
# 上线闭环：语法检查 → 启动 → 问答验证 → 统计基线
set -eu
CONF=/etc/unbound/unbound.conf

unbound-checkconf "$CONF" || { echo "配置语法不通过，保持原实例不动" >&2; exit 1; }

systemctl restart unbound
systemctl is-active --quiet unbound

# 1) 基本递归能力
dig @127.0.0.1 example.org A +short >/dev/null
dig @127.0.0.1 example.org A | grep -q 'flags:.* ra' || { echo "未以递归解析器身份应答" >&2; exit 1; }

# 2) TCP 退路可用
dig @127.0.0.1 example.org A +tcp +short >/dev/null

# 3) 信息隐藏生效
test -z "$(dig @127.0.0.1 version.server TXT CHAOS +short)" || echo "注意：version 未被隐藏"

# 4) 统计基线（不清零）
unbound-control stats_noreset | grep -E 'total.num.queries|total.num.cachehits|total.num.cachemiss|num.expired'
```

**为什么这么设**：顺序不能颠倒。`unbound-checkconf` 在最前面，保证语法错误时根本不碰运行中的实例；`restart` 而不是 `reload`，因为 4.2 节改了 `interface`；第 2 步专门测 `+tcp`，因为很多 EDNS / 大应答故障只在 TCP 退路上暴露；第 4 步用 `stats_noreset` 而不是 `stats`，因为 `stats` 默认会把计数清零（受 `statistics-cumulative` 控制），会在"验证"这一步把刚建立的基线抹掉。

要启用 `unbound-control`，主配置里需要 `remote-control:` 段的 `control-enable: yes` 与由 `unbound-control-setup` 生成的自签证书；不启用时这一步会失败，但前 3 步仍然有效。开启与不开的取舍、以及"不开时刷缓存只能重启实例"的代价在第 7 章展开。

**怎么验证**：

```bash
unbound-control status
unbound-control stats | grep -E 'total.num.cachehits|total.num.cachemiss' 
```

`status` 应返回 `alias is up`；`stats` 里 `cachehits` 随重复查询增长、`cachemiss` 只在首次解析 / 缓存过期时增长，说明缓存真的在工作。第一次跑完再连问三次同一个名字，命中数的增量应约等于 3。

**失败模式 / 回滚点**：`unbound-control` 连不上时先查 `control-interface`（默认只听回环）、证书文件权限与 `username` 降权后能否读取密钥，不要首先怀疑网络。整段脚本的回滚点是"保留上一版 `unbound.conf.d/` 目录的副本"：所有变更都是纯配置文件，`systemctl restart unbound` 会重建一个空缓存，不会损坏数据。真正的状态只有缓存，丢弃它是可接受的；如果链路上还有第二层缓存实例，别忘了那一层也要单独确认（第 7 章）。

## 从"能跑"到"可交付"的核对表

| 项 | 期望 | 一条检查命令 |
|---|---|---|
| 语法 | 无错误 | `unbound-checkconf` |
| 监听 | 回环 + 指定 LAN 地址 | `ss -lunp 'sport = :53'` |
| 访问控制 | 允许网段 NOERROR，其余 REFUSED | `dig -b 192.0.2.1 @192.0.2.1 example.org A` |
| 降权 | 进程 uid 为 `unbound` | `ps -o user= -C unbound` |
| 信息隐藏 | CHAOS 查询无版本串 | `dig @127.0.0.1 version.server TXT CHAOS +short` |
| EDNS | 出向 / 入向均 1232 | `dig @127.0.0.1 +bufsize=4096 example.org A` |
| TCP 退路 | `+tcp` 有答案 | `dig @127.0.0.1 example.org A +tcp +short` |
| 缓存 | 重复查询命中数增长 | `unbound-control stats_noreset \| grep cachehits` |
| 过期兜底 | 断链时 `num.expired` 增长 | `unbound-control stats_noreset \| grep expired` |

这张表不是"最佳实践清单"，而是把本章每个配置块后面的验证命令收拢成一次交付前的自查。任何一行不过，回到对应小节看"失败模式 / 回滚点"，不要同时改多个参数。

一个容易忽略的边界：`unbound-checkconf` 检查的是单文件语法与文件路径，不检查 `access-control` 语义、不检查 nftables 是否把 53 劫持到了别处、也不检查上游是否可达。第二篇后续章节会依次补上这三类问题——第 5 章解决"上游是谁"，第 6 章解决"根那一跳从哪来"，第 7 章解决"多实例下日志与缓存怎么看"。

## 本章要点

- 最小可用配置只需 `server:` 段的 `interface`、`access-control`、`do-ip4` / `do-ip6`、`do-udp` / `do-tcp`；`access-control` 无匹配时默认 `refuse`，`interface` 改动只在 restart 生效、reload 不生效。
- `edns-buffer-size` 管出向通告、`max-udp-size` 管入向应答，两者默认均为 1232（DNS Flag Day 2020）；1472 在 MTU 1280 与 PPPoE 1492 上分片，分片被丢会导致等超时重试 TCP。
- `msg-cache-size` 与 `rrset-cache-size` 默认各 4m，建议按 1:2 放大并计入 2–2.5 倍 malloc 开销；`num-threads` 取核数，`prefetch` 用约 10% 额外流量换热门条目不过期，`so-rcvbuf: 4m` 吸收突发丢包。
- `serve-expired-client-timeout: 1800` 单位是**毫秒**（1.8 秒），语义是"先刷新、超时才回过期缓存"；设 0 才切成"命中即回旧数据"的早期模式，`serve-expired-ttl` / `reply-ttl` 是秒。
- 完整闭环为 `unbound-checkconf` → `systemctl restart` → `dig`（含 `+tcp` 与 CHAOS 检查）→ `unbound-control stats_noreset`；统计基线用 `stats_noreset`，避免 `stats` 把计数清零。
- `username: unbound` 在绑定端口后降权；`chroot` 一旦启用，所有相对路径与 AppArmor 白名单一并要重排，未准备好就明确写 `chroot: ""`。

## 参考资料

- Unbound 官方手册，`unbound.conf(5)` 配置项与默认值：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound-checkconf(8)`：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-checkconf.html
- Unbound 官方手册，`unbound-control(8)` 命令与统计计数器：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- Unbound 官方手册，Serving Stale Data（`serve-expired` 与 RFC 8767 两种模式）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/core/serve-stale.html
- Unbound 官方手册，Performance Tuning（线程、缓存比例、`so-reuseport`）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/core/performance.html
- DNS Flag Day 2020，EDNS 缓冲区尺寸建议：https://dnsflagday.net/2020/
- RFC 8767，*Serving Stale Data to Improve DNS Resiliency*：https://www.rfc-editor.org/rfc/rfc8767
