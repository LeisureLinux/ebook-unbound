# DNS 故障排查工具箱

一台家庭网关加一台云主机的实验环境里，有个发版前做连通性探测的小脚本：对 `api.example.net` 调 `socket.getaddrinfo(host, 443)`，只试 `infos[:3]` 这前三个地址逐个发起 TCP 连接，全失败就记一条「TCP 连不上」。`getaddrinfo` 返回的九个地址里，前四个是 IPv6，在该链路上被对端直接 RST（`ECONNREFUSED 111`），后面五个 IPv4 全通；浏览器照常走 IPv4 打开了页面。于是日志里出现了一条很难解释的曲线：报故障，4 秒后「网络已恢复」，隔一会儿又报故障。网络从头到尾没坏过，坏的是探测脚本只看了排序列表的前三个。

这不是工具不会用，是**没分清哪一层在骗你**。`getaddrinfo` 给的是策略排序后的候选列表，[RFC 6724] 的默认地址选择规则会让双栈域名解析出的 IPv6 地址排在前面；它从来不保证「第一个最可用」，更不保证「前几个里一定有一个能连上」。名字解析成功了、TCP 连接失败了、上层页面却打开了，这三件事分别发生在三个不同的层，用一条「网络不通」概括，就会把告警和后续排查全带偏。第 19 章的源地址选择与临时地址寿命、第 22 章的前缀轮换故障、第 31 章的告警误报治理，都是同一条线索的不同切面。



从这个案例能抽出三条贯穿全章的结论：

1. **探测要全量遍历，不要取前几个。** 对 `getaddrinfo` 的返回先去重，再遍历全部地址（或至少显式 v4 优先），任一地址连通即判定为通；只试 `infos[:3]` 这类切片等于把地址排序策略当成了可用性结论。
2. **「网络已恢复」必须基于重探测。** 上层页面能打开，可能只是浏览器换了个地址族、换了条连接、甚至命中了缓存；不重新跑一遍同一套探测并记录实际连通的地址，这条日志不构成恢复证据。
3. **`getaddrinfo` 是策略排序，不是可用性排序。** 应用拿到列表后怎么选、失败后怎么回退，由应用自己决定（Happy Eyeballs 一类机制），不由解析器决定。排查时要把「名字给了哪些地址」和「应用试了哪些地址」分开看。

本章不按工具名字母序罗列命令，而是**按排查顺序**串一遍：谁在解析 → 解析器怎么答 → 抓到什么包 → 内核动过没 → 应用怎么连 → 归属与边界。全书前三十五章里 `dig`、`tcpdump`、`unbound-control`、`nft` 已经散落出现过几十次，这里做的是系统串讲与决策表，不重复各章的完整配置块。

## 第一层：谁在解析

用户说「DNS 挂了」，第一件事不是去 ping，而是确定**这个进程到底把名字交给了谁**。同一台机器上，浏览器、curl、Python 脚本、systemd 服务完全可能走不同的解析路径；把路径搞错，后面每一条命令都在回答另一个问题。

### 从存根配置读起

```bash
cat /etc/resolv.conf
cat /etc/nsswitch.conf
cat /etc/hosts
cat /etc/gai.conf
```

`/etc/resolv.conf` 里的 `nameserver` 只是存根解析器（stub resolver）的上游地址，不等于「真正在做递归的那台机器」。若看到 `nameserver 127.0.0.53`，那是 systemd-resolved 提供的**本地存根**，它后面还挂着按接口分配的 DNS、路由域、DNSSEC 与 LLMNR 策略；`127.0.0.53` 自己不解析任何东西，只是把查询接过去再分发。此时直接 `dig @127.0.0.53` 看到的，是 resolved 加工过的结果，而不是应用拿到的原始链路。

systemd-resolved 环境里优先读它的状态：

```bash
resolvectl status
resolvectl query api.example.net
```

`resolvectl status` 会按网络接口列出 `DNS Servers`、`DNS Domain`、`DNSSEC` 等设置，能解释「为什么这台机器上两个进程问同一个名字得到不同答案」——分接口、分路由域的上游本来就可以不同。若本机没有装 `resolvectl`（本章的实测环境就没装），退回到 `/etc/resolv.conf` 加上按接口的配置文件，`resolvectl status` 的替代路径见所用发行版的官方手册；**不要因为命令不存在就假定系统里没有存根层**，很多容器与精简镜像只是把配套命令行工具裁掉了。

`/etc/nsswitch.conf` 的 `hosts:` 行决定名字先查哪儿：`files dns` 意味着先读 `/etc/hosts`，命中就根本不发 DNS 包。`hosts` 行里若出现 `mdns4_minimal`、`resolve`、`myhostname` 之类模块，解析路径还会再拐弯（第 27 章讲单播 DNS 与多播 DNS 的分界）。`/etc/hosts` 里留的一条旧记录，是「改了权威数据却怎么都不生效」最常见的原因。`/etc/gai.conf` 则影响 `getaddrinfo` 的地址排序（label / precedence 表），章首案例里 IPv6 排前面就与这一层及 [RFC 6724] 的默认策略有关；改过 `precedence` 的机器，排序会跟别的机器不一样。

### getent 与应用走的是同一条路吗

