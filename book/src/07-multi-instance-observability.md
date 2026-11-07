# 多实例、缓存与可观测性

一台机器上只跑一个 Unbound 实例时，第 4–6 章的配置都还直观：改配置、restart、看 `dig`。一旦把角色拆开——一个实例做过滤与客户端入口，一个做递归 / 本地根区镜像，一个做加密出口——就会冒出三类新问题：端口与接口怎么划、过滤表大表吃掉多少内存、以及"到底哪一层缓存里有旧数据"。

本章不新增角色，只把前面三章的实例放到同一台主机上，给出规划方法、观测手段与刷缓存的完整路径。实验环境仍是一台家庭网关 + 一台境外云主机，以下为教学用示例地址：IPv4 用 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`（[RFC 5737]），IPv6 用 `2001:db8::/32`（[RFC 3849]），内网写 `192.168.68.0/24`、ULA 写 `fd00::/64`，主机名统一 `gw-01` / `edge-01` / `cloud-01`，不声明是真实环境。所有令牌与凭据一律写 `<TOKEN>` 或 `***`。

## 7.1 同一主机多实例的端口 / 接口规划

多实例最容易出的低级错是"配置文件都对了，但两个进程抢同一个东西"。先把独占资源列全：

| 资源 | 冲突表现 | 规划原则 |
|---|---|---|
| `interface` / `port` | 第二个实例启动报地址已占用 | 每个实例一组独立 `IP@端口` |
| `pidfile` | 后启动的覆盖前者，`systemctl` 管错进程 | 每实例独立路径，如 `/run/unbound-filter.pid` |
| `remote-control` 的 `control-interface` / 端口 | `unbound-control` 连到别的实例 | 每实例独立控制端口，或只给一个实例开 |
| 证书 / 密钥文件 | 控制连接认证失败 | 每实例一套 `server-key-file` 等四件套 |
| `chroot` / 工作目录 | 相对路径互相覆盖 | 不共用；本章统一 `chroot: ""` |
| `zonefile` / 缓存落盘路径 | 根区副本互相踩 | 每实例独立目录 |
| uid 豁免规则（`nft meta skuid`） | 第 6 章的根区镜像不更新 | 所有实例的 uid 都要覆盖 |

一个可复制的三实例规划。注意 `interface` 用 `@port` 后缀，这是 Unbound 原生支持的写法：

**为什么这么设**（三段配置合起来看）：三实例分别承担入口过滤、递归 / 本地根区镜像、加密出口，因此端口段、`pidfile`、`zonefile` 目录与控制端口必须互不重叠；A 把 B 当作唯一 `forward-addr`，B 有真实出向 53，C 走 853。这三点共同决定了下面三段配置里的每一个端口与路径。

```yaml
# 实例 A：客户端入口 + 过滤表，/etc/unbound/filter.conf —— 教学用示例地址
server:
  interface: 192.168.68.1@53
  interface: 127.0.0.1@5301
  access-control: 192.168.68.0/24 allow
  access-control: 127.0.0.0/8 allow
  username: unbound
  chroot: ""
  pidfile: /run/unbound-filter.pid
  num-threads: 2
  msg-cache-size: 64m
  rrset-cache-size: 128m
  do-not-query-localhost: no
  edns-buffer-size: 1232
  max-udp-size: 1232
  log-servfail: yes

forward-zone:
  name: "."
  forward-addr: 127.0.0.1@5302
  forward-first: no

remote-control:
  control-enable: yes
  control-interface: 127.0.0.1@8953
```

```yaml
# 实例 B：递归 + 本地根区镜像，/etc/unbound/recursor.conf —— 教学用示意地址
server:
  interface: 127.0.0.1@5302
  access-control: 127.0.0.0/8 allow
  username: unbound
  chroot: ""
  pidfile: /run/unbound-recursor.pid
  num-threads: 2
  msg-cache-size: 64m
  rrset-cache-size: 128m
  edns-buffer-size: 1232
  max-udp-size: 1232
  log-servfail: yes

auth-zone:
  name: "."
  primary: 198.51.100.53
  fallback-enabled: yes
  for-downstream: no
  for-upstream: yes
  zonefile: /var/lib/unbound-recursor/root.zone

remote-control:
  control-enable: yes
  control-interface: 127.0.0.1@8954
