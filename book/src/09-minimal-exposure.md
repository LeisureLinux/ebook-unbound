# 最小暴露面与访问控制

`interface: 0.0.0.0` 配一行 `access-control: 0.0.0.0/0 allow`，能让全网立刻问通，也能让这台机器几小时内变成别人的反射放大器。前者是「能用」，后者是「被用」。本章处理两者之间那道边界：谁可以问、从哪个地址问、能问出多大的应答、问完留下多少痕迹。

实验环境是一台家庭网关 + 一台境外云主机，以下为教学用示例地址：IPv4 取 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`（[RFC 5737]），IPv6 取 `2001:db8::/32`，域名用 `example.net` / `example.org`，内网写通用 `192.168.68.0/24` 与 ULA `fd00::/64`，主机名 `gw-01` / `edge-01`，凭据一律 `<TOKEN>`。本章只做暴露面：DNSSEC 验证在第 10 章，加密传输（DoT / DoH）在第 11、12 章，QMIN 与 DDR 压制在第 13 章，nftables 的 `:53` 劫持与按 uid 豁免在第 14 章，都不在这里展开。

## 监听地址逐条列出，不要 `0.0.0.0`

`interface` 决定在哪些本地地址上收包。`0.0.0.0` 与 `::` 的含义是「本机所有地址，包括以后新加的地址」：今天多一个网卡、明天多一条隧道，解析器就自动多一张嘴。

```yaml
# /etc/unbound/unbound.conf.d/10-exposure.conf —— 教学用示例地址
server:
  interface: 127.0.0.1
  interface: ::1
  interface: 192.0.2.1
  interface: 2001:db8::1
  port: 53
  do-ip4: yes
  do-ip6: yes
  do-udp: yes
  do-tcp: yes
```

**为什么这么设**：`interface` 可重复给多条，官方手册说明「一条都不给时默认只监听 localhost」，显式列出是为了让清单可审计。回环留给本机诊断与 `unbound-control`，另两个地址是面向客户端的网卡地址，不写通配。`do-tcp: yes` 必须保留：UDP 是常态，TCP 是大应答与重试的退路，为「少开一个面」关掉 TCP 只会把故障推到超时。`@port` 后缀用于同机多实例（第 7 章），本章不涉及。

**怎么验证**：`ss -lunp 'sport = :53' ; ss -lntp 'sport = :53'` —— 只应看到上述四个地址，不应出现 `0.0.0.0:53` 或 `[::]:53`。

**失败模式 / 回滚点**：官方手册明确写了 `interface` 的变更在 `kill -HUP` reload 时**不生效**、只有 restart 生效，所以「改了配置但 `ss` 没变」先怀疑 reload。地址还没配到网卡上就 restart 会绑定失败。回滚：保留上一版副本，删掉新增行并 `systemctl restart unbound`；回环仍可用，缓存重建是可接受代价。

## `access-control`：默认拒绝、最长前缀匹配、与顺序无关

`interface` 管「收不收这个包」，`access-control` 管「收到之后答不答」，两者不是一回事。

```yaml
# /etc/unbound/unbound.conf.d/20-access.conf —— 教学用示例地址
server:
  access-control: 127.0.0.0/8 allow
  access-control: ::1/128 allow
  access-control: 192.0.2.0/24 allow
  access-control: 192.0.2.9/32 deny
  access-control: 2001:db8::/64 allow
  access-control: 0.0.0.0/0 refuse
  access-control: ::/0 refuse