```bash
getent hosts api.example.net
getent ahosts api.example.net
getent ahostsv4 api.example.net
getent ahostsv6 api.example.net
```

`getent` 走 NSS（Name Service Switch），是「以应用视角」取地址最贴近的一条只读命令：`hosts` 通常只给一个系列，`ahosts` 会给出 `getaddrinfo` 风格的多个地址，正好用来复现章首那个「前几个全是 v6」的场景。但要注意判读边界：**`getent` 可能根本不碰 DNS**——命中 `/etc/hosts`、命中缓存模块时就地返回了。因此 `getent` 成功不能证明 DNS 正常，`getent` 失败也不能直接推出 DNS 故障；它证明的是「NSS 这条链路的结果」。

想看应用程序真正调用了什么、拿到几个地址，最直接的办法是让它自己报出来：

```python
import socket
infos = socket.getaddrinfo("api.example.net", 443,
                          type=socket.SOCK_STREAM)
for family, socktype, proto, canonname, sockaddr in infos:
    print(family.name, sockaddr[0], sockaddr[1])
```

这段只是把候选列表打全，不做连接。把它和 `getent ahosts` 对照，就能分开「NSS 给了什么」与「`getaddrinfo` 排序后给了什么」。若列表前几项全是 `AF_INET6` 而应用只试前三项，结论已经出来了——不需要再抓包。

**怎么验证这一层**：`getent ahosts api.example.net` 与上面 Python 脚本的输出逐项对照，再 `cat /etc/resolv.conf` 确认存根指向哪里。
**失败模式 / 回滚点**：只读命令没有回滚问题；真正的风险是**误判**——把 `getent` 命中 `/etc/hosts` 的结果当成 DNS 应答，或者把 `127.0.0.53` 当成递归解析器。若为了测试临时改了 `/etc/hosts` 或 `/etc/gai.conf`，改前留一份 `cp -a` 备份，测完立刻还原；这两个文件是全局生效的，忘了还原会污染后面所有章节的实验结果。

这一层的出口：搞清楚名字交给了谁，才能进第二层去问那台解析器。若发现应用根本没走系统解析器（浏览器内置 DoH、App 内嵌 SDK、`local-zone` 接管），转第 12、13、24、29、32 章，不要继续往下查 DNS 服务器。

## 第二层：解析器怎么答

确定「谁在解析」之后，第二个问题是**那台解析器给出的答案是什么、以及这个答案从哪来**。`dig` 直连指定服务器、绕开本机存根；`+trace` 从根开始迭代，把委派链每一跳摊开；`delv` 与 `drill` 回答「签名对不对、为什么 bogus」；`dnstracer` 专门追委派链。这一层的分工是**查数据**，不是查连通。

### dig：把查询路径拆开

```bash
dig @192.0.2.53 api.example.net A +short
dig @192.0.2.53 api.example.net A +trace
dig @192.0.2.53 api.example.net A +dnssec
dig @192.0.2.53 api.example.net A +cd
dig @192.0.2.53 api.example.net A +tcp
```

- `@server` 指定服务器，绕过 `/etc/resolv.conf`。这是它最大的价值，也是最大的陷阱（见「哪条命令会骗你」）。示例里的 `192.0.2.53` 是文档地址，实际换成自己的解析器地址。
- `+short` 只给 RDATA，适合塞进脚本；排障时别只用 `+short`，头部里的 `status`、`flags`、TTL 往往才是线索。
- `+trace` 从根迭代到权威，能定位「卡在根、卡在 TLD、还是卡在权威」；它走的是**本机发起的迭代查询**，与递归解析器自己的缓存路径不是一回事（部分 `dig` 版本会连带请求 DNSSEC 记录，选项细节见官方手册）。
- `+dnssec` 请求 DNSSEC 相关记录，看应答里的 `AD` 位与 RRSIG；`+cd`（checking disabled，某些版本写作 `+cdflag`）让上游跳过验证，用来区分「数据本身坏了」和「本地验证器把它判成 bogus」。AD 位是**验证状态**，不是加密传输的标志——DNSSEC 管数据完整性与来源认证，DoT / DoH / DoQ 管传输加密与隐私，二者不互相替代。
- `+tcp` 强制走 TCP/53。UDP 被丢与 TCP 能通，是两种完全不同的故障；[RFC 7766] 之后 TCP 是 DNS 传输的必备能力，具体参数见官方手册。

```bash
host -t A api.example.net 192.0.2.53
nslookup -type=A api.example.net 192.0.2.53
```

`host` 输出简洁，适合快速看一个记录。`nslookup` 早已不推荐用于诊断（它有自己的解析库与行为，不总跟随 `resolv.conf` 的全部路径），但几乎所有系统上都还在、交互模式也确实方便查多个 QTYPE，所以还得会读它的输出；只是**别拿它的结果当权威判据**。

### delv：验签与「为什么 bogus」

```bash
delv @192.0.2.53 api.example.net A
delv @192.0.2.53 api.example.net A +rtrace
delv @192.0.2.53 api.example.net A +cd
```

