# 递归解析器与本地根区镜像

第 5 章的转发器把所有查询交给上游，本机不做递归。本章走相反方向：让 Unbound 自己做完整递归，并把根区（root zone）复制到本机，做成"本地根区镜像"（hyperlocal root）。这样做的收益是根这一跳不出本机——查询 `www.example.org` 时，根 NS 与胶水记录（glue record）直接从内存里读，不必向根权威服务器发 UDP 包；在跨境链路抖动、根服务器被 QoS、以及需要减少出向元数据的场景里很实用。

代价是这台实例必须有**真实可用的出向 53**：初次 AXFR 拉根区、后续 SOA 探测 / IXFR 更新都要用。这正是本章最容易翻车的地方，6.6 节会单独讲。

实验环境仍是一台家庭网关 + 一台境外云主机，以下为教学用示例地址：IPv4 用 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`（[RFC 5737]），IPv6 用 `2001:db8::/32`（[RFC 3849]），内网写 `192.168.68.0/24`、ULA 写 `fd00::/64`，主机名统一 `gw-01` / `edge-01` / `cloud-01`，不声明是真实环境。**根服务器地址一律写成示意地址**（例如 `198.51.100.53`），实际部署时应换成权威机构公开的根服务器地址，不要照抄本书的文档地址段。

## auth-zone 的四个开关

`auth-zone` 是 Unbound 的"权威区"机制：把一份区数据装进本机，按两个不同阶段参与应答。配根区镜像就是配一个 `name: "."` 的 `auth-zone`。

```yaml
# /etc/unbound/unbound.conf.d/60-auth-zone.conf —— 教学用示意地址
auth-zone:
  name: "."
  primary: 198.51.100.53
  fallback-enabled: yes
  for-downstream: no
  for-upstream: yes
  zonefile: /var/lib/unbound/root.zone
```

**为什么这么设**：四个开关各管一件事，组合起来才叫 hyperlocal。

| 选项 | 默认 | 语义 |
|---|---|---|
| `name: "."` | 必填 | 区名；可配多个 `auth-zone`，按"最接近查询名"的区生效 |
| `primary` | 无 | 用 AXFR / IXFR 从这里下载区副本；可给多条，一条失败就试下一条 |
| `for-downstream` | `yes` | 在 local-zone 之后、缓存之前处理，Unbound 对下游**像权威服务器一样**应答（CNAME 原样返回、不再解析） |
| `for-upstream` | `yes` | 在缓存查询之后、真正出网递归之前处理，递归过程从这里取本地副本 |
| `fallback-enabled` | `no` | 本区查询失败（含 DNSSEC 验证失败）时，退回互联网正常递归 |
| `zonefile` | 无 | 区数据落盘路径；不配则只用内存 |

官方手册对两个阶段的说明值得逐字理解：`for-downstream: yes` 时处于"local-zone 之后、缓存之前"，本实例对区内名字表现为权威服务器，CNAME 记录**原样**回给客户端；`for-upstream: yes` 时处于"缓存查询之后、出网抓取之前"，提供一份权威服务器的本地副本用来加速递归。若两者都开（默认），客户端查根区会得到权威式应答，而内部递归需要根区数据时读本地副本。

本章选 `for-downstream: no` + `for-upstream: yes`，正是官方手册点名的 hyperlocal 组合：客户端与内部查询**都走完整递归流程**，只是根那一跳从本地副本取，CNAME 会被彻底解析而不是原样丢回。好处是下游拿到的是正常递归答案，不会误以为这台机器是根的权威服务器；同时若配了 DNSSEC 验证，本机会先验证区内容再入缓存。

`fallback-enabled: yes` 是可用性兜底：区数据过期、验证失败或 AXFR 全挂时，退回正常的互联网递归而不是全量 SERVFAIL。官方手册明确，区数据到达 SOA expiry 后会过期，过期时查询返回 SERVFAIL，此时若 fallback 开启就改走上游递归。取舍是：开启后镜像失效时仍可解析（但失去"根不出本机"的收益），关闭后故障立刻可见。教学环境建议先开，观察稳定后再按策略收紧。

`primary` 可以写多条，也可以追加 `@端口` 或 `#认证名`（后者用于 AXFR over TLS）；官方手册提醒**不要把 `primary` 写成需要靠这份区数据本身才能解析的主机名**，否则形成循环依赖，用纯 IP 最稳。还有一条容易踩：把 `primary` 指向另一个 Unbound 实例是无效的，因为 Unbound 不提供 AXFR / IXFR 服务；这也是 6.2 节要引入 NSD 的原因。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf
unbound-control list_auth_zones
dig @127.0.0.1 . NS +norecurse +short | head
```

**失败模式 / 回滚点**：`unbound-checkconf` 只验语法，不会验 AXFR 是否真能拉到数据。若 `dig . NS +norecurse` 返回的是空答案或 SERVFAIL，而日志里有区过期 / 传输失败信息，说明镜像没建立起来，`fallback-enabled: yes` 此时正在替你兜底。回滚：删掉整个 `auth-zone` 段并 restart，实例退回普通递归解析器（用内置根提示）。

## 用 NSD 做本地根镜像

Unbound 自己不能对外提供 AXFR，所以本机根镜像的常见形态是：**NSD 持有一份根区 zonefile 并允许本机 AXFR，Unbound 通过 `auth-zone` 从 NSD 拉取**。这一步把 `primary` 从"远端根服务器"换成"本机 NSD"，也让 6.6 节的出向 53 豁免问题更直观。

```yaml
# /etc/nsd/nsd.conf.d/root-mirror.conf （NSD 配置，非 unbound.conf）—— 教学用示意地址
server:
  ip-address: 127.0.0.1@5355
  ip-address: ::1@5355
  do-ip4: yes
  do-ip6: yes
  username: nsd
  zonesdir: /var/lib/nsd
  logfile: /var/log/nsd.log
  verbosity: 1

