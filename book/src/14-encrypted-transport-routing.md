# 加密通道选路与 fail-closed 工程

前几章把 DoT、DoH、QMIN、fail-closed 分别讲成了可独立的配置片段。这一章把它们放回**一条完整链路**里：客户端的 `:53` 被 nftables 劫持进本地解析器，解析器按国内/国外分流，转发器把查询送进加密隧道，隧道另一端的递归验证器继续向权威服务器迭代。任何一段的规则写错，表现都像「上游挂了」——所以本章的排查顺序是固定的：**先看 nftables 规则，再怀疑别的**。


## 整体链路：从客户端 :53 到远端递归验证器

```text
[LAN 客户端 / WG 对等端]
   │ ① 明文 :53（写死 DNS 的物联网设备、不理 RA 的存根解析器）
   ▼
[gw-01 策略层]  nft：hijack-dns 把 LAN/WG 的 :53 tproxy 到本地 :5353
   │             ★ 本机解析器进程（unbound / nsd）出向 53 按 uid 豁免
   ▼
[本地策略解析器 A]  过滤 / canary / DDR 压制 / 国内-国外分流
   │ ② 按域名选路：国内域 → 本地明文上游；其余 → 转发器 B
   ▼
[转发器 B]  单上游、forward-first: no、fail-closed
   │ ③ 加密隧道入口（本机回环 :5354 → SSH SOCKS / DTLS / DoT）
   ▼
[加密隧道]  TCP 承载优先（SSH SOCKS / DoT 853），UDP/QUIC 只在链路允许时
   │ ④ 跨境链路
   ▼
[cloud-01 递归验证器]  DNSSEC 验证 + QMIN + EDNS 1232
   │ ⑤ 明文 53 迭代查询（根 / TLD / 权威）
   ▼
[权威服务器]
```

这张图里有两个容易搞混的「加密」：③→④ 是**传输加密/隐私**，⑤ 的 DNSSEC 是**数据完整性/来源认证**。第 11、12 章强调过的边界在这里依然成立：整条链路里只有「客户端 ↔ 本地解析器」和「网关 ↔ 云主机」这两段能被我们加密，云主机向权威服务器发出的迭代查询通常仍是明文 53。想让根那一跳不出本机，走第 6 章的本地根区镜像。

```yaml
# /etc/unbound/policy.conf —— 本地策略解析器 A（节选，教学用示例地址）
server:
    interface: 127.0.0.1@5353
    access-control: 127.0.0.0/8 allow
    access-control: 192.0.2.0/24 allow
    access-control: fd00::/8 allow
    access-control: 0.0.0.0/0 refuse

    edns-buffer-size: 1232
    max-udp-size: 1232
    qname-minimisation: yes
    qname-minimisation-strict: no
    harden-glue: yes
    local-zone: "_dns.resolver.arpa." always_nodata
    local-zone: "use-application-dns.net." always_nxdomain

# 国内域走本地上游（教学示例：198.51.100.53）
forward-zone:
    name: "cn.example."
    forward-addr: 198.51.100.53
    forward-first: no

# 其余走转发器 B（本机回环），B 再进加密隧道
forward-zone:
    name: "."
    forward-addr: 127.0.0.1@5354
    forward-first: no
    do-not-query-localhost: no
```