`delv` 是 BIND 系工具，自带 DNSSEC 验证器并用自己配置的信任锚（trust anchor）独立验签，输出会给出 `fully validated` / `unsigned answer` / `validation failed` 一类结论。它可能返回 SERVFAIL 并附上原因（签名过期、缺 DS、链断在某一级、系统与权威时钟不同步导致 RRSIG 有效期不合法等），这正是 `dig` 只给一个 `SERVFAIL` 时不提供的信息。`+rtrace` 一类追链选项让验证过程可见，具体选项名与输出格式见所用版本的官方手册，不要凭记忆猜。

判读次序建议固定下来：`dig +dnssec` 看有没有 RRSIG / AD → `dig +cd` 看绕过验证后数据是否回来 → `delv` 看独立验证结论与失败原因。若 `+cd` 有答案、`delv` 报 bogus，故障在验证链（信任锚、时钟、上游剥离签名、`harden-dnssec-stripped` 一类设置），转第 10、16 章；若 `+cd` 也没答案，故障在数据或链路，回到 `+trace` 看断在哪一跳。

### drill 与 dnstracer：签名链和委派链

```bash
drill -S api.example.net @192.0.2.53
drill -T api.example.net @192.0.2.53
dnstracer -q a -r 3 api.example.net
```

`drill` 出自 NLnet Labs 的 ldns 工具集，`-S` 一类选项做 DNSSEC 签名链追踪，`-T` 做逐级 trace；`dnstracer` 专门做**逐级委派追踪**，输出从根到权威每一级由哪些 NS 应答、有没有缺胶水记录（glue record）、有没有环路。注意名字是 **`dnstracer`**，不是 `dnstrace`；命令拼错会让人误以为工具没装。两者在实测环境里属于「需要另装」的一类（按发行版包名安装 `ldnsutils` 一类工具包，具体包名见官方手册或发行版包索引），不是基础镜像自带。

`drill -S` 与 `delv` 的区别在于视角：`delv` 给「验签结论」，`drill` 更偏「把签名链逐段摊开给你看」；`dnstracer` 与 `dig +trace` 的区别在于 `dnstracer` 会把同一级多个 NS 都试一遍，容易发现「只有一个 NS 答错」这类单台权威问题。三者都不替代对方，按「先结论、再链、再逐台」的顺序用。

**kdig 不在本书范围内**：按用户决定忽略，只在此处与工具实装表标注为「忽略」，不写进书里的命令示例与排查流程。需要类似能力时用上面的 `dig` / `delv` / `drill` 组合。

**怎么验证这一层**：`dig @192.0.2.53 api.example.net A +short` 与 `delv @192.0.2.53 api.example.net A` 各跑一次，前者确认有应答，后者确认签名链结论。
**失败模式 / 回滚点**：全是只读查询，无回滚；但 `@server` 指向错误的地址、或在生产解析器上误跑大量 `+trace`，会拿到「另一台机器」的答案或给权威添压。演练控制在一个自持域名上，别对第三方权威做批量追踪。

这一层的出口：解析器确实返回了预期数据 → 进第三层看线上报文与缓存；验签失败 → 第 10、16 章；委派链断在权威 → 属于对方或链路问题，转第 28 章的故障域隔离。

## 第三层：抓到什么包

第二层看到的是「我主动问一次，得到什么」；第三层要看的是**链路上真实发生了什么**，包括那些没人主动问就发出的重传、被悄悄丢掉的包、以及缓存里已经存了很久的旧答案。

### 抓包：先分清三种「不通」

```bash
tcpdump -ni any 'port 53 or port 853 or port 443'
tcpdump -ni any -vv 'host 192.0.2.53 and udp port 53'
tshark -ni any -f 'port 53' -Y 'dns.flags.rcode != 0'
```

`tcpdump -ni any` 的 `-n` 关掉反向解析（否则抓包工具自己会发 DNS 查询，把现场搅乱），`any` 覆盖所有接口，过滤表达式把 53（明文 DNS）、853（DoT）、443（DoH / HTTPS）一次收进来。`tshark` 是 Wireshark 的命令行版，若装了可以用显示过滤器直接挑出非零 RCODE；没装就用 `tcpdump -w /tmp/dns-cap.pcap` 存盘后离线分析，抓包文件含完整查询内容，按第 7、31 章的日志纪律设保留期并及时删除。

三种「不通」在包上是完全不同的形状，混淆它们会直接导致改错配置：

| 现象 | 抓包看到 | 典型位置 |
|---|---|---|
| UDP 被丢 | 有出向查询、无回包，随后间隔重传同一事务 ID；无 ICMP 差错、无 RST | 上游限速、策略路由送进黑洞、QoS 丢 UDP、防火墙静默 drop |
| 被 RST / 拒绝 | TCP SYN 后收到 RST，或 UDP 后收到 ICMP port unreachable | 端口没监听、被显式 reject、服务进程已退出 |
| 超时 | 出向、回向都看得到，但应答 RCODE 为 SERVFAIL，或递归侧一直在等上游 | 上游超时、验证链等待、缓存里的旧 SERVFAIL、EDNS 缓冲区分片被丢 |