```

**为什么这么设**：官方手册原文是「使用最具体的网段匹配，若都不匹配则 refuse，因此语句顺序无关紧要」。三条要一起记：① **默认拒绝**，不写 `0.0.0.0/0 refuse` 也生效；② **最长前缀匹配**，`192.0.2.9/32` 比 `192.0.2.0/24` 更具体，因此客户端网段里单独这一台被静默丢弃，哪怕它被写在 allow 段之后也一样；③ **顺序无关**，allow 写在最后不会被前面的 deny 覆盖，这与从 iptables 带来的直觉相反。两条兜底 `refuse` 只是把意图写给人看。`refuse` 回 REFUSED、`deny` 静默丢包；手册提醒 DNS 协议不擅长处理按策略丢弃，丢包会引发重试，故兜底用 `refuse`。`allow` 只给递归查询，非递归查询仅能取本地 `local-data`，动态缓存不会被顺手挖走。

**怎么验证**：`dig -b 192.0.2.1 @192.0.2.1 example.org A +short ; dig @192.0.2.1 example.org A +norecurse` —— 前者有答案且带 `ra`，后者应 REFUSED；从 `203.0.113.9` 这类未放行源问也应 REFUSED。用 `-b` 指定源地址很关键，否则测到的是回环放行规则。

**失败模式 / 回滚点**：客户端拿到 REFUSED 而端口正常，问题一定在 `access-control`；最隐蔽的错是把 allow 段写成 `/32` 却以为写的是 `/24`。回滚：删掉误加的 `deny` 行后 `unbound-control reload`（`access-control` 支持 reload，`interface` 不支持）。

## 不对公网开放递归：放大倍数从哪来

一台对任意源地址都提供递归的解析器叫**开放解析器（open resolver）**。它被别人当免费 DNS 用，也被当反射器打第三方。

机理要讲清「倍数」的来历。DNS over UDP 没有握手、源地址可伪造：攻击者把查询包的源 IP 写成受害者地址发往开放解析器。查询包很小——一个 QNAME 加固定字段，量级几十字节；而解析器要做完整递归并回一个带多条记录、可能还含 authority / additional 段的应答，量级几百到上千字节。**放大倍数就是这两个字节数相除**：60 字节查询换 1200 字节应答，约 20 倍。这不是实测结论，只是把两个字节数相除的示意；真实倍数取决于查询名、记录类型与应答是否被截断，具体数值见官方手册与 NIST SP 800-81r3。

| 环节 | 控制 | 本章位置 |
|---|---|---|
| 谁能触发递归 | `interface` + `access-control` 默认拒绝 | 9.1 / 9.2 |
| 单个应答能有多大 | `minimal-responses`、`max-udp-size: 1232` | 9.7 |
| 单位时间能触发多少 | `ratelimit` / `ip-ratelimit` | 9.7 |
| 包能不能进内核 | 防火墙只放行客户端网段 | 9.9 |

`max-udp-size` 管**入向应答上限**，`edns-buffer-size` 管**出向通告**，两者默认 1232（DNS Flag Day 2020 建议）；参数细节在第 4 章，这里只取「压低放大倍数」这层含义。防火墙是最后一道：即使某天误配成 `0.0.0.0/0 allow`，包过滤仍挡得住公网源。

**怎么验证**：在 `203.0.113.9` 上执行 `dig @192.0.2.1 example.net A +time=2 +tries=1`，期望 `status: REFUSED` 或超时，绝不能是 `NOERROR` 带答案。这是整章最重要的一条断言。

**失败模式 / 回滚点**：若返回答案，说明有更宽的 allow 段或测到了另一个实例。回滚：撤掉最宽的 allow 段、reload，再逐段放回；不要靠「关掉防火墙」排障，那会把暴露面直接打开。相关 BCP 编号与年份见官方手册，不在这里编造。

## 降权、chroot 与 AppArmor 路径白名单

`username` 在绑定 53 端口后降权，`chroot` 把文件视图限制到一棵子树，AppArmor 再从 LSM 层限制可读路径。三层叠加，最容易踩的是路径。

```yaml
# /etc/unbound/unbound.conf.d/30-privilege.conf
server:
  username: unbound
  chroot: ""
  directory: "/etc/unbound"
  hide-identity: yes
  hide-version: yes
  identity: ""
  version: ""