zone:
  name: "."
  zonefile: root.zone
  provide-xfr: 127.0.0.1 NOKEY
  provide-xfr: ::1 NOKEY
  request-xfr: AXFR 198.51.100.53 NOKEY
```

**为什么这么设**：NSD 是权威服务器（authoritative server），正好补上 Unbound 缺的那一半能力。`zone: name "."` 声明它持有根区；`provide-xfr` 允许本机回环地址来做 AXFR；`request-xfr: AXFR <上游根服务器> NOKEY` 让 NSD 主动从真正的根服务器拉一份根区并保持更新。这里 `<上游根服务器>` 是示意地址，实际部署换成公开根服务器地址；`NOKEY` 表示本例不用 TSIG，若上游要求签名认证则按 NSD 手册配置密钥，密钥一律写 `<TOKEN>` / `***`，不要出现真实凭据。

注意 NSD 与 Unbound 的 uid 不同：这里分别是 `nsd` 与 `unbound`。6.6 节的 nftables 豁免必须**两个 uid 都覆盖**。

Unbound 侧把 `primary` 指向本机 NSD：

```yaml
auth-zone:
  name: "."
  primary: 127.0.0.1@5355
  fallback-enabled: yes
  for-downstream: no
  for-upstream: yes
  zonefile: /var/lib/unbound/root.zone
```

**为什么这么设**：`primary` 是回环地址，Unbound 侧还需要 `do-not-query-localhost: no`（第 5 章讲过适用条件：本机另一实例是设计中的下一跳，且端口不冲突）。这里 5355 是 NSD、Unbound 监听 53 / 5354，pidfile 与缓存目录各自独立，拓扑不成环：NSD 只做权威，不向 Unbound 发查询。

**怎么验证**（NSD 配置用 `nsd-checkconf` 校验，不要用 `unbound-checkconf`）：

```bash
nsd-checkconf /etc/nsd/nsd.conf
dig @127.0.0.1 -p 5355 . SOA +norecurse +short
dig @127.0.0.1 -p 5355 . AXFR +norecurse | head -20
unbound-control stats_noreset | grep -E 'num.queries|num.cachemiss'
```

第二条应回一条根区 SOA；第三条应看到 NS / A / AAAA 胶水记录成批输出——这是 AXFR 真的可用的直接证据。若 AXFR 被拒，检查 `provide-xfr` 与 NSD 实际监听地址是否一致。

**失败模式 / 回滚点**：`request-xfr` 指向的示意地址不可达时，NSD 手里只有空区或旧区，Unbound 拉到的就是空数据。回滚：把 `auth-zone` 的 `primary` 改回直连真实根服务器（6.3 节），或临时撤掉 `auth-zone` 让 Unbound 用内置根提示递归。

## 直连 AXFR 的实测形态

不引入 NSD 也可以：`auth-zone` 的 `primary` 直接写根服务器地址，Unbound 自己发起 AXFR / IXFR。

```yaml
# /etc/unbound/unbound.conf.d/60-auth-zone-direct.conf —— 示意地址
server:
  do-not-query-localhost: no
  edns-buffer-size: 1232
  max-udp-size: 1232