第一行最容易误判成「上游挂了」，实际可能是本机 nftables 把出向 53 劫持进了 tproxy（第 14、22 章那两个案例），下一层专门处理。第三行若伴随大应答，要想到 `edns-buffer-size` 过大在 MTU 1280 / PPPoE 链路上分片被丢、随后等超时重试 TCP 的坑（第 14 章）；分片与 QTYPE 分布都能在 `stats_noreset` 里找到旁证。

### dnstap 与运行时状态

dnstap 是解析器**自己的查询流副本**：它记录客户端地址、QNAME、QTYPE、应答码与上游交互，不需要从网卡上猜。采集点配置与保留期在第 7、31 章，这里只用它回答「这次故障期间，那个名字到底进来过没有、被判成什么」。全量日志要写明保留期并到期删除，否则「抗元数据聚合」在自己门口破功。

```bash
unbound-control stats_noreset
unbound-control lookup api.example.net
unbound-control dump_cache
unbound-control flush api.example.net
unbound-control local_zone example.net. always_nodata
```

- `stats_noreset` 只读不重置计数，看命中、SERVFAIL、`num.queries_timed_out`、QTYPE 分布；排障期间千万别用会重置计数的 `stats`，否则差分基线全丢（第 31 章的注入演练专门演示过这点）。
- `lookup` 打印该名字当前记录的权威服务器（nameserver）信息，`cache_lookup` 与 `dump_cache` 才是看缓存内容——「权威改了、解析仍旧返回旧值」时先用 `dump_cache` / `cache_lookup` 确认是不是旧应答还在，而不是继续改配置。
- `flush` **会清缓存**，属于有副作用的操作。它清的不只是正应答：NXDOMAIN 一类否定应答也会按 [RFC 2308] 的负缓存（negative caching）规则留存，SOA minimum 常见是几千秒量级，上游一次瞬时故障能被放大成「修好权威后客户端仍失败」的长时间故障。更麻烦的是链路上每个缓存实例都得刷——只刷一台，前一层照样返回缓存里的 NXDOMAIN。执行前记下当前 `dump_cache` 的相关条目，执行后重测；具体子命令参数见官方手册。
- `local_zone` 是运行时加本地区，用来验证「是不是本地区接管了名字」（第 24 章讲 `local-zone` 的匹配优先级）。运行时改动 reload 即失效，别当成已固化配置写进报告。

**怎么验证这一层**：`tcpdump -ni any 'port 53'` 开着跑一次 `dig @192.0.2.53 api.example.net A`，确认出向与回向成对出现；再 `unbound-control stats_noreset` 看计数器是否递增。
**失败模式 / 回滚点**：`flush` 清掉热缓存后瞬时递归量上升，`unbound-control` 通常需要相应权限（实测环境里要 `sudo -n` 或指定控制密钥路径，见官方手册）；抓包文件与 `dump_cache` 导出文件含查询内容，用完删除。回滚点是提前保存的 `dump_cache` 快照与规则集备份，不是「再刷一次」。

这一层的出口：包根本没出门 → 第四层查内核；包出门没回来且无 RST → 查上游与链路；包回来了但答案是旧的 → 处理缓存与负缓存（第 7、10 章）。

## 第四层：内核动过没

**先看 nftables，再怀疑别的。** 这是第 14、22 章反复用真实故障换来的顺序：本机解析器自己的出向 53 被 tproxy 吃掉、WireGuard 回包被 output 链打标送进代理，现象都长得像「上游不通」。在内核这一层，DNS 只是普通流量，规则不认识 QNAME。

### 只读查看与临时计数探针

```bash
nft list ruleset
nft -a list chain inet mangle output
conntrack -L | grep -E 'dport=53|sport=53'
ss -lntup | grep -E ':53|:853|:5353'
ip rule
ip -6 rule
```

`nft list ruleset` 一次看全表、链、集合与规则；`nft -a` 带出 `handle` 编号，是后面精确删除探针的依据。`conntrack -L` 看 UDP/53 条目是否堆积（重传与超时的旁证）；`ss -lntup` 确认本机到底有没有进程在 53 / 853 / 转发端口上监听；`ip rule` 与 `ip -6 rule` 看策略路由，解释「为什么这个源地址的包走了另一张表」。

要看「包究竟走到哪一行」，加一条临时 `counter` 探针：

```bash
nft -a list chain inet mangle output
nft add rule inet mangle output udp dport 53 counter
nft list chain inet mangle output
nft delete rule inet mangle output handle 12
```

**为什么这么设**：只读的 `list ruleset` 只能告诉你「规则存在」，不能告诉你「这条规则有没有被命中」；一条带 `counter` 的探针能在不改动现有规则、不 `nft -f` 重建表的前提下给出命中数。

**怎么验证**：`nft add rule` 后发起一次 `dig @192.0.2.53 example.org A`，再 `nft list chain inet mangle output`，探针的 `packets` 应递增；不递增说明流量根本没经过这条链。

**失败模式 / 回滚点**：`nft -f` 是**重建表**，会清空运行时加入的集合与计数（第 14 章专节讲过），排查期间不要顺手加载规则文件；`nft reset counters` 会清零所有计数、破坏差分基线（第 31 章）。删规则**必须带链名**（`nft delete rule <table> <chain> handle <N>`），只给 handle 在多数写法里不成立，删错链会留下半截状态。回滚按 handle 精确删除探针，或整体恢复改动前的 `nft list ruleset > /etc/nftables.conf.bak` 备份。