```

**为什么这么设**：`username` 默认就是 `unbound`，显式写出是给审计与第 14 章按 uid 做 nftables 豁免留下依据；`username: ""` 只用于排障。`chroot: ""` 是**明确选择暂不启用**——启用后所有相对路径都在 chroot 内解释，`include`、`pidfile`、`logfile`、密钥与根区 zonefile 要整体重排，还得准备 `dev/urandom` 与 `dev/log` 绑定挂载，AppArmor 白名单也要跟着改；半开半关是启动即失败的经典原因。`hide-identity` / `hide-version` 默认均为 `no`：`identity: ""` 会回主机名、`version: ""` 会回真实版本串，打开后 `id.server` / `hostname.bind` / `version.server` / `version.bind` 这些 CHAOS 查询回 REFUSED。这是**减少信息暴露，不是访问控制**。

**怎么验证**：`dig @127.0.0.1 version.server TXT CHAOS +short ; ps -o user=,pid=,comm= -C unbound` —— 前者为空或 REFUSED，后者显示 `unbound` 而非 `root`。

**失败模式 / 回滚点**：返回版本串通常是后面的 include 把 `hide-version` 覆盖回 `no`，或 reload 了错的实例。回滚：`chroot` 恢复空串并 restart，降权与信息隐藏可独立保留。

```text
# /etc/apparmor.d/local/usr.sbin.unbound —— 路径白名单示意，按本机 profile 合并
  /etc/unbound/               r,
  /etc/unbound/**             r,
  /run/unbound/**             rw,
  # 注意：没有 /var/lib/unbound/** 的授权
```

**为什么这么设**：`unbound-checkconf` 只查语法与文件路径，不查 LSM 是否放行。把过滤表、include 片段或运行时数据挪到 `/var/lib/unbound/` 下而 profile 没授权，现象是启动失败、或 checkconf 通过但配置根本没加载；日志里只有 DENIED，不会有一句「路径不允许」。第 7 章过滤表大表与第 6 章根区 zonefile 落的是同一个坑。

**怎么验证**：`aa-status 2>/dev/null | grep -i unbound ; journalctl -k --since '10 min ago' | grep -i DENIED | tail -5`

**失败模式 / 回滚点**：出现 DENIED 就把文件放回 `/etc/unbound/` 既有目录，或按实际路径补 `r,` 后 `apparmor_parser -r` 重载。回滚：注释掉新增 include 行并 restart，代价是那部分配置不生效，要同步告知使用方。SELinux 机制相同、工具不同，见官方手册对应章节。

## `private-address`：防 DNS rebinding

DNS rebinding 让浏览器先解析到攻击者控制的公网地址，再把同一名字的应答改成你的内网地址；浏览器同源策略按域名判断，页面脚本就成了进入内网的代理。

```yaml
# /etc/unbound/unbound.conf.d/40-private.conf —— 教学用示例网段
server:
  private-address: 10.0.0.0/8
  private-address: 172.16.0.0/12
  private-address: 192.168.0.0/16
  private-address: 169.254.0.0/16
  private-address: fd00::/8
  private-address: fe80::/10
  private-address: ::ffff:0:0/96
  private-domain: "home.example.net."
```

**为什么这么设**：官方手册说明该选项把指定网段从**公网域名的应答**里剔除，覆盖 A / AAAA / SVCB / HTTPS，连 SVCB / HTTPS 里的 `ipv4hint` / `ipv6hint` 参数也检查；默认不启用任何网段。`::ffff:0:0/96` 堵住 IPv4-mapped IPv6 绕过过滤的写法，`private-domain` 给确实要返回内网地址的域名开白名单（自己配置的 `local-data` 默认允许）。手册提示把 `127.0.0.0/8` 加进去会误伤以 `127.*` 作应答码的垃圾邮件黑名单；启用 DNSSEC 验证时含私有地址的应答可能被标为 bogus（第 10 章），需用 `private-domain` 或本地数据例外处理。

**怎么验证**：`unbound-control get_option private-address ; unbound-control list_local_zones | head` —— 确认运行中的实例确实加载了这些网段，而不是只写在文件里。

**失败模式 / 回滚点**：最典型的误判是「内网服务突然解析不到地址」，因为自己的权威应答也返回 `192.168.68.x`，被一并剔除。回滚：给该域名加 `private-domain`，或逐条注释网段后 reload；不要整段关掉。

## `local-zone` 与 [RFC 6761] 特殊用途域

`local-zone` 让解析器在本地给出权威式应答，查询根本不出网；既省流量，也防内网信息泄漏到公网权威服务器。

```yaml
# /etc/unbound/unbound.conf.d/50-special-zones.conf
server:
  local-zone: "test." always_nxdomain
  local-zone: "invalid." always_nxdomain
  local-zone: "example." always_nxdomain
  local-zone: "onion." always_nxdomain
```

**为什么这么设**：[RFC 6761] 把 `test.`、`invalid.`、`localhost.`、`example.` 列为特殊用途域名，本就不该去公网查。官方手册列出的默认区包括 `localhost.`、回环反向区、`home.arpa.`、`resolver.arpa.`、`service.arpa.`、`onion.`、`test.`、`invalid.` 及 AS112 反向区；`onion.` 依据 [RFC 7686]，`home.arpa.` 依据 [RFC 8375]，具体清单以所用版本的官方手册为准。这里用 `always_nxdomain` 覆盖默认 `static`：官方定义是「像 static，但忽略 local data 并返回 NXDOMAIN」，好处是日后有人往该区塞 `local-data` 也绝不出网，代价是丢掉默认 SOA / NS 内容，依赖它的客户端应改回 `static`。要连应答都不给就用 `always_deny`。刻意**不写** `example.net.` / `example.org.`：本书验收命令要用这两个名字做真实解析，写进去等于挡住自己的测试；`home.arpa.` 与 `resolver.arpa.` 留给默认区，后者与 DDR 自动发现相关，压制方法在第 13 章，本章不重复。

**怎么验证**：`dig @127.0.0.1 whatever.test. A ; dig @127.0.0.1 whatever.invalid. A ; dig @127.0.0.1 example.org A +short` —— 前两条 `NXDOMAIN` 且不产生出向递归，第三条仍有正常答案，证明没有误伤。

**失败模式 / 回滚点**：过度压制会挡掉合法内部命名（例如真用了 `*.test` 做测试域）。回滚：把对应行改成 `nodefault` 或删掉再 reload，过滤失效是预期代价。

## 限流与丢弃异常查询

先纠正高频拼写错误：官方选项名是 **`ratelimit`**（无连字符）与 **`ip-ratelimit`**，写成 `rate-limit` 会直接报 unknown option。两者管的不是同一件事：

| 选项 | 统计维度 | 超限动作 | 默认 |
|---|---|---|---|
| `ratelimit` | 发给某目标域的递归查询 qps | 回 SERVFAIL；**缓存命中不受限** | 0（关闭） |
| `ip-ratelimit` | 每客户端 IP 接受的查询 qps | **完全丢弃**，连 SERVFAIL 都没有 | 0（关闭） |
| `ip-ratelimit-cookie` | 带有效 DNS Cookie 的客户端 | 同上，手册建议约为前者十倍 | 0（关闭） |
| `unwanted-reply-threshold` | 每线程累计「不该来的应答」数 | 告警并清空 rrset / message 缓存 | 0（关闭） |

```yaml
# /etc/unbound/unbound.conf.d/60-ratelimit.conf —— 数值需按自己的 qps 基线调
server:
  ratelimit: 1000
  ratelimit-factor: 10
  ip-ratelimit: 50
  ip-ratelimit-cookie: 500
  unwanted-reply-threshold: 10000000
  do-not-query-localhost: yes
  tcp-connection-limit: 192.0.2.0/24 50
  tcp-connection-limit: 2001:db8::/64 50
  deny-any: yes
  harden-short-bufsize: yes
  harden-large-queries: yes
  minimal-responses: yes
  max-udp-size: 1232
  edns-buffer-size: 1232
```

**为什么这么设**：`ratelimit` 挡随机子域洪泛（自身不被压垮、也不把流量灌向受害权威域），`ratelimit-factor: 10` 表示超限后仍放行约 1/10 真实查询以便缓存恢复；手册明确写了配置的转发器不受此限，且这两组选项标为 **experimental**，上面数字只是起点，须按 `stats_noreset` 基线调，不要照抄当结论。`ip-ratelimit` 在查缓存**之前**生效，手册说明用途正是缓解放大攻击；带有效 DNS Cookie 的客户端会绕过它，需限制时用 `ip-ratelimit-cookie`。`unwanted-reply-threshold` 是投毒防线：收到大量并非本实例发出的应答就清缓存（手册建议量级为千万），代价是误触发时命中率掉一截。`do-not-query-localhost: yes` 默认开启，把回环与零网段排除在出向目标外，避免不必要的 unwanted reply 顶高计数。

后半段是「丢弃异常查询」：`deny-any: yes` 对 ANY 查询给空应答，不再触发可能很长的上游应答；`harden-short-bufsize: yes` 忽略异常小的 EDNS 缓冲区声明（默认已是 yes）；`harden-large-queries: yes` 忽略超大查询（默认 **no**，合法的大 TSIG / EDNS 载荷确实存在，开启前确认自己没有这类客户端）；`minimal-responses: yes` 不塞非必需的 authority / additional 段，直接压低 9.3 的放大倍数；两个缓冲区保持 1232，不在本章重复第 4 章的 MTU 分析。`tcp-connection-limit` 限制单网段并发 TCP 连接，同样是实验选项。

**怎么验证**：`unbound-control stats_noreset | grep -E 'num.queries|num.queries_cookie|num.queries_discard_timeout|num.cachehits'` —— 先记空闲基线，再短时并发打一批不同名字，看丢弃类计数是否增长、命中率是否不合比例下降。

**失败模式 / 回滚点**：`ip-ratelimit` 设得低于正常突发量，表现是「偶尔整批超时且日志里什么都没有」——超限是静默丢包，最易误判成网络抖动。回滚：几个值改回 0 并 `unbound-control reload`（手册把 `ratelimit` / `ip-ratelimit` 列为可 reload 项），确认业务恢复后逐项加回；`unwanted-reply-threshold` 触发时缓存被清空是设计行为，不是数据损坏。

## 日志最小化与留存策略

查询日志是查询流在自己门口的副本：客户端地址、QNAME、类型与时序都在里面。加密传输把链路上的观察者请出去之后，日志就成了新的集中观察者。原则是**默认不记、排障时临时开、开了必须有到期删除**。

```yaml
# /etc/unbound/unbound.conf.d/70-logging.conf
server:
  use-syslog: yes
  logfile: ""
  verbosity: 1
  log-queries: no
  log-replies: no
  log-local-actions: no
  log-servfail: yes
  statistics-interval: 0
```

**为什么这么设**：手册说明 `log-queries` / `log-replies` 每查询打一行、会**显著降低**服务速度，默认 no；`verbosity: 1` 只给运行级信息，2 以上开始出现逐查询细节；`statistics-interval: 0` 关闭周期统计打印。唯一保留的例外是 `log-servfail: yes`：手册说它单独打印 SERVFAIL 原因，量小、级别是 error，排障价值远高于画像风险。`logfile: ""` 与 `use-syslog: yes` 显式化，避免日志落两份（手册说明两者互相覆盖）。全量结构化日志用 dnstap 时是另一套配置（第 7、13 章），本书统一 30 天保留期并配套到期删除。

**怎么验证**：`unbound-control get_option log-queries ; journalctl -u unbound --since '5 min ago' | grep -c 'query:' || true` —— 前者为 `no`、后者为 0；随后制造一次 SERVFAIL，应能看到原因行。

**失败模式 / 回滚点**：临时用 `set_option verbosity 3` / `set_option log-queries yes` 排障后忘记关，是最常见的破功方式，且要 reload 才回到配置文件的值。回滚：`set_option log-queries no` 立即生效；已落盘的历史日志不会因关掉采集而消失，必须靠下面的策略。

```conf
# /etc/systemd/journald.conf.d/30-dns-retention.conf —— 30 天留存
[Journal]
Storage=persistent
SystemMaxUse=512M
MaxRetentionSec=30day
```

**为什么这么设**：`MaxRetentionSec` 把「30 天」变成系统执行而非口头承诺，`SystemMaxUse` 给磁盘设上限，避免日志写满根分区把解析器一起拖挂。若发行版用 rsyslog 落文件，等价做法见官方手册的 logrotate 配置（`daily` / `rotate 30` / `compress`），二者选一。

**怎么验证**：`grep -R '^MaxRetentionSec\|^SystemMaxUse' /etc/systemd/journald.conf.d/ ; journalctl --disk-usage`

**失败模式 / 回滚点**：改完不 `systemctl restart systemd-journald` 就不生效；`SystemMaxUse` 过小会让日志在保留期到期前被清掉。回滚：删掉 drop-in 并重启 journald，同时确认备份里没有无限期保存的查询日志副本。

## 防火墙只放行必要端口：nftables 基础写法

`access-control` 是应用层的门，防火墙是内核层的门；两道都要有，一次误配才不至于直接变成公网暴露。

```conf
#!/usr/sbin/nft -f
# /etc/nftables.conf —— 教学用示例地址，勿直接用于生产
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority filter; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif lo accept
    # 管理口：只从管理网段进 SSH（本机唯一额外放行的端口）
    ip saddr 198.51.100.0/24 tcp dport 22 ct state new accept
    ip saddr 192.0.2.0/24 udp dport 53 ct state new accept   # DNS，只从客户端网段进
    ip saddr 192.0.2.0/24 tcp dport 53 ct state new accept
    ip6 saddr 2001:db8::/64 udp dport 53 ct state new accept
    ip6 saddr 2001:db8::/64 tcp dport 53 ct state new accept
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, packet-too-big } accept
    icmp type { destination-unreachable, time-exceeded } accept
  }
  chain output {
    type filter hook output priority filter; policy accept;  # 递归出向 53 需留路
  }
}
```

**为什么这么设**：`policy drop` 默认拒绝，配 `ct state established,related accept` 让应答回来，`ct state new` 限定只有新建连接需显式放行。53 同时开 UDP 与 TCP，对应 9.1 的 `do-udp` / `do-tcp`。IPv6 ND 与 `packet-too-big` 必须留：全丢掉表现为「v4 正常、v6 时通时断」，常被误判成上游问题——这是本书反复强调「排查网络故障先看 nftables 规则」的原因之一。出向暂用 `accept` 以免把本章变成出口策略章；收紧出向时记得本机解析器自己的递归查询也要留路，若网关还有 `:53` 劫持规则，本机 Unbound 的出向 53 必须按 uid 豁免（第 14 章）。

**怎么验证**：`nft -c -f /etc/nftables.conf && nft list ruleset | head -30` —— `-c` 只查语法、不加载；加载后从 `203.0.113.9` 测 `dig @192.0.2.1 example.net A +time=2 +tries=1` 应超时，从 `192.0.2.0/24` 内应有答案。

**失败模式 / 回滚点**：在唯一一条远程 SSH 会话里直接 `nft -f` 一个 `policy drop` 的文件，可能把自己锁在外面——`-c` 查语法，不查「你会不会被锁」。操作前先 `nft list ruleset > /etc/nftables.conf.bak`，最好有带外控制台。回滚：`nft -f /etc/nftables.conf.bak`；`nft -f` 重建表会清掉运行时 `nft add element` 加的集合元素与计数器（第 14 章），所以备份必须是完整 ruleset 而非几行命令。

## 完整可复制的加固基线

把前九节合成**一个文件**。假设客户端在 `192.0.2.0/24` 与 `2001:db8::/64`，管理主机在 `198.51.100.0/24`，本机监听 `192.0.2.1` / `2001:db8::1`，走普通 53 端口、不做加密传输、不做 DNSSEC 验证。

```yaml
# /etc/unbound/unbound.conf.d/90-minimal-exposure.conf
# 完整基线 —— 教学用示例地址，上线前按自己的网段替换
server:
  # 9.1 监听面 / 9.2 默认拒绝 + 最长前缀匹配
  interface: 127.0.0.1
  interface: ::1
  interface: 192.0.2.1
  interface: 2001:db8::1
  port: 53
  do-ip4: yes
  do-ip6: yes
  do-udp: yes
  do-tcp: yes
  access-control: 127.0.0.0/8 allow
  access-control: ::1/128 allow
  access-control: 192.0.2.0/24 allow
  access-control: 192.0.2.9/32 deny
  access-control: 2001:db8::/64 allow
  access-control: 0.0.0.0/0 refuse
  access-control: ::/0 refuse
  # 9.4 降权与信息隐藏 / 9.5 防 DNS rebinding
  username: unbound
  chroot: ""
  directory: "/etc/unbound"
  hide-identity: yes
  hide-version: yes
  identity: ""
  version: ""
  private-address: 10.0.0.0/8
  private-address: 172.16.0.0/12
  private-address: 192.168.0.0/16
  private-address: 169.254.0.0/16
  private-address: fd00::/8
  private-address: fe80::/10
  private-address: ::ffff:0:0/96
  private-domain: "home.example.net."
  # 9.6 特殊用途域不出网 / 9.7 限流与异常查询
  local-zone: "test." always_nxdomain
  local-zone: "invalid." always_nxdomain
  local-zone: "example." always_nxdomain
  local-zone: "onion." always_nxdomain
  ratelimit: 1000
  ratelimit-factor: 10
  ip-ratelimit: 50
  ip-ratelimit-cookie: 500
  unwanted-reply-threshold: 10000000
  do-not-query-localhost: yes
  tcp-connection-limit: 192.0.2.0/24 50
  tcp-connection-limit: 2001:db8::/64 50
  deny-any: yes
  harden-short-bufsize: yes
  harden-large-queries: yes
  minimal-responses: yes
  max-udp-size: 1232
  edns-buffer-size: 1232
  # 9.8 日志最小化
  use-syslog: yes
  logfile: ""
  verbosity: 1
  log-queries: no
  log-replies: no
  log-local-actions: no
  log-servfail: yes
  statistics-interval: 0
```

**为什么这么设**：所有条目都在前面各节给过来历。两个刻意的缺席要说明：没有 `auto-trust-anchor-file` / `module-config`——DNSSEC 验证是第 10 章，本章不半开半关；没有 `tls-service-key` / `tls-service-pem`、也没有 853 或 443 监听——加密传输是第 11、12 章。提前塞进来会让文件看起来更「全」，却引入本章给不出验证命令的状态。

**怎么验证**：`unbound-checkconf /etc/unbound/unbound.conf.d/90-minimal-exposure.conf && echo CONFIG_OK`

**失败模式 / 回滚点**：`unbound-checkconf` 只验语法与路径，不验 `access-control` 语义、不验 AppArmor 放行、不验防火墙丢包；通过不等于基线生效。上线顺序固定为「先备份旧 `unbound.conf.d/` 与 ruleset → checkconf → `systemctl restart unbound`（改了 `interface`，reload 不生效）→ 跑 9.11」，回滚见 9.12。

## 逐条验证命令

按序号跑，任何一条不过就停在原地，不要同时改多个参数。

```bash
# 0) 备份（必须先做）；1) 语法
cp -a /etc/unbound/unbound.conf.d /root/unbound.conf.d.bak.$(date +%F)
nft list ruleset > /etc/nftables.conf.bak
unbound-checkconf /etc/unbound/unbound.conf.d/90-minimal-exposure.conf
# 2) 监听面：不应出现 0.0.0.0 或 [::]
ss -lunp 'sport = :53' ; ss -lntp 'sport = :53'
# 3) 放行源可递归（带 ra）；5) 非递归查询不能挖缓存
dig -b 192.0.2.1 @192.0.2.1 example.org A +short
dig @192.0.2.1 example.org A +norecurse
# 4) 公网源不可递归（在 203.0.113.9 上跑，期望 REFUSED 或超时）
dig @192.0.2.1 example.net A +time=2 +tries=1
# 6) 信息隐藏 + 降权；7) 特殊用途域本地终结且不误伤测试域名
dig @127.0.0.1 version.server TXT CHAOS +short ; ps -o user=,comm= -C unbound
dig @127.0.0.1 whatever.test. A ; dig @127.0.0.1 example.org A +short
nft -c -f /etc/nftables.conf ; nft list chain inet filter input -a  # 8) 防火墙语法与计数
# 9) 日志最小化与留存
unbound-control get_option log-queries ; grep -R '^MaxRetentionSec' /etc/systemd/journald.conf.d/
unbound-control stats_noreset | grep -E 'total.num.queries|num.cachehits|num.queries_discard_timeout'  # 10) 统计基线，不清零
```

**为什么这么设**：顺序是「配置 → 内核 → 应用 → 行为」，先排除语法与端口，再问递归，最后看统计；反过来（先看统计）容易把 REFUSED 当成缓存未命中。第 4 条必须从**未放行的源地址**执行，在本机跑会命中回环 allow，得到假阳性。

**怎么验证**：第 3 条有答案且带 `ra`，第 4 条 REFUSED / 超时，第 6 条 CHAOS 为空且进程用户是 `unbound`，第 7 条前空后有，第 9 条 `log-queries` 为 `no` 且看得到 30 天配置，即视为基线生效。

**失败模式 / 回滚点**：`unbound-control` 连不上先查 `control-interface` 与证书权限，不要据此判定实例挂了；`nft -c` 通过但第 4 条仍返回答案，说明最宽的那条 allow 还在。逐条期望值就是验收表，失败时回到对应小节的回滚点。

## 出向收敛：把 DNS 隧道数据外泄堵在边界上

DNS 隧道是加固中最常见的通病之一：防火墙放行了 UDP/53，却没管**谁能发、发给谁**。攻击者在内网主机上把凭据或配置编码进子域标签（`base64chunk.exfil.example.net`），查询一路经过内部解析器到达其控制的权威服务器；应答用 TXT / NULL / CNAME 回传，就形成一条双向 C2 与外泄信道。吞吐很低也够用，而 DGA 轮换域名让「封几个坏域名」必然失效。第 29 章会展开检测与排查，这里只给加固动作。

```bash
# 教学骨架：客户端只能问内部解析器，内部解析器只能问受控上游
# 示例地址取自 RFC 5737 文档段
nft add rule inet filter output ip daddr != 192.0.2.53 udp dport 53 counter drop
nft add rule inet filter output ip daddr != 192.0.2.53 tcp dport 53 counter drop
```

**为什么这么设**：把「谁能发 53」收敛到一台自己可控的解析器，外泄信道的最后一跳就消失；同时保留内部解析器向受控上游的例外。若上游用 DoT，边界上还要放行 853 给指定地址，并在解析器侧写死 `forward-addr`。

**怎么验证**：从一台客户端 `dig @198.51.100.9 example.org` 应超时；`dig @192.0.2.53 example.org` 应正常；nft counter 应随被丢弃的直连查询增长。再用 dnstap 看是否还有高熵长标签流向非常规域。

**失败模式 / 回滚点**：最容易误伤的是容器/kubelet 自带的 DNS、以及 IoT 设备硬编码的公共解析器。先只 `counter` 观察一周再 `drop`；回滚就是删掉这两条规则。**注意**：这一层只防「客户端绕过内部解析器」，不防内部解析器自己被滥用 —— 那要靠第 29 章的速率与唯一 FQDN 检测。

## 回滚点

| 变更 | 生效方式 | 回滚动作 | 回滚代价 |
|---|---|---|---|
| `interface` / `port` | **仅 restart** | 恢复 `unbound.conf.d.bak.*`，`systemctl restart unbound` | 缓存重建，可接受 |
| `access-control`、`ratelimit`、`harden-*` | reload 可生效 | 撤掉误配的行，`unbound-control reload` | 无状态损失 |
| `private-address` / `local-zone` | reload 可生效 | 逐条加 `private-domain` 或删区行 | 该域保护暂时失效 |
| AppArmor profile | `apparmor_parser -r` | 文件移回 `/etc/unbound/**` 或补规则 | 部分配置不加载 |
| nftables ruleset | `nft -f` | `nft -f /etc/nftables.conf.bak` | 运行时集合 / 计数被清空 |
| journald 留存 | 重启 journald | 删 drop-in 并重启 | 历史日志不会自动消失 |

统一顺序：**先恢复配置，再 restart，最后重跑 9.11**。有一类故障不该靠回滚解决——若第 4 条（公网源不可递归）失败，回滚到「更宽松的旧配置」只会把暴露面重新打开；正确方向是收紧到 REFUSED 为止。另有一条跨层约束：`unbound-checkconf` 从不告诉你防火墙与 AppArmor 的判决，所以「配置全对但不生效」时按 `nft list ruleset` → `journalctl -k | grep DENIED` → `access-control` 的顺序查，而不是先怀疑上游。

## 本章要点

- `interface` 只列必要地址，绝不写 `0.0.0.0` / `::`；它的变更 reload 不生效、只有 restart 生效。`access-control` 无匹配即 `refuse`，按**最长前缀**匹配且**与语句顺序无关**，兜底写 `refuse` 比 `deny` 更协议友好。
- 开放解析器能放大攻击，是因为 UDP 源地址可伪造、而「几十字节的小查询」能换回「上千字节的大应答」，放大倍数就是这两个字节数之比；默认拒绝递归、`minimal-responses` / 1232 压应答、`ratelimit` / `ip-ratelimit` 压速率、防火墙兜底，四道缺一不可。
- `username` 降权、`hide-identity` / `hide-version` 隐藏 CHAOS 信息、`chroot: ""` 明确暂不启用，三者互相独立；AppArmor 只允许读 `/etc/unbound/**` 时，把文件放到 `/var/lib/unbound/` 会被 DENIED，而 `unbound-checkconf` 照样通过。
- `private-address` 剔除公网应答里的内网地址（含 SVCB / HTTPS 的 hint）防 DNS rebinding，用 `private-domain` 开白名单；`local-zone` 让 [RFC 6761] 特殊用途域本地终结，别把本书验收用的 `example.net` / `example.org` 也写进去。
- 官方选项名是 `ratelimit` / `ip-ratelimit`（无连字符）且标为 experimental：前者按目标域回 SERVFAIL、缓存命中不受限，后者按源 IP 静默丢弃；`unwanted-reply-threshold` 触发时清缓存是设计行为。日志默认 `verbosity: 1` + `log-queries: no`，只留 `log-servfail`，且必须有 30 天到期删除。
- 基线刻意不含信任锚与 TLS 监听：DNSSEC 在第 10 章、DoT / DoH 在第 11、12 章。所有变更都要「先备份 → checkconf → restart → 逐条验证」，且防火墙与 AppArmor 的判决不在 `unbound-checkconf` 检查范围内。

## 参考资料

- Unbound 官方手册，`unbound.conf(5)`（`interface`、`access-control`、`private-address`、`local-zone`、`ratelimit`、`ip-ratelimit`、`unwanted-reply-threshold` 与默认值）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound-checkconf(8)`（检查范围与用法）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-checkconf.html
- Unbound 官方手册，`unbound-control(8)`（`get_option` / `set_option` / `stats_noreset` / `list_local_zones`）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- NLnet Labs，Unbound 项目文档入口（版本相关的默认区清单与 AppArmor 说明）：https://nlnetlabs.nl/documentation/unbound/
- NIST SP 800-81r3《Secure Domain Name System (DNS) Deployment Guide》官方出版物页面（开放解析器与部署基线）：https://csrc.nist.gov/pubs/sp/800/81/r3/final
- RFC 6761，*Special-Use Domain Names*：https://www.rfc-editor.org/rfc/rfc6761
- RFC 5737，*IPv4 Address Blocks Reserved for Documentation*：https://www.rfc-editor.org/rfc/rfc5737
- DNS Flag Day 2020，EDNS 缓冲区尺寸建议（1232 的由来）：https://dnsflagday.net/2020/