auth-zone:
  name: "."
  primary: 198.51.100.53
  primary: 203.0.113.53
  fallback-enabled: yes
  for-downstream: no
  for-upstream: yes
  zonefile: /var/lib/unbound/root.zone
```

**为什么这么设**：官方手册说 `auth-zone` 可以从 zonefile 读，也可以通过 AXFR / IXFR 保持更新；更新机制按 SOA 计时器工作，先发 **UDP SOA 查询**探测序列号是否变化，变了才做 IXFR / AXFR，更新完成后重写 zonefile。两条 `primary` 在这里是"一条失败就试下一条"（与第 5 章 `forward-addr` 的并发竞速语义不同，不要混淆）。

`edns-buffer-size: 1232` 仍然必要：SOA 探测小，但 AXFR 走 TCP，大应答场景下若之前被人为调成 1472，分片被丢会先卡在超时重试（第 4 章）。`do-not-query-localhost: no` 在直连形态下并非必需，写出来是为了和 6.2 节的本机 NSD 形态共用一份配置模板时少踩坑——实际不需要时应删掉，保持默认 `yes` 更安全。

**直连 AXFR 的实测形态**，按时间顺序大致是这样：

1. 启动时 `zonefile` 不存在或为空 → Unbound 尝试从 `primary` 取区数据。
2. 先向 `primary` 发 UDP/53 的 SOA 查询，比对序列号。
3. 序列号有变化 → 建立 TCP/53 做 IXFR（首次通常直接 AXFR）。
4. 收到完整区数据 → 写入 `zonefile`，后续按 SOA refresh 周期重复第 2 步。
5. 若连续失败直到 SOA expiry → 区过期，查询 SERVFAIL；`fallback-enabled: yes` 时改走互联网递归。

**怎么验证**：

```bash
tcpdump -ni any 'host 198.51.100.53 and (tcp port 53 or udp port 53)' -c 20
ls -l /var/lib/unbound/root.zone
dig @198.51.100.53 . AXFR +norecurse | wc -l
```

抓包里应同时看到 UDP SOA 探测与 TCP 传送；手工 AXFR 的行数能给"完整区有多大"一个基线。若只看到 UDP 没有 TCP，通常是序列号没变（正常）或 TCP/53 被拦（故障）。

**失败模式 / 回滚点**：最典型的失败是 **TCP/53 出向被防火墙 / nftables 吃掉**——UDP SOA 探测能通，AXFR 却永远建不起来，表现为 `root.zone` 一直是空文件或旧时间戳。这正是下一节的主题。回滚：`fallback-enabled: yes` 保证解析不断，撤掉 `auth-zone` 段即可彻底回到内置根提示。

## root-hints 与 auth-zone 的区别

这两个都和"根"有关，但完全不是一回事，混用会产生很难解释的行为。

| 项 | `root-hints` | `auth-zone: name "."` |
|---|---|---|
| 内容 | 一份根 NS 名字与地址的**提示列表**（zone file 格式，仅 NS / A / AAAA） | 一份**完整根区副本**（NS、胶水、DNSKEY、RRSIG、SOA 等） |
| 默认值 | `""`，用内置根提示 | 不存在，需显式配置 |
| 作用 | 告诉递归器"从哪几台根服务器开始 priming" | 让递归器**不出网就能拿到根区数据** |
| 更新方式 | 手工换文件；官方手册提醒内置值可能随根服务器变更而过时 | 按 SOA 计时器 UDP 探测 + IXFR / AXFR 自动更新，重写 zonefile |
| 对外表现 | 普通递归解析器 | `for-downstream: yes` 时像根的权威服务器；`no` 时仍是普通递归 |
| 依赖 | 需要出向 53 做 priming 与后续迭代查询 | 需要出向 53 做 SOA 探测与区域传送（初次尤其） |

一句话记：`root-hints` 是"通讯录"，`auth-zone` 是"把整本通讯录连同内容复印回家"。前者只解决起点问题，后者才让根这一跳落在本地。

**怎么验证**：

```bash
unbound-checkconf -o root-hints
unbound-control lookup example.org | head -20
```

第一项为空说明用内置根提示；第二项能看出递归时选中的名字服务器集合。若两者都配了，以 `auth-zone` 的本地副本优先（`for-upstream: yes` 时它在出网前就被查），`root-hints` 只在本地副本未命中 / fallback 时才起作用。

**失败模式 / 回滚点**：把 `root-hints` 指向一台不可达的根服务器，会让 priming 一直超时；把 `auth-zone` 的 zonefile 当成 root-hints 文件用（后者只该含 NS 与地址）会导致解析异常。回滚时一次只撤一个，别同时改两处。

## zonefile 持久化：重启别把根区弄丢

```yaml
auth-zone:
  name: "."
  primary: 198.51.100.53
  fallback-enabled: yes
  for-downstream: no
  for-upstream: yes
  zonefile: /var/lib/unbound/root.zone