```

```yaml
# 实例 C：加密出口（示意），/etc/unbound/egress.conf —— 教学用示例地址
server:
  interface: 127.0.0.1@5303
  access-control: 127.0.0.0/8 allow
  username: unbound
  chroot: ""
  pidfile: /run/unbound-egress.pid
  tls-cert-bundle: /etc/ssl/certs/ca-certificates.crt
  log-servfail: yes

forward-zone:
  name: "."
  forward-addr: 192.0.2.53@853#dns.example.org
  forward-tls-upstream: yes
  forward-first: no

remote-control:
  control-enable: yes
  control-interface: 127.0.0.1@8955
```

**为什么这么设**：三个实例分别承担"入口过滤 / 递归 / 加密出口"，端口段 5301–5303 与控制端口 8953–8955 全不重叠；`pidfile`、`zonefile` 目录、控制端口各自独立。A 用 `do-not-query-localhost: no` 把 B 当作唯一 `forward-addr`，条件与第 5 章一致：下一跳是本机另一实例、端口不冲突、拓扑不成环（B 做递归 / 本地根镜像，不会转发回 A）。

B 是第 6 章的形态，因此它**需要真实出向 53** 做 SOA 探测与 AXFR；C 走 853 的加密传输（DoT），与 DNSSEC 的数据完整性 / 来源认证不是一回事（第三篇第 11 章展开服务端 DoT）。

三个实例都以 `username: unbound` 运行，所以第 6 章的 `nft output-tproxy` 豁免规则里 `meta skuid unbound udp/tcp dport 53 accept` 对它们同时生效；若你把某个实例改成别的 uid，别忘了补规则。

**怎么验证**：

```bash
for f in filter recursor egress; do unbound-checkconf /etc/unbound/${f}.conf || exit 1; done
ss -lunp | awk '$5 ~ /:(53|5301|5302|5303|8953|8954|8955)$/ {print}'
unbound-control -c /etc/unbound/filter.conf status
unbound-control -c /etc/unbound/recursor.conf status
unbound-control -c /etc/unbound/egress.conf status
```

`unbound-control -c` 指定配置文件是关键：它会读取该文件里的 `remote-control:` 段去连对应控制端口，不指定就会连到默认实例，出现"命令执行成功但改的不是我想改的实例"。

**失败模式 / 回滚点**：`ss` 里缺某个端口 → 先检查 `interface` 是否只 reload 没 restart（第 4 章：`interface` 变更只有 restart 生效）；控制端口连不上 → 查 `control-interface` 与证书四件套是否各实例独立。回滚：每实例的配置、pidfile、zonefile 都是独立文件，逐个 `systemctl stop` 即可，不会互相牵连。

## 7.2 按域名分流到不同实例

"按域名分流"有两种完全不同的实现，先分清再动手：

1. **在单个实例内部按区（zone）分流**：用 `forward-zone: name: "example.org"` / `stub-zone` 把特定后缀交给指定上级，其他查询走默认路径。这是 Unbound 原生的能力，不需要多进程。
2. **让不同客户端落到不同实例**：靠 `interface` 地址 / 端口区分，由上游设备（RA/RDNSS、DHCP、策略路由）决定客户端问哪一个。第五篇第 19–21 章讲 RA / RDNSS 与网关工程，这里只给 DNS 侧的接口。

第 1 种的写法，把内部域名交给内网权威服务器、其余交给唯一上游：

```yaml
# /etc/unbound/filter.conf 的追加片段 —— 教学用示例地址
# 需与 7.1 节的 server: 段合并使用：那里已有 do-not-query-localhost: no
server:
  do-not-query-localhost: no

forward-zone:
  name: "corp.example.net."
  forward-addr: 192.168.68.53
  forward-first: no

forward-zone:
  name: "."
  forward-addr: 127.0.0.1@5302
  forward-first: no