**为什么这么设**：A 只做策略与过滤，把「是否出隧道」的决定集中在一处；`forward-first: no` 保证 SERVFAIL 时不会逃出该区自行递归（第 13 章的 fail-closed）。`do-not-query-localhost: no` 只在「本机另一实例确实是设计中的下一跳」时才放开，且必须保证端口、pidfile、控制接口不重叠、拓扑不成环（第 5、7 章）。canary 与 DDR 两条压在 A 上，是因为**客户端的 DNS 首先到 A**；压在 B 上就晚了。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/policy.conf
dig @127.0.0.1 -p 5353 www.cn.example. A +short
dig @127.0.0.1 -p 5353 example.org A +short
unbound-control -c /etc/unbound/policy.conf list_forwards
```

**失败模式 / 回滚点**：`unbound-checkconf` 对 `forward-addr` 指向回环且 `do-not-query-localhost: yes` 时会给出警告「地址不会被用于查询」——不会报错，但转发会静默失败，表现为全量 SERVFAIL。这是最容易被忽略的一条。回滚：明确写 `do-not-query-localhost: no`，并确认 A 与 B 的端口不重叠。

## nft 规则：劫持 :53 与 uid 豁免

### 完整规则示例

```bash
#!/usr/sbin/nft -f
# /etc/nftables.conf —— 教学用示例地址段，勿直接用于生产
table inet mangle {
    set domestic-dns {
        type ipv4_addr
        flags interval
        elements = { 198.51.100.53 }
    }

    chain hijack-dns {
        type nat hook prerouting priority dstnat; policy accept;
        # LAN 与 WG 网段里写死 :53 的流量，tproxy 到本地策略解析器
        iifname { "lan0", "wg0" } udp dport 53 \
            meta mark set 0x2333 tproxy ip to 127.0.0.1:5353 counter accept
        iifname { "lan0", "wg0" } tcp dport 53 \
            meta mark set 0x2333 tproxy ip to 127.0.0.1:5353 counter accept
    }

    chain output-tproxy {
        type route hook output priority mangle; policy accept;

        # ★ 链首：WG 自己的 udp sport 51820 出包必须显式放行（见 14.5）
        udp sport 51820 counter accept

        # ★ 本机解析器进程出向 53 按 uid 豁免，否则自己吃掉自己的递归查询
        meta skuid unbound udp dport 53 counter accept
        meta skuid unbound tcp dport 53 counter accept
        meta skuid nsd udp dport 53 counter accept

        # 需要走代理的流量：打标后交给策略路由（教学示例：目的为文档地址段）
        ip daddr 203.0.113.0/24 tcp dport { 22, 853, 443 } \
            meta mark set 0x2332 counter accept
    }
}

table inet filter {
    chain output {
        type filter hook output priority filter; policy accept;
        # fail-closed 演练用：临时 drop 853（14.4 节），平时保持注释
        # tcp dport 853 counter drop
    }
}
```

**为什么这么设**：`hijack-dns` 用 `tproxy` 把 LAN/WG 的 53 交给本地 5353，客户端无需改任何配置；`output-tproxy` 处理**本机自身发出的包**，两条 `meta skuid` 豁免在第 5、7 章已作为结论给出——不豁免的话，Unbound 自己向权威服务器发的递归查询会被自己的 hijack 吃掉，表现为根区镜像永远不更新、AXFR 失败。`udp sport 51820` 的链首 accept 是 14.5 节的坑，必须放在打标规则**之前**（链尾规则会连它一起打标）。

**怎么验证**：

```bash
nft -c -f /etc/nftables.conf          # 只检查语法，不加载
nft -f /etc/nftables.conf
nft list ruleset | sed -n '1,120p'
nft list chain inet mangle output-tproxy
nft reset counters
# 触发流量后再看计数器
dig @127.0.0.1 -p 5353 example.org A +short
nft list chain inet mangle output-tproxy
```

**失败模式 / 回滚点**：`nft -f` 是**重建表**，会清空集合（见 14.3）；`meta skuid unbound` 要求系统里确实存在该用户，写成别的名字等于没写。回滚：改动前 `nft list ruleset > /etc/nftables.conf.bak`，出问题时 `nft -f /etc/nftables.conf.bak`。

### 排查顺序：先看 nftables

「根区镜像不更新」「AXFR 失败」「WG 握手不通」「上游看起来全挂」这四个现象里，至少三个的根因在规则表，不在 Unbound 配置。固定排查顺序：

```bash
# ① 规则与集合是不是还在
nft list ruleset | sed -n '1,120p'
nft list chain inet mangle hijack-dns
nft list chain inet mangle output-tproxy

# ② uid 豁免有没有命中（本机解析器出向 53 不应被 tproxy）
nft list ruleset | grep -i skuid
nft reset counters && dig @127.0.0.1 -p 5353 example.org A +short
nft list ruleset | grep -i -E 'counter|packets' | head -20