老系统还有 iptables-legacy 的 NAT 表在跑，单独看一眼即可，别在两套框架里同时改：

```bash
iptables -t nat -L -n -v
ip6tables -t nat -L -n -v
```

完整的劫持、豁免、fail-closed 配置块在第 14、22 章。这里要能判断「规则是否还在、是否被后续加载冲掉、出向豁免是否还在链首」。

**怎么验证这一层**：`nft list ruleset | grep -i -E 'counter|mark|dport 53|skuid'` 与 `ss -lntup` 对照，确认监听端口与包路径一致。
**失败模式 / 回滚点**：把「规则文件里写了」当成「运行时生效了」是这层最典型的误判——`nft -f` 重建后动态集合被清空、链首的显式 accept 被冲掉，现象会伪装成上游或 DDNS 脚本坏了。回滚用整体备份，不逐条删。

这一层的出口：规则与包路径都正常 → 第五层做端到端；规则有问题 → 第 14、22 章；监听端口不对 → 回到第 4–7 章的实例配置。

## 第五层：应用怎么连

前四层都在回答「名字和包」的问题；第五层才回答用户真正抱怨的那句「网页打不开」。这一层的核心手法是**逐项把变量摘出方程**：`--resolve` 把 DNS 摘掉，`-4` / `-6` 把地址族摘掉，`--noproxy` 把代理摘掉，`socks5h://` 则反过来把 DNS 明确交给代理。

### curl 的四种隔离

```bash
curl -v https://api.example.net/healthz
curl -v --noproxy '*' https://api.example.net/healthz
curl -v -4 --noproxy '*' https://api.example.net/healthz
curl -v -6 --noproxy '*' https://api.example.net/healthz
curl -v --resolve api.example.net:443:192.0.2.10 https://api.example.net/healthz
```

`curl -v` 的 `Trying 192.0.2.10:443...` 一行就是章首案例最需要的证据：它把「实际尝试了哪个地址」写出来了。`curl` **默认读取** `http_proxy` / `https_proxy` / `all_proxy` 环境变量，透明代理环境里残留一个指向死端口的变量，会让请求根本不经过本机 DNS，日志里却显示「连接失败」；所以第一组命令一定要带 `--noproxy '*'`，或先 `env | grep -i proxy` 看一眼。`-4` / `-6` 强制地址族，用来复现「v6 被 RST、v4 正常」；`--resolve` 直接指定 IP，把 DNS 从方程里彻底摘掉——若 `--resolve` 能通而原名不通，故障一定在解析链而非 TCP 链。

代理归属是另一条独立的分界。`ssh -D` 起的 SOCKS 监听**不自动接管系统 DNS**，应用要自己把名字交给代理，否则查询仍走本机解析器：

```bash
curl -v --noproxy '*' --socks5-hostname 127.0.0.1:1080 https://api.example.net/healthz
curl -v --noproxy '*' --proxy socks5h://127.0.0.1:1080 https://api.example.net/healthz
curl -v --noproxy '*' --proxy socks5://127.0.0.1:1080 https://api.example.net/healthz
```

`socks5h://` / `--socks5-hostname` 是**远程解析**（把主机名交给代理端），`socks5://` / `--socks5` 是**本地解析**（本机解析完把 IP 交给通道）。这类「DNS 归属开关」的语义（`resolvdns yes/no` 一类：yes 交对端、no 本机解析）在第 32 章已展开；要特别说明：**OpenSSH 的 `ssh_config` 里没有同名选项**，同类能力散落在不同客户端（PuTTY 的 "Do DNS name lookup at proxy end"、Firefox 的 `network.proxy.socks_remote_dns`、`sshuttle --dns` 等），具体归属以所用客户端文档为准。本章只用它做判读：本机无查询、对端有查询，才能证明名字确实交给了通道。

### DoT 与 ICMP

```bash
openssl s_client -connect 192.0.2.53:853 -servername resolver.example.org
ping -c 3 192.0.2.53
ping6 -c 3 2001:db8::53
```

`openssl s_client` 用来测 DoT 端点能否完成 TLS 握手并出示预期证书，端口是 853，与 DoH 的 443 不是一回事（完整服务端配置见第 11、12 章）。`ping` / `ping6` 只测 ICMP 可达性：**`ping` 通不能判断 DNS 通**——ICMP 可能被单独放行，而 53 被 drop；反过来 DNS 通也不代表 TCP 通，章首 v6 被 RST 就是反例。把 ping 当作「链路层还活着」的一个粗信号即可，绝不写进「DNS 正常」的结论里。

**怎么验证这一层**：`curl -v --noproxy '*' -4 https://api.example.net/healthz` 与 `-6` 版本对照，再与 `curl --resolve` 版本对照；三者的差异直接定位到地址族或解析链。
**失败模式 / 回滚点**：忘了 `--noproxy '*'` 是最常见的自欺；`--resolve` 里写错端口（应与 URL 的 443 一致）会连到不存在的服务；`socks5h://` 与 `socks5://` 只差一个字母却属于完全不同的 DNS 归属，写反了会得到「DNS 泄漏」的相反结论。全是客户端一次性参数，无回滚；但测试脚本里若把这些参数固化，记得发版前还原。