```

**为什么这么设**：多个 `forward-zone` 段可以共存，官方手册说"按最接近查询名的区生效"，所以 `corp.example.net.` 比 `.` 更具体、优先命中。两条都写 `forward-first: no`，保证任一路径失败时不会逃出去自行递归。注意每个区仍只给**一条** `forward-addr`：第 5 章的"并发竞速取最快"结论对每一个 `forward-zone` 都成立，多写一条就会绕过既定上游。

第 2 种是让不同实例监听不同地址，客户端按需选：

| 客户端类别 | 询问地址 | 承担实例 |
|---|---|---|
| 普通 LAN 客户端 | `192.168.68.1@53` | A（入口 + 过滤） |
| 需要完整递归的排障主机 | `127.0.0.1@5302` | B（递归 + 根镜像） |
| 只走加密出口的进程 | `127.0.0.1@5303` | C（DoT 出口） |
| IPv6 / ULA 客户端 | `fd00::1@53` | A（同一实例另一 `interface`） |

**怎么验证**：

```bash
dig @127.0.0.1 -p 5302 host.corp.example.net A +short
dig @192.168.68.1 host.corp.example.net A +short
dig @127.0.0.1 -p 5303 example.org A +short
unbound-control -c /etc/unbound/filter.conf list_forwards
```

**失败模式 / 回滚点**：最常见的错是"配了具体区却忘了默认区"，导致不匹配的名字无路可走；或者反过来，把具体区写成了 `.`，等于没分流。回滚：删掉具体 `forward-zone` 段并 restart，所有查询回落到默认区。还要注意第 6 章讲过的 uid 豁免——B 实例的分流若把出向 53 指向了自己，会形成自问自答的环。

## 7.3 过滤表（local-zone 大表）的内存代价与 AppArmor 路径限制

```yaml
# /etc/unbound/filter.conf 的追加片段 —— 教学用示例地址
server:
  # 过滤表通常通过 include 引入，路径必须在 AppArmor 白名单内
  include: /etc/unbound/filter.d/*.conf

  private-address: 192.168.68.0/24
  private-address: 10.0.0.0/8
  private-address: fd00::/8
  private-address: 2001:db8::/32
```

**为什么这么设**：`local-zone` 大表是过滤的主流做法——每个要挡的域名一条 `local-zone: "name." always_nxdomain` 或 `refuse`。官方手册允许用 `include` 拆分配置文件，但这里有两个工程约束。

**内存代价**：`local-zone` 条目在启动时全部载入内存，不是按需查磁盘。表越大，常驻内存越高；叠加 `msg-cache-size` / `rrset-cache-size`（第 4 章按 1:2 放大、实际还有 2–2.5 倍 malloc 开销）与每个实例的独立开销，三实例机器上很容易把内存吃满。用 `unbound-control stats_noreset` 里的 `mem.*` 计数器观察，别只盯着 RSS；如果过滤表从十万条涨到百万条，先做一次内存基线再上线。RPZ 是另一种过滤机制（官方手册单列一节），大表场景下可以评估是否比逐条 `local-zone` 更省，但那是另一套配置，不在本章展开。

**AppArmor 路径限制**：`include` 的相对路径在 `chroot` / 工作目录内解释，而 AppArmor / SELinux 只允许进程读白名单里的路径。把过滤表放到 `/etc/unbound/filter.d/` 之外的新目录（例如 `/srv/blocklist/`）而不更新 profile，现象是启动失败或表里根本没加载——`unbound-checkconf` 可能照样通过，因为检查的是语法而不是 LSM 放行。第 6 章的根区 `zonefile` 落盘也受同一条限制：路径要同时对 `unbound` uid 可写、在 chroot 内可见、在 AppArmor 白名单内。

`private-address` 与过滤表是两件事：它把指定网段从**公网域名的应答**里剔除，防 DNS Rebinding；官方手册说明它移除 A / AAAA / SVCB / HTTPS 里匹配的资源记录（含 SVCB 的 `ipv4hint` / `ipv6hint` 参数），且启用 DNSSEC 验证时可能把含私有地址的应答标为 bogus。这里把内网段与文档地址段都列上，仅作教学示例。**副作用要提前知道**：若你自己用 `192.168.68.x` 作为内网服务的权威应答，也会被剔除，需要用 `local-zone` / `local-data` 或 `private-domain` 例外处理。

**怎么验证**：

```bash
unbound-control -c /etc/unbound/filter.conf list_local_zones | wc -l
unbound-control -c /etc/unbound/filter.conf stats_noreset | grep -E 'mem\.|total.num.queries'
aa-status 2>/dev/null | grep -i unbound || true
dig @192.168.68.1 blocked.example.net A +short
```

**失败模式 / 回滚点**：`list_local_zones | wc -l` 远小于预期条数，先怀疑 include 通配符没匹配到、或 AppArmor 拒绝读取；`journalctl -k` / `audit.log` 里的 DENIED 是证据。回滚：把过滤表放回白名单内的既有目录，或临时注释掉 `include` 行 restart（过滤失效是预期代价，要同步告知使用方）。

## 7.4 DDR 压制与 always_nodata

```yaml
# /etc/unbound/filter.conf 的追加片段 —— 教学用示例地址
server:
  local-zone: "_dns.resolver.arpa." always_nodata
  local-zone: "use-application-dns.net." always_nxdomain
  local-zone: "blocked.example.net." always_nxdomain
  local-zone: "ads.example.org." always_nodata
```

**为什么这么设**：DDR（Discovery of Designated Resolvers）允许客户端通过 `_dns.resolver.arpa.` 自动发现采用加密传输（DoT / DoH / DoQ）的服务器，然后绕过本地解析器直连外部 DoH / DoT。给这个特区配 `always_nodata`（返回 NODATA，且忽略 local-data）就是压制自动发现，让客户端继续用本地实例。官方手册对 `always_nodata` 的定义是"像 `static`，但忽略 local data 并返回 NODATA"；`always_nxdomain` 同理返回 NXDOMAIN。

浏览器侧还有 canary 域：Firefox 用 `use-application-dns.net` 判断该不该启用自带 DoH，回 NXDOMAIN 即可关闭；Chrome 靠系统解析器是否匹配已知 DoH 提供商列表来判断，不需要按域名封。**按 IP 封 443 是 denylist 而不是证明**，会误伤且随时失效，别把它当主要手段。

注意这里 `always_nodata` / `always_nxdomain` 的语义与 `static` 不同：它们忽略该区下的 `local-data`，因此"整区一律这样答"的场景才用；需要给个别名字留白名单时，应改用 `transparent` 或配合具体 `local-data`。

**怎么验证**：

```bash
dig @192.168.68.1 _dns.resolver.arpa. SVCB +short
dig @192.168.68.1 use-application-dns.net A +short
dig @192.168.68.1 blocked.example.net A
```

第一条应为空且 `status: NOERROR`（NODATA），第二条 `status: NXDOMAIN`，第三条同样 NXDOMAIN。若第一条返回了 SVCB 记录，说明 `local-zone` 没生效或客户端问的是另一个实例。

**失败模式 / 回滚点**：过度压制会挡掉合法的内部 SVCB / HTTPS 记录使用（例如自己部署的服务发现），排查时先确认是不是这条 `always_nodata` 吞掉了。回滚：把对应 `local-zone` 行改为 `nodefault` 或删掉并 reload。

## 7.5 dnstap 全量查询流与 30 天保留期

dnstap 是结构化二进制日志，**记录的是自己的查询流副本**：客户端问了什么、本实例向谁问了什么、回了什么。它对抗"元数据在别处被聚合"很有用，但代价是把同样的元数据集中到自己门口。所以开启全量日志的同时必须写清保留期——**本书统一取 30 天**，并配套轮转与到期删除。

```yaml
# /etc/unbound/filter.conf 的追加片段 —— 教学用示例地址
dnstap:
  dnstap-enable: yes
  dnstap-socket-path: /run/dnstap.sock
  dnstap-bidirectional: yes
  dnstap-log-client-query-messages: yes
  dnstap-log-client-response-messages: yes
  dnstap-log-resolver-query-messages: yes
  dnstap-log-resolver-response-messages: yes
  dnstap-log-forwarder-query-messages: yes
  dnstap-log-forwarder-response-messages: yes
```

**为什么这么设**：官方手册说明 dnstap 需编译时带 `--enable-dnstap`，启用后（带线程编译时会）另起一个线程把日志写到目标；`dnstap-socket-path` 指向接收端监听的 unix socket，`dnstap-ip` 为空时就用 socket。上面六项 `dnstap-log-*-messages` 分别覆盖"客户端查询 / 应答、递归器查询 / 应答、转发器查询 / 应答"，六项全开才是全量。多实例机器上要注意：A 是转发器角色（会产 forwarder 类消息），B 是递归角色（会产 resolver 类消息），C 也是转发器，三份日志合起来才是完整链路视图。

`dnstap-tls` 默认 `yes`，只在用 `dnstap-ip` 走 TCP 时才涉及；unix socket 形态下用不到。接收端（例如 dnstap 收集器）把帧流写盘，落盘路径同样要过 AppArmor 白名单。

**保留期的落地写法**（示意，接收端落盘后由 logrotate / timer 执行）：

```bash
#!/bin/sh
# /usr/local/sbin/dnstap-retention.sh —— 保留 30 天，教学用示例
set -eu
DIR=/var/log/dnstap
find "$DIR" -type f -name '*.dnstap' -mtime +30 -delete
find "$DIR" -type f -name '*.dnstap' -mtime +1 -exec gzip -9 {} +
```

**为什么这么设**：`-mtime +30 -delete` 明确实现 30 天保留，`+1` 起压缩省空间。没有这一步，"抗元数据聚合"在自己门口破功：一年后磁盘里躺着完整的全屋查询历史，风险比上游聚合更集中。保留期要写进运维文档并定期审计，不要只放在脚本里。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/filter.conf
ls -l /run/dnstap.sock
find /var/log/dnstap -type f -name '*.dnstap*' -printf '%TY-%Tm-%Td %p\n' | sort | head
find /var/log/dnstap -type f -name '*.dnstap*' -mtime +31 | wc -l
```

最后一条应为 0（若有残留说明清理任务没跑）。接收端是否真的收到帧，用收集器自带的计数或 `dnstap-*` 工具查看，具体命令随实现不同，不要凭 `ls` 有文件就断定内容完整。

**失败模式 / 回滚点**：`dnstap-enable: yes` 但 socket 不存在 → 连接失败，日志里持续报错，解析本身通常不受影响；编译没带 dnstap 时选项可能被忽略。回滚：`dnstap-enable: no` 并 restart，同时确认保留期脚本仍在跑（历史文件不会因为关掉采集就自动消失）。

## 7.6 unbound-control 开启与不开的取舍

`remote-control` 默认 `control-enable: no`。开启需要 `unbound-control-setup` 生成自签证书四件套（`server-key-file`、`server-cert-file`、`control-key-file`、`control-cert-file`），客户端与服务端都读同一段配置。

| 维度 | 开启 `unbound-control` | 不开 |
|---|---|---|
| 刷缓存 | `flush` / `flush_zone` / `flush_type` 精确清除 | **只能重启实例**清空整份缓存 |
| 统计 | `stats` / `stats_noreset` 随时取 | 只能靠 `statistics-interval` 打日志或 dnstap |
| 在线改配置 | `set_option`、`list_forwards`、`list_local_zones` 等 | 只能改文件 + restart / reload |
| 暴露面 | 多一个控制端口 + 证书文件权限管理 | 无控制端口 |
| 多实例 | 需每实例独立控制端口与证书 | 无此问题，但也无法定位刷哪一层 |

**不开时的代价要特别算清**：负缓存（negative caching）会放大故障——上游一次瞬时 NXDOMAIN 按 SOA minimum 缓存（常见 3600 秒），修好权威服务器后客户端仍在失败。`unbound-control` 关着时只能重启实例，而**链路上每个实例都要刷**：7.1 节里 A（入口过滤）、B（递归 / 根镜像）、C（加密出口）各自有独立缓存，只重启 A 的话，B 里那条缓存的 NXDOMAIN 照样返回给 A。同一类问题还有 `qname-minimisation-strict: yes` 的兼容坑：某大型 CDN 的 CNAME 区违反 [RFC 8020]（父名 NXDOMAIN、子名却存在），strict 模式下中间层拿到 NXDOMAIN 就不回退完整域名，导致整站 CSS/JS 全挂；改成 `strict: no` 后**链路上每一层缓存都要刷**，否则前一层照样返回缓存的 NXDOMAIN。参数本身在第三篇第 13 章，这里只处理"缓存怎么清"。

因此本书的建议是：**多实例部署必须开 `unbound-control`**，但把 `control-interface` 严格限制在回环、证书文件属主与权限收紧、每实例独立端口。单实例且完全不需要运行时观测的极简部署才考虑不开，并接受"排障靠重启"的代价。

```bash
# 精确刷缓存：先定位是哪一层，再决定刷哪个实例
unbound-control -c /etc/unbound/recursor.conf flush_zone example.org
unbound-control -c /etc/unbound/filter.conf  flush_zone example.org
unbound-control -c /etc/unbound/egress.conf  flush_zone example.org

# 只清坏条目 / 只看不清零的统计
unbound-control -c /etc/unbound/recursor.conf flush_bogus
unbound-control -c /etc/unbound/recursor.conf stats_noreset | grep -E 'total.num.cachehits|total.num.cachemiss'
```

**为什么这么设**：`flush_zone name` 清除该名字及以下的所有缓存信息，比整实例重启温和得多；`flush_bogus` 清掉 DNSSEC 相关的坏条目与迭代器失败记录。三条命令逐个实例执行，就是把"链路上每个实例都要刷"落成可执行动作。统计一律用 `stats_noreset`——官方手册说明 `stats` 默认会重置计数（受 `statistics-cumulative` 控制），做基线时把自己读没的数据抹掉很冤。

**怎么验证**：

```bash
unbound-control -c /etc/unbound/recursor.conf cache_lookup example.org | head
unbound-control -c /etc/unbound/recursor.conf lookup example.org | head
dig @192.168.68.1 example.org A +short
```

`cache_lookup` 刷前后应看到条目消失 / 重建；`lookup` 显示"将会用哪些名字服务器查这个名字"，可用于确认根镜像是否生效。

**失败模式 / 回滚点**：控制证书权限不对（降权后 `unbound` 用户读不到）→ 连不上，`journalctl` 里有 TLS / 文件权限错误。回滚：`control-enable: no` 并 restart，回到"只能重启刷缓存"的模式；此时务必把链路上所有实例列成清单，避免漏刷。

## 7.7 日志字段与 SERVFAIL 排查

```yaml
# 各实例 server: 段的公共片段
server:
  verbosity: 1
  log-servfail: yes
  statistics-interval: 0
  extended-statistics: yes
  log-time-ascii: yes
```

**为什么这么设**：`log-servfail: yes` 专门打印"查询为什么回了 SERVFAIL"，官方手册说它独立于 verbosity 的调试日志、体积小、且以 error 级别输出——这是排障时性价比最高的一行，不必把 `verbosity` 拉到 4 把磁盘灌满。`extended-statistics: yes` 让统计里多出查询类型等维度；`statistics-interval: 0` 关掉周期性日志打点，改由 `unbound-control` 按需取（官方监控页也是这个建议）。`log-time-ascii` 让时间戳可读，便于把 dnstap、journald、nftables 计数三处时间对齐。

**怎么验证**：改完 `systemctl restart` 后查一次 `unbound-control -c <对应配置> stats_noreset | grep -E 'num.queries|num.answer.bogus|recursion.time'`，确认扩展统计字段已出现；`unbound-control -c <对应配置> get_option log-servfail` 应回 `yes`；`journalctl -u unbound-filter -n 20` 应能看到可读的 ASCII 时间戳。

**失败模式 / 回滚点**：四行是各实例 `server:` 段的公共片段，不要单独作为一个配置文件加载；若某个实例的 include 顺序把它覆盖掉，排查时先比对生效值（`unbound-control -c <配置> get_option log-servfail`）。回滚：删掉追加的 `extended-statistics` / `log-time-ascii` 两行，保留 `log-servfail: yes` 即可。

一条 SERVFAIL 的排查顺序，按"离客户端由近到远"：

1. **哪个实例回的**：`dig` 的 `SERVER:` 行 + `unbound-control -c <对应配置> stats_noreset` 看 `num.queries` / `num.SERVFAIL` 类计数。多实例时最容易在这一步认错对象。
2. **是不是访问控制**：`access-control` 无匹配默认 `refuse`（回 REFUSED，不是 SERVFAIL）；REFUSED 与 SERVFAIL 要分清。
3. **看 `log-servfail` 的具体原因**：常见是上游超时、DNSSEC 验证失败（bogus）、根区 / 转发区无法建立。第 5 章的 `failed to prime trust anchor`、第 6 章的区过期都在这里出现。
4. **是不是缓存里的旧负面结果**：`unbound-control cache_lookup <name>` 看有没有缓存的 NXDOMAIN / bogus；有就按 7.6 节逐层刷。
5. **是不是出向链路被自己劫持**：第 6 章的结论——先看 `nft list ruleset`，确认 `meta skuid unbound udp/tcp dport 53 accept` 在劫持规则之前。SOA 探测通但 AXFR 建不起来，就是 TCP/53 没豁免。
6. **是不是 EDNS / MTU**：`edns-buffer-size` 被改成 1472 导致分片被丢，表现为"超时后重试 TCP"而非立刻报错（第 4 章）。

**怎么验证**：

```bash
journalctl -u unbound-filter --since '10 min ago' | grep -iE 'servfail|error|prime' | tail -30
unbound-control -c /etc/unbound/recursor.conf stats_noreset | grep -E 'num.queries_timed_out|num.recursivereplies|num.expired'
dig @192.168.68.1 example.org A | grep -E 'status:|SERVER:'
```

**失败模式 / 回滚点**：只把 `verbosity` 调高而不开 `log-servfail`，会得到大量无关调试行却仍看不清 SERVFAIL 原因。回滚：`verbosity: 1` + 保留 `log-servfail: yes`。`num.queries_timed_out` 持续增长而 `num.recursivereplies` 不涨，基本可以断定是出向链路或上游，不是本机缓存。

## 7.8 缓存命中率与延迟的基线测量方法

没有基线就谈不上"调优有效"。下面给一套可复制的测量法，全部用 `stats_noreset` 与 `dig`，不依赖外部监控栈。

```bash
#!/bin/sh
# /usr/local/sbin/unbound-baseline.sh —— 教学用示例地址
# 用法：unbound-baseline.sh <实例配置> <监听地址> [端口]
set -eu
CONF=${1:?实例配置}
ADDR=${2:?监听地址}
PORT=${3:-53}
CTRL="unbound-control -c $CONF"

# 1) 清空计数、冷缓存起点
$CTRL flush_stats >/dev/null
$CTRL flush_zone . >/dev/null 2>&1 || true

# 2) 固定样本集：冷查一次 + 热查三次
NAMESET="example.org example.net example.com"
for n in $NAMESET; do dig @${ADDR} -p ${PORT} "$n" A +short >/dev/null; done
$CTRL stats_noreset | grep -E 'total.num.cachehits|total.num.cachemiss' > /tmp/ub-cold.$$
for i in 1 2 3; do
  for n in $NAMESET; do dig @${ADDR} -p ${PORT} "$n" A +short >/dev/null; done
done
$CTRL stats_noreset | grep -E 'total.num.cachehits|total.num.cachemiss' > /tmp/ub-hot.$$

# 3) 延迟样本：dig 输出的 "Query time"
for n in $NAMESET; do
  dig @${ADDR} -p ${PORT} "$n" A | grep 'Query time'
done

# 4) 递归耗时分布（仅统计需要递归的查询，缓存命中不计入）
$CTRL stats_noreset | grep -E 'recursion.time.avg|recursion.time.median|num.prefetch|num.expired|num.queries_timed_out'

echo "--- 冷 ---"; cat /tmp/ub-cold.$$
echo "--- 热 ---"; cat /tmp/ub-hot.$$
rm -f /tmp/ub-cold.$$ /tmp/ub-hot.$$
```

**为什么这么设**：

- `stats_noreset` 全程使用，避免自己把计数读没；`flush_stats` 只在起点调用一次，用来定义测量窗口。
- `flush_zone .` 是可选的冷缓存起点（会清整份缓存），生产环境慎用；若不想清空，就改成"记录起始计数、用差值计算"，不要影响线上业务。
- 命中率按窗口算：`hit / (hit + miss)`。冷查三个新名字应几乎全是 miss，随后三轮同名字查询应几乎全是 hit，这个对比能直接证明缓存确实在工作。
- `dig` 的 `Query time: N msec` 是**单次、客户端侧**的观测值，样本少、抖动大，只用来看量级；真正稳定的递归延迟看 `threadX.recursion.time.avg` 与 `recursion.time.median`。官方手册说明 avg 只统计需要递归处理的查询（缓存命中的不计入），且因个别不响应服务器的离群值，avg 常大于 median，median 由直方图插值得出、代表 50% 用户查询的上限。
- `num.prefetch`（预取次数）、`num.expired`（过期缓存应答次数）、`num.queries_timed_out` 一起看，能区分"命中率高是因为缓存健康"还是"因为一直在回过期数据"。

基线要落在纸上：改动前后各跑一次，记录命中率、median 递归延迟、`num.expired`、`num.queries_timed_out` 四项。第 4 章的 `num-threads` / 缓存尺寸 / `prefetch`、第 5 章巡检脚本的 `cmp -s`、第 6 章的 uid 豁免，效果都应该能在这四项上看到——`cmp -s` 缺失时最典型的信号就是命中率周期性掉到接近 0（每分钟被 restart 清一次缓存）。

**怎么验证**：

```bash
unbound-baseline.sh /etc/unbound/filter.conf 192.168.68.1 53
unbound-control -c /etc/unbound/filter.conf stats_noreset | grep -E 'total.num.cachehits|total.num.cachemiss'
```

热查阶段的 `cachehits` 增量应约等于重复查询次数；若命中数不涨，先怀疑脚本里 `dig` 的目标地址 / 端口与 `unbound-control -c` 指向的不是同一个实例。

**失败模式 / 回滚点**：`flush_zone .` 在多实例链路上只清了一个实例的缓存，其他层仍返回旧答案——这正是 7.6 节的结论，测量时若发现冷查也全命中，先逐层确认。回滚：脚本只做读操作与一次可选的 flush，不改任何配置；删掉 `flush_zone .` 行即可让测量完全无副作用。

## 本章要点

- 同一主机多实例要独立规划 `interface@port`、`pidfile`、`remote-control` 控制端口与证书、`zonefile` 目录；`interface` 变更只有 restart 生效，`unbound-control -c <配置>` 才能连到指定实例。
- 按域名分流可用多个 `forward-zone`（最具体区优先，每区仍只留一条 `forward-addr`），或靠不同 `interface` 地址让客户端落到不同实例；后者由 RA / DHCP / 策略路由决定，DNS 侧只负责监听面。
- `local-zone` 大表全量常驻内存，需与缓存尺寸、malloc 开销一起做基线；`include` 路径与根区 zonefile 都要同时满足 `chroot` 可见、`unbound` uid 可读写、AppArmor 白名单三项。
- DDR 压制用 `local-zone: "_dns.resolver.arpa." always_nodata`，Firefox canary 域 `use-application-dns.net` 回 NXDOMAIN；按 IP 封 443 是 denylist 不是证明。`private-address` 剔除公网应答里的私有地址防 DNS Rebinding，会误伤内网权威应答。dnstap 六类 `dnstap-log-*-messages` 全开才是全量查询流副本，**本书统一保留 30 天**且必须有到期删除，否则"抗元数据聚合"在自己门口破功。
- 不开 `unbound-control` 时刷缓存只能重启实例，且链路上每个实例都要刷（负缓存 NXDOMAIN、QMIN strict 兼容坑都属此类）；多实例部署建议开启但把控制接口限制在回环。
- `log-servfail: yes` 是排障首选，SERVFAIL 按"哪个实例 → 访问控制 → 日志原因 → 缓存旧条目 → nftables 出向豁免 → EDNS/MTU"顺序查；基线用 `stats_noreset` 记录改动前后的命中数、`recursion.time.median` 与 `num.expired`。

## 参考资料

- Unbound 官方手册，`unbound.conf(5)`（`interface`、`local-zone`、`private-address`、`include`、`remote-control`、`dnstap` 各节）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound-control(8)` 命令与统计计数器（`stats_noreset`、`flush_zone`、`flush_bogus`、`lookup`、`cache_lookup`、`list_local_zones`）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- Unbound 官方手册，Monitoring and Reporting（`extended-statistics`、`statistics-interval`、按需统计）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/core/monitoring.html
- Unbound 官方手册，Performance Tuning（缓存内存比例、线程与 slabs）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/core/performance.html
- Unbound 官方手册，Tags and Views / RPZ 过滤主题页：https://unbound.docs.nlnetlabs.nl/en/latest/topics/filtering/rpz.html
- RFC 9462，*Discovery of Designated Resolvers (DDR)*：https://www.rfc-editor.org/rfc/rfc9462
- RFC 8020，*NXDOMAIN: There Really Is Nothing Underneath*（QMIN strict 兼容问题的背景）：https://www.rfc-editor.org/rfc/rfc8020