# ③ 抓包看包究竟去了哪里（mark 是否被打上）
tcpdump -ni any 'udp port 53 or tcp port 853 or udp port 51820' -c 30
```

**为什么这么设**：把「配置写了」与「规则命中了」分开。计数器是唯一能证明包实际走过哪条规则的证据；抓包用来确认打标后是否真的进了隧道。

**怎么验证**：LAN 客户端查询使 `hijack-dns` 计数增加；本机解析器出向 53 的计数**不**增加；`udp sport 51820` 在 `output-tproxy` 链首有 accept 且计数增加。

**失败模式 / 回滚点**：`nft reset counters` 会清零所有计数，排查前先记录基线。回滚：优先恢复备份规则集，而不是逐条删规则——逐条删容易留下半截状态。

## `nft -f` 重建表会清空集合

一个很实际但很少写进文档的坑：`nft -f file` 加载规则文件时，如果文件里定义了 `table inet mangle { ... }` 且包含 `set`，它是按文件内容**重建**这张表，而不是增量合并。这意味着：

- 通过 `nft add element inet mangle domestic-dns { 203.0.113.7 }` 在运行时往集合里加的地址，加载文件后**全部消失**；
- 由脚本（DDNS 更新、策略域名列表）动态维护的集合，加载后回到文件里写的那几个初始值；
- 计数器、quota 等有状态元素同样被重置。

```bash
# 危险做法：直接 nft -f 覆盖，动态集合会被清空
nft -f /etc/nftables.conf

# 安全做法：加载前导出含运行时集合的规则集，加载后补齐
nft list ruleset > /var/tmp/nft-before-lab.txt
nft -f /etc/nftables.conf
nft add element inet mangle domestic-dns { 203.0.113.7 }
nft list table inet mangle | grep -A5 'set domestic-dns'
```

**为什么这么设**：把「补齐」写成加载流程的一部分，而不是事后想起来。脚本化时应把动态集合的初始内容写进一个单独的 `.nft` 片段，并在主文件加载后统一 `nft -f` 追加，避免依赖人工记忆。

**怎么验证**：加载前后各跑一次 `nft list table inet mangle`，比对 `elements = { ... }`；再触发一条针对动态成员地址的查询，确认命中对应规则。

**失败模式 / 回滚点**：现象通常是「重启后分流突然只对几个默认域生效」「策略路由部分失效」，很容易被误判成上游或 DDNS 脚本坏了。回滚：`nft -f /var/tmp/nft-before-lab.txt`（导出的规则集包含当时的集合内容），并把动态集合的维护脚本纳入开机流程。

## 跨境链路实测：为什么加密通道优先 TCP

这一节给出本项目在「一台家庭网关 + 一台境外云主机的实验环境」里的实测结论。同一条跨境链路上，用一个 1 MB 的测试文件对比两种承载：

| 承载 | 传输层 | 实测吞吐 | 现象 |
|---|---|---|---|
| UDP / QUIC 下载 | UDP | ~300 B/s | 1 MB 要跑很久，丢包与限速叠加，几乎不可用 |
| SSH SOCKS（TCP 承载） | TCP | ~660 KB/s | 同一链路同一时段，稳定在数百 KB/s |

差距约三个数量级。机理不是「TCP 比 UDP 快」，而是这条链路对 UDP 做了明显的 QoS / 限速：UDP 没有拥塞控制反馈，被策略性丢弃后只能靠上层重试，速率塌到几百字节每秒；TCP 有拥塞控制与重传，能收敛到一个可用速率。**DNS 隧道同理**：把 DNS 查询装进 UDP/QUIC 上跑，丢包后要等超时重试，客户端表现为「解析时好时坏、慢得离谱」；装进 TCP 承载的通道（SSH SOCKS、DTLS 之外的 TLS 流、DoT 853 本身就是 TCP），反而稳。

```bash
# 教学示例：用 SSH SOCKS 把转发器的出向流量塞进 TCP 隧道
ssh -N -D 127.0.0.1:1080 -p 22 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    user@203.0.113.10