这一层的出口：任一地址能连、上层仍失败 → 应用层（证书、HTTP、代理 ACL）；全部地址连不上但 DNS 正确 → 回到第四层查包路径；只有某一地址族失败 → 第 19 章的源地址选择与前缀轮换。

## 第六层：归属与边界

最后一层不新增工具，只做一件容易被跳过的事：**把证据按层归位，指向该修的那一章**。同一条 `SERVFAIL`，可能是本地区接管（第 24 章）、验证链断裂（第 10 章）、上游超时（第 5、14 章）、负缓存（第 7 章）、也可能是本机 nftables 劫持（第 14、22 章）。不归位就会在错误的层反复改配置，越改越乱。

运行时状态与日志是归位的依据：

```bash
unbound-control stats_noreset
unbound-control get_option verbosity
unbound-control verbosity 3
journalctl -u unbound --since '10 minutes ago' --no-pager
unbound-control verbosity 1
```

`verbosity` 平时保持 1，排障窗口临时调到 2–3，排完立刻调回；它是 `unbound-control` 的运行时临时值，reload 即失效，别当固化配置写进演练报告（第 31 章已说明）。`log-servfail` 一行常开比把 verbosity 拉到很高更有性价比；`journalctl -u unbound` 配合 `--since` 缩小窗口，看 SERVFAIL 原因、验证失败、trust anchor priming 失败等行。日志关联的前提是所有链路共用同一个可信时钟：dnstap 帧、journald、nft 计数、抓包时间戳差几秒，因果链就会拼错（第 31 章）。

六层各自的「下一步去哪一章」：

| 层 | 确认的事实 | 若出问题，去哪一章 |
|---|---|---|
| ① 谁在解析 | 应用走的是哪个存根 / NSS 路径 | 第 3 章（四种角色）、第 19 章（地址选择）、第 27 章（mDNS）、第 12 / 13 / 24 / 29 / 32 章（客户端绕过与接管） |
| ② 解析器怎么答 | 数据与签名链是否正常 | 第 10、16 章（DNSSEC）、第 5 章（转发器）、第 6 章（递归与根镜像） |
| ③ 抓到什么包 | 报文有没有出门、缓存是不是旧的 | 第 7 章（缓存与可观测性）、第 14 章（EDNS 缓冲区 / 分片）、第 31 章（三层观测） |
| ④ 内核动过没 | 规则、集合、监听端口与策略路由 | 第 14、22 章（nftables 经验与故障案例） |
| ⑤ 应用怎么连 | 实际尝试的地址族、代理归属、TLS 握手 | 第 11、12 章（DoT / DoH）、第 19 章（源地址选择）、第 32 章（socks5h DNS 归属） |
| ⑥ 归属与边界 | 证据落在哪一层、该谁修 | 第 22 章（故障案例集）、第 28 章（故障域隔离）、第 31 章（告警与误报治理） |

与第 14/22 章的 nftables 经验、第 19 章的源地址选择、第 31 章的告警与观测、第 32 章的 socks5h DNS 归属，本章只作交叉引用，不重复那些章的完整配置块。

## 哪条命令会骗你

把散落在前面几层的陷阱集中列在这里。它们的共同点是：**命令本身没报错，输出也「看起来对」，但回答的是另一个问题**。

1. **`ping` 通 ≠ DNS 通。** ICMP 与 53 端口是两套放行规则；ICMP 通只能说明三层可达。`ping` 不通也不必然是故障——很多主机主动不回 ICMP。
2. **DNS 通 ≠ TCP 通。** 章首案例最典型：`getaddrinfo` 全量成功，前四个 v6 地址被 RST，后面五个 v4 全通。「解析成功」只覆盖名字到地址这一步，连接能否建立还要看地址族、路由、对端监听与防火墙。
3. **`curl` 默认吃代理环境变量。** `http_proxy` / `https_proxy` / `all_proxy` 残留时，请求可能根本不走本机 DNS、也不走目标地址；结论会指向一个根本没参与的设备。第一组命令永远带 `--noproxy '*'`。
4. **`nslookup` 与 `getent` 路径不同。** `nslookup` 用自己的解析库，不总跟随 `resolv.conf` 的全部路径；`getent` 走 NSS，可能直接读 `/etc/hosts` 或缓存模块而完全不碰 DNS。两者结果不一致时，先怀疑路径，别怀疑数据。
5. **`dig @server` 看到的是那台服务器，不是应用实际拿到的答案。** 指定 `@192.0.2.53` 就绕开了本机存根、绕开了 systemd-resolved 的分接口策略、绕开了 `local-zone` 与过滤表。它能证明「这台服务器这样答」，不能证明「应用收到这个」。要复现应用路径，用不带 `@` 的 `dig` 加上 `getent` / `curl -v`。
6. **「网络已恢复」不基于重探测就不是证据。** 上层页面打开、告警被手工清掉、下个周期探测恰好命中了 v4，都可能在故障未修复时产生「恢复」信号。恢复日志必须记录同一套探测的重跑结果与实际连通的地址（第 31 章的误报治理）。
7. **`unbound-control flush` 与负缓存 [RFC 2308]。** 权威修好后仍失败，第一反应应该是「缓存里是不是还留着旧的 NXDOMAIN / SERVFAIL」，而不是继续改配置。否定应答按 SOA minimum 缓存，会放大上游的瞬时故障；且链路上每个缓存实例都要刷。`flush` 有副作用，执行前先 `dump_cache` 留底。
8. **`+trace`、`drill -S`、`dnstracer` 看到的是「从本机重新迭代一遍」的结果**，不是递归解析器当时走的缓存路径。用它定位委派链断点可以，用它断言「Unbound 当时就是这么查的」不行。
9. **`nft list ruleset` 里写着，不等于运行时命中了。** 规则被后续 `nft -f` 冲掉、动态集合被清空、出向豁免被挪到链尾，文件里都还是「有」的。要看命中得靠 `counter`，要看运行时值得靠 `nft -a list chain ...` 与 `ss -lntup`。