```

**为什么这么设**：官方手册对 `zonefile` 的说明很直白——不给就没有 zonefile；文件不存在或为空时，Unbound 会尝试从 `primary` 重新取区数据，更新完成后**重写该文件**。也就是说：

- 不配 `zonefile`：区数据只在内存里，每次重启都要重新做一遍完整 AXFR。若重启时出向 53 恰好不通，区就建立不起来，只能靠 `fallback-enabled` 兜底。
- 配了 `zonefile`：重启后先从磁盘载入已有副本，立刻可用，再按 SOA 计时器做增量更新。

因此 `zonefile` 不只是"顺手存个档"，它是**冷启动可用性**的一部分。三个工程约束：

1. 路径必须对 `username: unbound` 降权后的进程可写；同时若启用了 `chroot`（第 4 章），路径在 chroot 内解释，要一并搬进去。
2. AppArmor / SELinux 需要放行该路径的读写；第 7 章会讲过滤表大表与 AppArmor 路径白名单的冲突，根区 zonefile 同样受这条限制。
3. 目录要持久（`/var/lib/unbound/`），不要放在 `/run` 或 tmpfs 上——否则"配了 zonefile"和"没配"在重启后是同一回事。

官方手册还有个相关提醒：区过期（SOA expiry 到达）时查询会 SERVFAIL，且此后**即便主服务器发来更旧的序列号也会被接受**，因为本地已经没有可用副本了；此时 `fallback-enabled` 才会切到互联网递归。这意味着 zonefile 的时间戳与 SOA 序列号是排障时最先要看的两个值。

**怎么验证**：

```bash
stat -c '%y %s %U' /var/lib/unbound/root.zone
dig @198.51.100.53 . SOA +norecurse +short | awk '{print $3}'
grep -m1 -A3 '^\.' /var/lib/unbound/root.zone | grep -i serial
sudo -u unbound test -w /var/lib/unbound/root.zone && echo writable
```

**失败模式 / 回滚点**：文件属主是 root 且权限 0644 → Unbound 能读不能写，AXFR 更新后重写失败，日志报错但解析暂时照旧，直到重启暴露问题。`sudo -u unbound test -w` 是这条最便宜的检查。回滚：把 zonefile 移到正确目录并修正属主，或临时撤掉 `zonefile` 行靠 `primary` 每次重拉（接受冷启动代价）。

## 出向 53 必须按 uid 豁免，否则自己吃掉自己

这是本章的核心故障机理，也是全书"排查网络故障先看 nftables 规则"的最典型例子。

策略网关常把 LAN / WG 侧发往 53 的查询劫持进透明代理（`hijack-dns`），让所有客户端的 DNS 强制走本地解析器。规则一般挂在 `output-tproxy` 一类链上。问题在于：**本机解析器进程自己发出的出向 53 也会被这条规则匹配**。Unbound 要向根服务器发 SOA 探测 / AXFR，NSD 要向根服务器发 `request-xfr`，这些包同样从本机 `output` 出去，同样目的端口 53，于是被送回 tproxy，落到别的解析器或代理上——甚至形成环。

结果不是"慢一点"，而是**根区镜像永远不更新**：`root.zone` 时间戳停在初次拉取那一刻，SOA 探测没有真实应答，IXFR 永不触发；NSD 的 `request-xfr` 一直失败，AXFR 建不起来。若同时 `fallback-enabled: no`，客户端最终会看到间歇性全量 SERVFAIL，很容易被误判成"上游根服务器不稳"。

正确做法是在 `output-tproxy` **链首**按 uid 显式 accept，把两个解析器进程的出向查询放过去：

```text
#!/usr/sbin/nft -f
# /etc/nftables.d/10-dns-exempt.conf —— 教学用示意规则
table inet filter {
  chain output-tproxy {
    # 本机解析器自己发出的 53 查询必须豁免，否则被自己的 hijack-dns 吃掉
    meta skuid unbound udp dport 53 accept
    meta skuid unbound tcp dport 53 accept
    meta skuid nsd     udp dport 53 accept
    meta skuid nsd     tcp dport 53 accept

    # 下面才是原本对 LAN / WG 客户端的劫持规则（示意）
    # ip saddr 192.168.68.0/24 udp dport 53 meta mark set 0x2332
    # ...
  }
}
```

**为什么这么设**：豁免必须覆盖 **UDP 与 TCP 两种**、**unbound 与 nsd 两个 uid**。只放 UDP 会导致 SOA 探测通、AXFR 的 TCP 建连失败，正好对应 6.3 节那个"只看到 UDP 没有 TCP"的实测形态；只放 `unbound` 会让 NSD 的 `request-xfr` 被吃掉，本机 NSD 形态（6.2 节）下根区照样不更新。

顺序也关键：豁免规则要放在链首。同类问题上还有个已被验证的坑——`meta skuid 0` 匹配不到内核发出的某些包，必须按实际特征（例如 `udp sport`）显式 accept，否则 WireGuard 自己的回包也会被 `meta mark set 0x2332` 打标送进代理，握手响应到不了对端、隧道起不来。这两件事同源：**本机 / 内核发出的包不能被面向客户端的劫持规则捕获**。

`meta skuid` 匹配的是降权后的实际 uid，所以第 4 章的 `username: unbound` 与 NSD 的 `username: nsd` 必须和这里的规则一致。若你改成 `username: ""`（不降权），这些豁免全部失效——这也是不建议在生产里留空 username 的原因之一。

**怎么验证**：

```bash
nft list chain inet filter output-tproxy
ps -o user=,pid=,comm= -C unbound -C nsd
tcpdump -ni any 'host 198.51.100.53 and (tcp port 53 or udp port 53)' -c 20
stat -c '%y' /var/lib/unbound/root.zone
```

`nft list` 里四条豁免要在劫持规则之前；`ps` 确认 uid 与规则一致；`tcpdump` 应看到真实的出向 53 报文抵达示意根服务器地址（而非被重定向到本机代理端口）；`stat` 的时间戳应在 SOA refresh 周期之后前进。四条一起成立才算镜像真的在更新。

**失败模式 / 回滚点**：改完 nftables 仍不更新，先看规则命中计数（`nft list ruleset -a` 配合 `nft list chain ... -a`），确认豁免规则有没有被匹配；再看是不是 `primary` 用了文档示意地址而实际根本不可达。回滚：`nft flush` 前保留一份可回滚的规则文件，或临时 `nft delete rule` 撤掉新增豁免观察差异；解析业务本身有 `fallback-enabled: yes` 兜底，不会立刻全断。

## 验收与镜像失效演练

把本章各段收成一次可复现的验收。下面的顺序故意从"数据是否存在"查到"链路是否真的出去"，避免一上来就怀疑网络：

```bash
#!/bin/sh
# 本地根区镜像验收 —— 教学用示意地址
set -eu