# 转发器侧：只用 TCP 出向，避免掉进 UDP 被 QoS 的路径
# /etc/unbound/exit.conf
#   server:
#     tcp-upstream: yes
#     forward-zone:
#       name: "."
#       forward-addr: 192.0.2.53@853#dns.example.org
#       forward-tls-upstream: yes
#       forward-first: no
```

**为什么这么设**：`tcp-upstream: yes` 让 Unbound 向权威/上游侧只用 TCP（官方手册称其「useful in tunneling scenarios」）；DoT 853 天然是 TCP/TLS，两者叠加后整条出向路径没有 UDP 承载的 DNS 报文。`ServerAliveInterval` / `ExitOnForwardFailure` 保证隧道挂掉时快速失败而不是静默卡住，配合 fail-closed 才能在两波失败后明确 SERVFAIL。

**怎么验证**：

```bash
# 吞吐对照：同一 1MB 文件，两种承载各测一次（教学示例命令形态）
dd if=/dev/urandom of=/tmp/test1m.bin bs=1M count=1
curl -sS -o /dev/null -w 'socks=%{speed_download} B/s\n' \
  --socks5-hostname 127.0.0.1:1080 --max-time 60 \
  https://dns.example.org/test1m.bin
# 断链演练：隧道断开后 DNS 必须 SERVFAIL，不能静默走明文
pkill -f 'ssh -N -D 127.0.0.1:1080' || true
dig @127.0.0.1 -p 5353 example.org A | grep 'status:'
tcpdump -ni any 'udp port 53' -c 10
```

期望 `status: SERVFAIL`，抓包里没有明文 53 出向；`curl` 的 `speed_download` 用来对照 TCP 通道的可用速率。

**失败模式 / 回滚点**：`tcp-upstream: yes` 是全局开关，会同时影响本地国内域的解析（如果 A 也用它）；更稳的做法是把「出隧道的那一段」限定在 B 的配置里（第 5 章的 `forward-tcp-upstream` 可按 zone 设置）。另一个坑是 SSH SOCKS 只代理 TCP，DNS 若还有 UDP 段（例如 A 的国内上游）照旧走明文，别把「开了 SOCKS」当成「全链路加密」。回滚：恢复 `tcp-upstream: no` 的配置快照，并重新拉起隧道。

### fail-closed 在整条链路上怎么落地

第 13 章给的是单点的 fail-closed，这里补链路版：

```text
① 客户端 :53 → A              nft 劫持；A 自身出向 53 按 uid 豁免
② A → 国内上游 / B             两条 forward-zone 都 forward-first: no
③ B → 加密隧道                 单上游、forward-tls-upstream: yes、#认证名
④ 隧道断开                     两波都失败 → SERVFAIL，绝不回退明文 53
⑤ 兜底配置                     fallback 定义了但无人引用也算安全（第 13 章）
```

```yaml
# /etc/unbound/exit.conf —— 转发器 B：唯一出隧道的出口
server:
    interface: 127.0.0.1@5354
    access-control: 127.0.0.0/8 allow
    do-not-query-localhost: yes
    tcp-upstream: yes
    tls-cert-bundle: "/etc/ssl/certs/ca-certificates.crt"
    edns-buffer-size: 1232
    qname-minimisation: yes
    qname-minimisation-strict: no

forward-zone:
    name: "."
    forward-addr: 192.0.2.53@853#dns.example.org
    forward-tls-upstream: yes
    forward-first: no
```

**为什么这么设**：B 只有一个上游、只有一个出口，切换交给外部巡检脚本（第 5 章）；`forward-first: no` 把「隧道断了」变成可见的 SERVFAIL。`edns-buffer-size: 1232` 是第 11 章 IPv6 最小 MTU 1280 的推导结果，套上 WireGuard 后 MTU 更小，1472 会分片被丢。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/exit.conf
unbound-control -c /etc/unbound/exit.conf list_forwards
# 断链演练：drop 853，观察 SERVFAIL 与抓包
nft add rule inet filter output tcp dport 853 counter drop
dig @127.0.0.1 -p 5353 example.org A | grep 'status:'
tcpdump -ni any 'tcp port 853 or udp port 53 or tcp port 53' -c 20
nft flush chain inet filter output
```

**失败模式 / 回滚点**：演练时 `nft` 规则务必恢复（先 `nft list ruleset > /etc/nftables.conf.bak`）。若出现「慢一点但仍返回答案」，检查 A 的 `forward-first` 是否被别的文件覆盖、以及有没有第二条 `forward-addr`。

## 真实形态的坑：WG 回包被 `output-tproxy` 吃掉

这是策略网关里最反直觉的一个故障。现象是：**WireGuard 隧道起不来**，握手一直不完成；但 `wg show` 里能看到端点地址、配置看起来完全正确，云主机侧也没收到握手包。

### 因果链