一句话收束：**先确认这条命令回答的是哪一层的问题，再拿它的输出当证据。**

## 工具实装表

下表是本章实验环境的实测结果，不是各发行版的通用承诺。包名、版本与选项一律以所用发行版与官方手册为准，拿不准就写「见官方手册」，不要凭记忆写版本号。

| 工具 | 本机 | 另一台网关 | 备注 |
|---|---|---|---|
| `dig` / `host` / `nslookup` / `getent` / `tcpdump` | 有 | 有 | 基础工具，通常随系统或 `dnsutils` 一类包提供 |
| `delv` | 有 | 有 | BIND 系，自带信任锚做 DNSSEC 验签，能给出 bogus 原因 |
| `drill` | 有 | 无 | NLnet Labs / ldns，按发行版包名安装（`ldnsutils` 一类） |
| `dnstracer` | 有 | 无 | 逐级委派追踪；注意名字是 **dnstracer**，不是 dnstrace |
| `nft` / `unbound-control` | 有 | 需 `sudo -n` / 指定控制密钥路径 | 权限边界写清：只读查看与有副作用的 `flush` / `verbosity` 分开 |
| `resolvectl` | 无 | 无 | systemd-resolved 配套命令；未装时读 `/etc/resolv.conf` 与按接口配置，替代路径见发行版官方手册 |
| `tshark` | 视发行版 | 视发行版 | 「按发行版包名安装」；没装就用 `tcpdump -w` 存盘离线看 |
| `kdig` | — | — | **按用户决定忽略，不写进书**，也不列入命令示例 |

「有」只表示命令能跑起来，不代表该机器上启用了对应服务；`resolvectl` 未装也不代表 systemd-resolved 不存在，这两件事要分开。**工具缺失时不要改排查顺序**：第二层缺 `delv` 就用 `dig +dnssec` / `+cd` 组合加服务端日志，第三层缺 `tshark` 就用 `tcpdump` 存盘，第六层缺 `resolvectl` 就读配置文件。顺序是按层固定的，工具只是每层的可替换实现。

## 症状 → 第一条命令 → 判读 → 下一层 → 本书章节

最后给一张收尾决策表。用法是**从上往下按行对症状，每条只跑第一行命令**；判读成立就跳到「下一层」，不成立就继续看下一行，不要一上来把六层命令全跑一遍。

| 症状 | 第一条命令 | 判读 | 下一层 | 本书章节 |
|---|---|---|---|---|
| 某个应用报「DNS 挂了」，别的应用正常 | `getent ahosts api.example.net` 与 `cat /etc/nsswitch.conf` | NSS 路径或 `/etc/hosts` 命中不同，应用可能根本没走系统解析器 | ① → ⑤ | 第 3、24、29、32 章 |
| 双栈域名时通时断、日志出现「报故障 → 4 秒后恢复」 | `curl -v --noproxy '*' -4 https://api.example.net/healthz` 与 `-6` 对照 | v6 被 RST / v4 通，是地址族与探测只试前几项的问题 | ① → ⑤ | 第 19、22、31 章 |
| `dig` 有答案但应用仍失败 | `dig @192.0.2.53 api.example.net A` 与不带 `@` 的同名查询对照 | 指定服务器绕开了存根与 `local-zone`，两者答案不一致说明路径不同 | ① → ② | 第 3、24 章 |
| 全站 SERVFAIL，`+cd` 能拿到数据 | `delv @192.0.2.53 api.example.net A` | 验证链问题（信任锚、时钟、签名被剥离），不是数据缺失 | ② → ⑥ | 第 10、16 章 |
| 某个外部域解析不了，本级 NS 有一台答错 | `dnstracer -q a -r 3 api.example.net` | 委派链或单台权威异常，本机递归器未必有错 | ② → ③ | 第 6、28 章 |
| 出向查询无回包、无 RST、只有重传 | `tcpdump -ni any 'port 53'` | UDP 被静默丢，先排除本机规则劫持再怀疑上游 QoS | ③ → ④ | 第 14、22、31 章 |
| 权威已修好但客户端仍返回 NXDOMAIN | `unbound-control dump_cache \| grep api.example.net` | 负缓存 [RFC 2308] 未过期，链路上每个缓存实例都要刷 | ③ → ⑥ | 第 7、10 章 |
| 解析器「根区镜像不更新 / AXFR 失败」 | `nft -a list chain inet mangle output` | 本机解析器出向 53 被 tproxy 吃掉，需 uid 豁免在链首显式 accept | ④ → ③ | 第 14、22 章 |
| 规则文件里写着豁免，现象却像没生效 | `nft add rule inet mangle output udp dport 53 counter`（用完按 handle 删） | 计数器不递增 = 包没走到这条链；`nft -f` 重建可能已清空集合 | ④ → ① | 第 14、31 章 |
| `curl` 连接失败但 `dig` 正常，环境里配过代理 | `env \| grep -i proxy` 然后 `curl -v --noproxy '*' https://api.example.net/healthz` | 残留代理变量把请求送进死端口，DNS 根本没参与 | ⑤ → ④ | 第 23、32 章 |
| 用了 SOCKS 通道但本机仍看到 DNS 查询 | `curl -v --proxy socks5h://127.0.0.1:1080 https://api.example.net/healthz` | `socks5://` 本地解析、`socks5h://` 远程解析，归属开关没设对 | ⑤ → ① | 第 32 章 |
| `ping` 通、`dig` 通、DoT 端口连不上 | `openssl s_client -connect 192.0.2.53:853 -servername resolver.example.org` 与 `ss -lntup` | ICMP 与 DNS 与 TLS 是三条独立链路，逐项隔离 | ⑤ → ④ | 第 11、12 章 |
| 同一故障反复改配置都不见效 | `journalctl -u unbound --since '10 minutes ago' --no-pager` + `unbound-control stats_noreset` | 证据没按层归位，先用日志与计数确定断点在哪一层 | ⑥ → ① | 第 22、28、31 章 |