echo '== 1. 配置语法 =='
unbound-checkconf /etc/unbound/unbound.conf
nsd-checkconf /etc/nsd/nsd.conf

echo '== 2. zonefile 落盘与属主 =='
stat -c '%y %s %U' /var/lib/unbound/root.zone
sudo -u unbound test -w /var/lib/unbound/root.zone && echo 'writable by unbound'

echo '== 3. 本机 AXFR 可用（NSD 形态） =='
dig @127.0.0.1 -p 5355 . AXFR +norecurse | wc -l

echo '== 4. uid 与豁免规则一致 =='
ps -o user=,comm= -C unbound -C nsd
nft list chain inet filter output-tproxy | grep -E 'skuid|accept' | head

echo '== 5. 递归答案正常、根这一跳本地化 =='
dig @127.0.0.1 example.org A +short
unbound-control stats_noreset | grep -E 'total.num.cachemiss|num.recursivereplies'

echo '== 6. 镜像失效演练 =='
# 临时断掉示意根服务器的出向（按实际环境替换），观察 fallback 是否接管
# nft add rule inet filter output ip daddr 198.51.100.53 drop
# dig @127.0.0.1 example.org A +short   # 应仍有答案
# nft flush chain inet filter output    # 恢复
```

**为什么这么设**：第 6 步是刻意保留的注释式演练命令，不是自动执行——断链规则会影响同一台机器上其他业务，必须由人按环境替换地址并确认回滚路径。演练的预期是：`fallback-enabled: yes` 时解析继续可用（但根这一跳重新出网），`fallback-enabled: no` 时会出现 SERVFAIL，让镜像失效可见。两种都是正确行为，取决于你选的可用性策略。

**怎么验证**：演练前后各跑一次 `dig` 与 `unbound-control stats_noreset`，对比 `num.cachemiss` 与 `num.recursivereplies`；恢复后 `stat` 的 zonefile 时间戳应在下一个 SOA refresh 周期后前进。

**失败模式 / 回滚点**：`nft flush chain` 会把整条链清空，若链里还有别的规则必须逐条删除而不是 flush。所有变更的回滚点都是配置文件 + 一份 nftables 备份；根区 zonefile 本身是可重新拉取的派生数据，删掉不会损坏系统。

最后提醒角色边界：本章的镜像仍要求本机有出向 53，因此它**不是**第 5 章转发器的替代品，也不是无网环境下的完整方案。DNSSEC 验证的配置与信任锚放在第三篇第 10 章；QMIN 的参数与兼容坑在第三篇第 13 章；本地根镜像只是让根那一跳不出口，所以 QMIN 的收益落在 TLD 层，这里不展开。

## 本章要点

- `auth-zone: name "."` 的四个开关：`primary` 用 AXFR / IXFR 拉副本，`for-upstream: yes` 让递归从本地副本取根数据，`for-downstream: no` 保持普通递归语义（CNAME 被彻底解析），`fallback-enabled: yes` 在区失效时退回互联网递归。
- `for-downstream: yes` / `for-upstream: yes` 是两个不同阶段：前者在 local-zone 之后、缓存之前，对下游像权威服务器；后者在缓存之后、出网之前；官方手册把 `for-downstream: no` + `for-upstream: yes` 称为 hyperlocal 组合。
- Unbound 不提供 AXFR / IXFR，所以本机根镜像常用 NSD 持有 zonefile 并对回环 `provide-xfr`，再由 NSD 的 `request-xfr` 向真实根服务器拉区；`primary` 也可直连根服务器，实测形态是"UDP SOA 探测序列号 → TCP IXFR/AXFR → 重写 zonefile"。
- `root-hints` 只是根 NS 与地址的提示列表（默认 `""` 用内置值），`auth-zone` 是完整根区副本；前者解决起点，后者才让根这一跳不出本机。
- `zonefile` 必须对 `unbound` uid 可写、位于持久目录、且在 chroot / AppArmor 白名单内；不配则每次重启都要重做完整 AXFR，冷启动时出向不通就没副本可用。
- 本机解析器进程（`unbound` uid、`nsd` uid）的出向 53（**UDP 与 TCP 两种**）必须在 `nft output-tproxy` 链首按 uid 豁免，否则被自己的 `hijack-dns` 吃掉，表现为根区镜像永远不更新、AXFR 失败；排查这类故障先看 nftables 规则。

## 参考资料

- Unbound 官方手册，`unbound.conf(5)` 的 Authority Zone Options（`auth-zone`、`primary`、`fallback-enabled`、`for-upstream`、`for-downstream`、`zonefile`）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound.conf(5)` 的 `root-hints` 与 server options：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- NLnet Labs，NSD 官方配置手册（`nsd.conf` 的 `request-xfr` / `provide-xfr`）：https://nsd.docs.nlnetlabs.nl/en/latest/manpages/nsd.conf.html
- Unbound 官方手册，`unbound-control(8)` 的 `lookup` / `flush_zone` 等命令：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- IANA，根区与根服务器信息入口：https://www.iana.org/domains/root/servers
- RFC 1034，*Domain names - concepts and facilities*（胶水记录与委派）：https://www.rfc-editor.org/rfc/rfc1034
- RFC 5936，*DNS Zone Transfer Protocol (AXFR)*：https://www.rfc-editor.org/rfc/rfc5936
- RFC 1995，*Incremental Zone Transfer in DNS*（IXFR）：https://www.rfc-editor.org/rfc/rfc1995