```text
① 策略网关的 output-tproxy 链尾有一条兜底打标规则：meta mark set 0x2332
② 这条规则本意是「本机发出的、需要走代理的流量打标后交给策略路由」
③ 但 WG 自己的 udp sport 51820 出包也会经过 output 链
④ 于是 WG 发出的握手 / 响应包被打上 0x2332 → 被送进代理
⑤ 代理路径不会把它送回云主机的 WG 端点 → 对端收不到握手响应
⑥ 隧道起不来；此时任何「DNS 走隧道」的设计都无从谈起
```

第一反应通常是加一条 `meta skuid 0 accept` 放行「内核发出的包」，因为直觉上内核没有用户态 uid。**这个匹配不到**：`meta skuid` 只匹配用户态进程的 socket owner，WG 在内核态发出的这类包没有对应的 skuid 元数据，链尾的 mark 规则照样命中。

### 修法：链首按 sport 显式 accept

```bash
#!/usr/sbin/nft -f
# /etc/nftables.d/10-wg-exempt.nft —— 教学示例
table inet mangle {
    chain output-tproxy {
        # 必须放在所有 mark / tproxy 规则之前
        udp sport 51820 counter accept
        # 若隧道还承载其它端口，一并在这里列出
    }
}
```

**为什么这么设**：`udp sport 51820` 是 WG 的监听端口，出包的源端口就是它，这个字段在包里一定存在、与发包主体是内核还是进程无关。`accept` 会终止本链后续规则，包不再被打标、不再进 tproxy。关键在**链首**：nftables 链内规则按添加顺序匹配，链尾的兜底 mark 会命中任何走到那里的包。

**怎么验证**：

```bash
nft list chain inet mangle output-tproxy | sed -n '1,40p'
# 确认 sport accept 在 mark 之前
nft list chain inet mangle output-tproxy | grep -n -E '51820|mark set'
# 触发握手并观察计数器与对端
wg show wg0 latest-handshakes
nft reset counters && wg show wg0 peers | head
nft list chain inet mangle output-tproxy | grep -A1 '51820'
tcpdump -ni any 'udp port 51820' -c 20
```

期望：`grep -n` 里 `51820` 的行号**小于** `mark set` 的行号；`latest-handshakes` 出现近期时间戳；抓包能看到双向的 51820 流量。

**失败模式 / 回滚点**：两种常见复发方式——① 有人把「优化后的规则文件」重新 `nft -f` 加载，链首 accept 被冲掉（14.3 节）；② 换了 WG 端口却没改规则。回滚：`nft -f /etc/nftables.conf.bak`，并把这条 accept 写进主规则文件而不是临时命令。另一个误判来源是「以为是云主机防火墙没放行 51820」，结果在云主机侧查了半天——按 14.2.2 的顺序，先看网关自己的 nftables。

### 与 uid 豁免的对照

| 豁免对象 | 匹配方式 | 原因 |
|---|---|---|
| 本机解析器（unbound / nsd）出向 53 | `meta skuid unbound` / `meta skuid nsd` | 用户态进程，有 socket owner |
| WG 内核发出的 udp sport 51820 | `udp sport 51820` | 内核发包，`meta skuid 0` 匹配不到 |

**为什么这么设**：把两条并列写清楚，避免把「按 uid 豁免」当成通用方案。前者管的是「别让解析器吃掉自己的递归查询」（14.2.1），后者管的是「别让隧道自己的握手包被送进代理」，两者都是链首/链首附近的 accept，但匹配字段完全不同。

**怎么验证**：`nft list ruleset | grep -i -E 'skuid|51820'` 能同时看到两类规则；计数器上，解析器出向 53 不进 tproxy、WG 出包不打标。

**失败模式 / 回滚点**：如果为了图省事写了 `udp dport 53 accept` 而不带 `meta skuid`，等于把 LAN 客户端的 53 也放行了，劫持策略失效。回滚：恢复带 `meta skuid` 的精确豁免。

## 一张验收清单