表里「下一层」写成 `① → ⑤` 这种形式，是为了强调排查不是单向流水线：确认第五层的应用行为后，常常要回到第一层重看解析路径。真正省时间的不是记住多少命令，而是每次只回答一个问题，并且知道**这条命令会骗你到哪一层为止**。

## 本章要点

1. 排查按六层顺序走，不按工具名字母排：谁在解析 → 解析器怎么答 → 抓到什么包 → 内核动过没 → 应用怎么连 → 归属与边界；每层只回答一个问题，`dig @server`、`getent`、`ping` 各自只覆盖其中一小段。
2. 章首案例的三条结论贯穿全章：探测要全量去重遍历（或 v4 优先），「网络已恢复」必须基于重探测结果，`getaddrinfo` 给的是 [RFC 6724] 策略排序而非可用性排序。
3. 命令会骗人是因为它回答了另一层的问题：`ping` 只测 ICMP、`curl` 默认吃代理变量、`nslookup` 与 `getent` 路径不同、`dig @server` 绕开本机存根、`+trace` 不等于递归器当时的缓存路径。先用 `--resolve` / `-4` / `-6` / `--noproxy '*'` 把变量逐项摘出方程。
4. 三种「不通」在抓包里形状不同：UDP 被静默丢（有出向无回包、只有重传）、被 RST / 拒绝、以及超时回 SERVFAIL；对应上游丢包、显式拒绝、验证或分片问题，改错了层会越改越乱。
5. **先看 nftables**：`nft -a` 带 handle 的临时 `counter` 探针用完按链名精确删除，`nft -f` 重建表会清空集合、`reset counters` 会破坏差分基线；`unbound-control flush` 与 [RFC 2308] 负缓存是「权威修好仍失败」的第一嫌疑，且链路上每个实例都要刷。
6. 工具实装表按实测写清「有 / 需装 / 未装 / 忽略」（`delv` 有，`drill` 与 `dnstracer` 需装，`resolvectl` 未装，`kdig` 按用户决定忽略）；工具缺失不改排查顺序，收尾决策表给出「症状 → 第一条命令 → 判读 → 下一层 → 本书章节」。

## 参考资料

- Unbound 官方手册，`unbound-control(8)` 的 `stats_noreset`、`lookup`、`dump_cache`、`flush`、`local_zone`、`verbosity`：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- Unbound 官方手册，`unbound.conf(5)`（`verbosity`、`log-servfail`、`dnstap`、`edns-buffer-size` 各节）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- NLnet Labs，Unbound 项目文档入口（`drill` / ldns 相关工具与包名以此为准）：https://nlnetlabs.nl/documentation/unbound/
- RFC 6724, *Default Address Selection for Internet Protocol Version 6 (IPv6)*（`getaddrinfo` 返回顺序与源地址选择）：https://www.rfc-editor.org/rfc/rfc6724
- RFC 2308, *Negative Caching of DNS Queries (DNS NCACHE)*（负缓存与 SOA minimum）：https://www.rfc-editor.org/rfc/rfc2308
- RFC 7766, *DNS Transport over TCP - Implementation Requirements*（TCP/53 与 `+tcp` 判读）：https://www.rfc-editor.org/rfc/rfc7766
- RFC 5737, *IPv4 Address Blocks Reserved for Documentation*（本章全部示例地址）：https://www.rfc-editor.org/rfc/rfc5737
- nftables 官方 wiki，规则集管理、`nft -f` 加载语义与 handle 用法：https://wiki.nftables.org/wiki-nftables/index.php/Main_Page