| 检查项 | 期望 | 命令 |
|---|---|---|
| 规则语法 | `nft -c -f` 无错误 | `nft -c -f /etc/nftables.conf` |
| 劫持生效 | LAN :53 进本地 5353 | `nft list chain inet mangle hijack-dns` + 计数器 |
| uid 豁免 | 解析器出向 53 不进 tproxy | `nft list ruleset \| grep skuid` + 计数器 |
| WG 豁免 | `sport 51820` 在链首、mark 之前 | `nft list chain inet mangle output-tproxy \| grep -n` |
| 集合完整 | `-f` 加载后动态成员补齐 | `nft list table inet mangle` 前后比对 |
| 单上游 | A / B 每个 zone 一条 | `unbound-control -c ... list_forwards` |
| fail-closed | 断 853 / 隧道后 SERVFAIL | 断链演练 + `tcpdump` |
| 无明文出向 | 抓包无 `dst port 53` | `tcpdump -ni any 'udp port 53 or tcp port 53'` |
| 优先 TCP | 出向走 853 / SOCKS，不走 UDP/QUIC | 吞吐对照 + `curl --socks5-hostname` |
| MTU | 出向 EDNS 1232 | `dig +bufsize=4096 ... \| grep udp:` |
| 回滚可用 | 规则集备份存在且可加载 | `nft -c -f /etc/nftables.conf.bak` |

## 本章要点

- 完整链路是「客户端 :53 → nft 劫持 → 本地策略解析器 A（分流 / canary / DDR 压制）→ 转发器 B → 加密隧道（SSH SOCKS / DTLS / DoT）→ 远端递归验证器 → 权威服务器」；**加密传输只覆盖其中几段**，远端向权威发出的迭代查询通常仍是明文 53，DNSSEC 提供的是数据完整性/来源认证，两者不互相替代。
- nft `hijack-dns` 用 tproxy 把 LAN/WG 的 53 送进本地解析器；**本机解析器进程（unbound、nsd）的出向 53 必须按 `meta skuid` 豁免**，否则自己的递归查询被自己的劫持吃掉，表现为根区镜像不更新、AXFR 失败。
- **`nft -f` 是重建表，会清空集合**：运行时用 `nft add element` 加的地址、计数器、动态策略列表加载后全部消失，必须在加载流程里补齐，并把导出 `nft list ruleset` 作为回滚基线。排查网络故障时**先看 nftables 规则，再怀疑别的**。
- 跨境实测：同一链路 UDP/QUIC 下载 1 MB 只到 ~300 B/s，而 TCP 承载的 SSH SOCKS 达 ~660 KB/s，所以对 UDP 做 QoS 的网络里**加密通道优先 TCP**（`tcp-upstream: yes`、DoT 853、SSH SOCKS）。fail-closed 要求隧道断开两波都失败就 SERVFAIL，禁止静默明文降级；单上游是策略唯一的断言，fallback 定义了但无人引用也算安全。
- 策略网关 `output-tproxy` 链尾的 `meta mark set 0x2332` 会连 WireGuard 自己的 `udp sport 51820` 出包一起打标送进代理，导致握手响应到不了对端、隧道起不来；`meta skuid 0` 匹配不到内核发出的这类包，**必须在链首按 `udp sport 51820` 显式 accept**，且 accept 要排在 mark 规则之前。

## 参考资料

- nftables 官方 wiki，Setting packet metainformation（`meta mark` / `meta skuid`）：https://wiki.nftables.org/wiki-nftables/index.php/Setting_packet_metainformation
- nftables 官方 wiki，规则集管理与 `nft -f` 加载语义：https://wiki.nftables.org/wiki-nftables/index.php/Main_Page
- Unbound 官方手册，`unbound.conf(5)` 的 `tcp-upstream`、`forward-tcp-upstream`、`do-not-query-localhost`、`forward-first` 与 `edns-buffer-size`：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound-control(8)` 的 `list_forwards` 与统计命令：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- NLnet Labs，Unbound 项目文档入口：https://nlnetlabs.nl/documentation/unbound/
- RFC 7858，*Specification for DNS over Transport Layer Security (TLS)*：https://www.rfc-editor.org/rfc/rfc7858
- RFC 7766，*DNS Transport over TCP - Implementation Requirements*：https://www.rfc-editor.org/rfc/rfc7766
- RFC 5737，*IPv4 Address Blocks Reserved for Documentation*：https://www.rfc-editor.org/rfc/rfc5737
- WireGuard 官方手册（端点端口与握手）：https://www.wireguard.com/quickstart/
