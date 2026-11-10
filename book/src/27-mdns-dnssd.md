# 单播 DNS 与多播 DNS：从 mDNS 到 DNS-SD

一台机器在同一个网卡上其实同时开着两套名字系统：一套走 UDP/TCP 53，由递归解析器与权威服务器分工完成；另一套走 UDP 5353，只在同一段链路里喊话。它们共用 DNS 的报文格式，却不是同一条解析路径。第 9 章讲最小暴露面时，隐含前提之一是「名字查询都会经过 Unbound」；这一章要把这个前提拆开：**`.local` 那类名字根本不会到 Unbound 这里来**。

这里只讲部署边界：谁在哪个通道里应答、跨链路时该换哪条通道、以及企业网络里最常见的三种「时通时不通」。



## 两条互不相交的解析通道

### 一台网关上到底谁在监听

| 通道 | 端口 | 谁监听 | 谁应答 | 名字长什么样 |
|---|---|---|---|---|
| 单播 DNS | UDP/TCP 53 | Unbound（或转发器） | 递归解析器 / 权威服务器 | `gw-01.home.arpa.`、`dns.example.org.` |
| 多播 DNS | UDP 5353 组播 | 存根解析器（Avahi / systemd-resolved / mDNSResponder） | 链路上拥有该名字的那台主机 | `gw-01.local.`、`_demo._tcp.local.` |
| 加密传输 | 853 / 443 | Unbound 的 DoT / DoH 端点 | 递归解析器 | 与单播 DNS 同一命名空间 |

这张表的重点不是端口，而是**「谁应答」这一列换了人**：单播侧应答者是一台被配置出来的服务器，多播侧应答者是名字的拥有者本人。前者可以集中加固、集中观测，后者不能。

### Unbound 的位置：它不在多播通道里

**Unbound 不参与 mDNS**：它是单播递归验证器，不加入 `224.0.0.251` / `FF02::FB` 组播组，不监听 UDP 5353 上的多播查询，也不为 `.local` 名字生成应答。第 25 章已经说过 Unbound 是递归验证器而不是权威服务器，`local-zone` / `local-data` 只是「非常有限的权威服务」；这个定位在多播场景里更明显——多播应答由「链路上拥有这个名字的那台主机自己」发出，与递归、缓存、转发全无关系。

把两条通道分开的**不是 Unbound**，而是存根解析器（stub resolver）：Linux 上常见 systemd-resolved 或 Avahi， macOS / Windows 侧常见 mDNSResponder。存根解析器决定「这个名字该往 53 单播发，还是往 5353 多播发」，Unbound 只收到前者。换句话说，分流发生在客户端，发生在查询离开网卡之前。

一条能立刻看清边界的命令：

```bash
ss -lunp | grep -E '(:53|:5353)'
```

预期里 `:5353` 的持有者是 avahi-daemon 或 systemd-resolved，`:53` 的持有者是 Unbound；**不应该出现 Unbound 占着 5353 多播**。若只看到 53 而没有 5353，说明本机没有 mDNS 应答者，`.local` 查询会整条落到单播侧，随后被当作一个不存在的顶级域处理。

## mDNS：只在本链路里成立的名字

### 组播地址、端口与 TTL=1

mDNS [RFC 6762] 的固定参数很少，但每一个都是部署边界：

| 项 | 取值 | 部署含义 |
|---|---|---|
| IPv4 组播地址 | `224.0.0.251` | 属于 `224.0.0.0/24` 本地网络控制块 |
| IPv6 组播地址 | `FF02::FB` | 链路本地范围 |
| 传输 | UDP 5353 | 与单播 53 完全分离 |
| IPv4 TTL | 1 | 路由器不转发；`224.0.0.0/24` 的报文天然不出本链路 |
| 专用后缀 | `.local` | 专用于本链路，不是可全球解析的顶级域 |

TTL=1 是最容易被误当成「配置没调好」的一条：隔了两个子网之后发现不了设备，**不是交换机没开组播、也不是防火墙漏了端口**，而是 mDNS 按定义就只在一条链路上有效。想跨子网，只能换到单播 DNS-SD（见后文），不是想办法把多播「打通」。把时间和精力花在调 IGMP / MLD snooping 上，方向就错了。

`.local` 被 [RFC 6762] 留给 mDNS，这带来一个长期运维坑：企业若把内部单播 DNS 区也叫 `.local`，存根解析器就可能在两条通道之间摇摆，表现为时通时不通。给家庭与小型内网用的名字是 `home.arpa.` [RFC 8375]，后面单独讲。

### wire format 复用，语义不复用

mDNS 报文复用 DNS 的 wire format，所以 tcpdump / Wireshark 能直接解出来；但排障时若按单播 DNS 的语义去读这些位，几乎每个结论都会错。下面这张表是**部署排障用的对照**，不是条文复述：

| 字段 | 单播 DNS 的习惯 | mDNS 多播查询 / 应答 |
|---|---|---|
| Message ID | 每查询一个随机数，用来配对请求与应答 | 恒 0；多播应答不靠 ID 配对 |
| AA | 权威服务器才置 1 | 应答恒 1——每个应答者对自己名字都是权威 |
| RD / RA | 存根置 RD=1 请求递归，递归器回 RA=1 | 恒 0 / 0，**mDNS 不做递归** |
| RCODE | NXDOMAIN / SERVFAIL 等承载错误 | 恒 0；「没这个名字」表现为「没人应答」 |
| OPCODE | 可承载 UPDATE 等非查询操作 | 非 0 的 OPCODE 静默忽略，不应答也不报错 |
| TC | 应答放不下时置位，提示改用 TCP | 在**查询**里置位表示 Known-Answer 列表还有后续包 |

几个直接能用的排障推论：

- 抓包里看到 5353 上的应答没有对应 ID，别当错包；ID 恒 0 是规定动作。
- 拿 `dig` 的思路去等 NXDOMAIN 是等不到的：mDNS 里名字不存在 = 静默无应答，客户端靠超时收尾。
- 若看到 RD=1 的 5353 包，多半是某个实现把单播查询误投到多播口，或者两条通道被混在了同一条链路上。
- 应答者不会替你去问别人；「这个名字可能在别处」在 mDNS 里不成立，缓存由存根解析器自己维护。
- TC 位在查询里的含义与单播完全不同：看到它应该去找后续包里的 Known-Answer 记录，而不是准备切 TCP。
- AA=1 不代表链路上有一台权威服务器，只代表「发包的这台主机拥有它应答的名字」，别据此画拓扑。

### probing、announcing 与 goodbye

一台主机想用 `gw-01.local.` 这个名字，不是直接开喊，而是先 **probing**：连续发出若干查询，问「有没有人已经在用这个名字」，同时监听是否有别人应答；确认无冲突后才 **announcing**，把 PTR / SRV / TXT / A / AAAA 记录主动多播出去（通常不止发一次，让链路上各缓存都收到）。运行中若发现同名冲突，按规则退让并重新选名。

服务正常下线时发 **goodbye**：把该记录的 TTL 置 0 后多播出去，等于告诉邻居「立刻把这条从缓存里删掉」。

部署上要注意 goodbye 的脆弱性：它是尽力而为的通告，没有确认、没有重传保证。主机断电、网线被拔、无线漫游的瞬间都发不出来，邻居缓存里就会残留一条已经不存在的记录，直到本地缓存老化。清理只能靠存根解析器侧的缓存超时，**不要指望 Unbound 帮 mDNS 刷缓存**——Unbound 从没收到过这些记录。这也是第 7 章讲的「链路上每一层缓存都要刷」在这里失效的原因：这一层根本不在 Unbound 的缓存里。

### 别把它叫成 LLMNR

LLMNR（Link-Local Multicast Name Resolution）是另一套链路本地名字解析协议，与 mDNS 不是同一个东西：端口不同、报文格式不同、服务发现模型也不同。运维里最常见的误操作是「关掉 LLMNR 就等于关掉 mDNS」，或者反过来。systemd-resolved 里这两个开关本来就是分开的（后面的配置块会把 `LLMNR=no` 与 `MulticastDNS=yes` 同时写出来），排查时也要分别看：

```bash
resolvectl status | grep -iE 'MulticastDNS|LLMNR'
```

若输出里 `MulticastDNS` 是 no 而 `LLMNR` 是 yes，那么关 LLMNR 对 `.local` 一点帮助都没有；反之亦然。把两者混为一谈，最典型的后果是「明明按文档关了链路本地解析，打印机还是能被发现」，然后开始在错误的方向上查防火墙。

## DNS-SD：三件套，以及它并不依赖 mDNS

### PTR / SRV / TXT 各管什么

DNS-SD [RFC 6763] 定义的是「怎么用 DNS 记录描述一个服务」，不是「怎么发多播包」。三件套分工固定：

| 记录 | 回答的问题 | 查询形态 |
|---|---|---|
| PTR | 这类服务有哪些实例？ | `_demo._tcp.example.org. PTR` → `Demo Service._demo._tcp.example.org.` |
| SRV | 这个实例跑在哪台主机、哪个端口？ | `Demo Service._demo._tcp.example.org. SRV` → `edge-01.example.org.` + 端口 |
| TXT | 实例有哪些附加参数？ | 同上名的 TXT，键=值文本，例如 `note=demo` |

之后才用 A / AAAA 把 `edge-01.example.org.` 解析成地址。注意这套记录**完全可以用普通的单播 DNS 区承载**：把 `_demo._tcp.example.org.` 的 PTR、SRV、TXT 写进权威区，客户端用常规 `dig` 就能完成服务发现，全程不碰 5353。这一点是理解后面三种跨链路方案的前提。

### 关键一句：DNS-SD 不依赖 mDNS

**DNS-SD 不依赖 mDNS，mDNS 也不是 DNS-SD 的唯一承载方式。** 把两者绑成「Bonjour = mDNS + DNS-SD」是家庭网络里最常见的直觉，但在企业网里恰恰要拆开：

- 单链路、零配置：DNS-SD 记录由 mDNS 多播分发，最省事，也最不可控。
- 多子网、要审计、要权限：同一套 PTR/SRV/TXT 记录放进单播权威区，就是 Wide-Area DNS-SD。
- 设备多、电池供电、无线多播代价高：换成 SRP 的纯单播注册。

三种场景下记录形状是一样的，变的只有**传输与注册方式**。所以排障时先问「这条 PTR 是从哪来的」，比先问「组播通不通」有用得多。

### Wide-Area DNS-SD：把发现搬回单播

Wide-Area DNS-SD 用 [RFC 2136] 的 DNS `UPDATE` 报文注册服务，并用 TSIG 做认证（TSIG 规范编号见官方资料，按名称在 RFC Editor 检索即可）。浏览侧仍然是普通的 PTR / SRV / TXT 单播查询，走常规递归解析器，因此能跨子网、能进日志、能按区做访问控制。

对本书的架构来说，这条路径最自然：内部权威区由 NSD 或微软 DNS 之类持有（第 25 章的三条路径），Unbound 用 `stub-zone` / `forward-zone` 指过去，只做单播递归与验证。但**Unbound 自己不接受 `UPDATE` 报文**——[RFC 2136] 动态更新是权威服务器的能力，Unbound 没有。注册必须由客户端或 DHCP 脚本直接发给权威服务器，别把 `UPDATE` 往 Unbound 的 53 口发。

### Discovery Proxy：跨链路的翻译器

Discovery Proxy [RFC 8766] 解决的是「设备只会多播、客户端在另一个子网」这个过渡问题：代理身处设备所在的链路，替单播客户端发多播查询，把收到的多播应答整理成单播 DNS-SD 记录再回给客户端。它本质上是一个**通道翻译器**，不是新的名字系统，也不是中继转发器——它输出的是可被单播缓存、可被递归的标准 DNS-SD 记录。

部署边界：代理必须在每条需要被发现的链路上都有一个「腿」，单播侧则可以共用一套权威区与递归器。它适合打印机、摄像头这类改不了协议栈的设备；新设备应优先直接走 SRP 或 Wide-Area 注册，少一层状态。

### SRP：为 Wi-Fi 与低功耗网络做的纯单播注册

SRP [RFC 9665]（Service Registration Protocol）是**纯单播**的 DNS-SD 注册协议：设备用 DNS Update 把 PTR / SRV / TXT 提交给 SRP 注册服务器，认证用 SIG(0) 公钥而不是共享密钥，命名采用 FCFS（First Come, First Served，先到先得）规则来处理同名冲突。设计动机很直接：在 Wi-Fi 与低功耗、电池供电的网络里，多播收发既费电又容易被无线侧丢弃或限速，与其优化多播，不如彻底不发多播。

对部署的含义：设备侧没有 probing / announcing 那一套，名字冲突由注册服务器按 FCFS 裁定，浏览侧仍是单播 DNS-SD。于是「跨子网发现」不再需要打通多播，也不需要每条链路放一个代理，规模可以上到大量终端。密钥分发与 FCFS 命名策略是新的运维面，别把它当成零配置方案。

### LLQ：见 RFC Editor 索引

长轮询（Long-Lived Queries，LLQ）用于让单播客户端及时知道服务记录的变化，避免反复轮询。编号与当前状态**见 RFC Editor 索引 / 按名称在 RFC Editor 检索**，本章不写具体编号与条款；部署时优先确认自己用的注册服务器与客户端是否都支持该机制，而不是假定它默认可用。

## 四维对照表

传输、作用域、注册方式、递归、谁应答、适用规模，把这四种方案一次摆平：

| 方案 | 传输 | 作用域 | 注册方式 | 递归 | 谁应答 | 适用规模 |
|---|---|---|---|---|---|---|
| 单播 DNS | UDP/TCP 53（可承载 DoT / DoH / DoQ） | 路由可达即全网 | zonefile、[RFC 2136] UPDATE + TSIG，或资产清单生成 `local-data` | 有，递归解析器逐级查询 | 权威服务器 + 递归解析器 | 单台到全球 anycast 集群（第 26 章） |
| mDNS | UDP 5353 组播 `224.0.0.251` / `FF02::FB`，TTL=1 | 单一链路 | 主机自行 probing / announcing，goodbye 用 TTL=0 | **无**，RD/RA=0 | 链路上拥有该名字的那台主机 | 一个广播域，几十台以内 |
| Wide-Area DNS-SD | 单播 UDP/TCP 53 | 跨子网、跨园区 | [RFC 2136] UPDATE + TSIG 写入权威区 | 有，走常规单播递归 | 单播权威服务器（可叠加 Discovery Proxy） | 园区、多子网、需要审计与权限 |
| SRP | 纯单播 DNS Update | 由注册服务器决定，可跨链路 | DNS Update + SIG(0) 公钥，FCFS 命名 | 有，单播侧 | SRP 注册服务器 / 代理 | Wi-Fi、低功耗与电池设备、大量终端 |

读表时抓两条：**只有 mDNS 那一行的「递归」是无**；**只有 mDNS 那一行的「谁应答」不是服务器**。其余三行都落在本书前 26 章已经覆盖的单播架构里，只是记录内容换成了 DNS-SD 三件套。

## 一段可复制的教学配置：把分流钉死

下面用 Avahi 发布一个示例服务，再用 systemd-resolved 把 `.local` 与单播名字分开，最后确认 Unbound 只管单播侧。三块配置各自紧跟「为什么 / 怎么验证 / 失败模式与回滚点」，可按顺序照抄。

### 前置假设

- 网关单播侧地址 `192.0.2.53`（Unbound 监听 53），LAN 段 `192.0.2.0/24`；IPv6 用 `2001:db8::/32` 中的地址。
- 内部单播域用 `home.arpa.`，对外示例域用 `example.org.`；**不使用 `.local` 作为单播区名**。
- 参与演示的主机名统一写作 `gw-01`、`edge-01`、`cloud-01`。

### 配置块一：限定 Avahi 只在 LAN 接口上说话

`/etc/avahi/avahi-daemon.conf`：

```conf
[server]
use-ipv4=yes
use-ipv6=yes
allow-interfaces=lan0
deny-interfaces=wan0
domain-name=local
enable-dbus=yes

[publish]
publish-addresses=yes
publish-hinfo=no
publish-workstation=no

[reflector]
enable-reflector=no
```

**① 为什么这么设**：`allow-interfaces` 把多播限制在 LAN 一侧，避免 mDNS 通告从 WAN 口漏出；`domain-name=local` 明确后缀；`enable-reflector=no` 是关键——反射器会把多播在多个接口间转发，等于人为把「链路本地」改成了「跨链路」，既违背 TTL=1 的边界，也会和后面的单播 DNS-SD 抢同一批名字。这一项默认就该关。

**② 怎么验证**（一条可复制命令）：

```bash
avahi-browse -art | grep -i '_demo._tcp'
```

**③ 失败模式 / 回滚点**：若 `avahi-browse` 输出为空但服务进程在跑，先查 `deny-interfaces` 是否把 LAN 口也排除了，再查防火墙；反射器误开时会在多个网段看到同一批实例，回滚就是把 `enable-reflector` 改回 `no` 并重启 avahi-daemon。任何改动前先备份该配置文件，`systemctl restart avahi-daemon` 是回滚点。

### 配置块二：发布一个示例服务

`/etc/avahi/services/demo.service`：

```xml
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Demo Service on %h</name>
  <service>
    <type>_demo._tcp</type>
    <port>12345</port>
    <txt-record>note=demo</txt-record>
    <txt-record>site=example.org</txt-record>
  </service>
</service-group>
```

**① 为什么这么设**：这是 DNS-SD 三件套的本地发布形式——`<type>` 对应 PTR 的服务类型，`<port>` 与主机名一起进 SRV，两条 `<txt-record>` 进 TXT。TXT 里只放元数据，不放地址；示例域 `example.org` 只作为标记，不参与解析。用 `_demo._tcp` 而不是真实服务类型，避免和链路上真实设备撞名。

**② 怎么验证**（一条可复制命令）：

```bash
avahi-resolve -n "gw-01.local" && avahi-browse -rt _demo._tcp
```

**③ 失败模式 / 回滚点**：`avahi-resolve` 超时说明 `.local` 没被存根解析器交给 mDNS（回到前面 `resolvectl status` 那一节），此时查服务文件没有意义；`avahi-browse` 能看到实例但解析不出地址，通常是多播被防火墙挡在入向。回滚点：删除该 `.service` 文件并重启 avahi-daemon，Avahi 会发出 goodbye（TTL=0）；若进程被强杀没来得及发，邻居侧残留记录只能等缓存老化，这一点前面已经说明。

### 配置块三：systemd-resolved 明确两条通道

`/etc/systemd/resolved.conf.d/10-mdns-split.conf`：

```conf
[Resolve]
DNS=192.0.2.53
Domains=home.arpa. example.org.
MulticastDNS=yes
LLMNR=no
```

**① 为什么这么设**：`DNS=` 把单播查询固定指向 Unbound 的文档示例地址；`Domains=` 只列单播侧的名字，`.local` **不写进去**——它由存根解析器内部判定走 mDNS 通道，写进路由域反而会把 `.local` 强行送去单播，制造后面要讲的抢解析。`MulticastDNS=yes` 与 `LLMNR=no` 并列，正是为了把两套链路本地协议分开，避免「关错了开关」。不同发行版与版本的默认值有差异，取值见官方手册对应章节。

**② 怎么验证**（一条可复制命令）：

```bash
resolvectl query gw-01.home.arpa. && resolvectl status | grep -iE 'MulticastDNS|LLMNR|DNS Servers'
```

**③ 失败模式 / 回滚点**：`gw-01.home.arpa.` 解析失败而 `.local` 正常，说明单播路由域没生效或 Unbound 未监听 `192.0.2.53`；反过来 `.local` 落到单播、返回 NXDOMAIN，说明 `.local` 被误写进了 `Domains=`。回滚点：删除该 drop-in 文件后 `systemctl restart systemd-resolved`，并确认 `/etc/resolv.conf` 仍指向存根解析器而不是被别的管理工具覆盖。

### 配置块四：Unbound 只补单播侧，绝不碰 `.local`

`/etc/unbound/unbound.conf.d/10-internal.conf`：

```conf
server:
    interface: 192.0.2.53
    access-control: 192.0.2.0/24 allow

local-zone: "home.arpa." static
local-data: "gw-01.home.arpa. IN A 192.0.2.1"
local-data: "edge-01.home.arpa. IN A 192.0.2.2"
```

**① 为什么这么设**：这两条 `local-data` 只服务于单播内部域，是第 25 章「静态生成 local-data」路径的最小例子。**故意不为 `.local` 配任何 `local-zone` / `local-data`**：一旦配上，Unbound 就会开始对 `.local` 给出权威性应答，与 mDNS 形成两条都自称权威的通道，「时通时不通」就从别人的问题变成自己的配置问题。第 9 章的默认本地区清单要与这里的取舍一起看，别把清单里的保留名当成可随便接管的可写区。

**② 怎么验证**（一条可复制命令）：

```bash
unbound-checkconf && dig @192.0.2.53 gw-01.home.arpa. A +short
```

**③ 失败模式 / 回滚点**：`unbound-checkconf` 报错就别 reload，先按行号修配置；`dig` 能拿到 `192.0.2.1` 说明单播侧正常。再补一条反向验证：

```bash
dig @192.0.2.53 gw-01.local. A +short
```

预期是**拿不到 A 记录**；无论返回 NXDOMAIN、SERVFAIL 还是 REFUSED，都说明这次查询走的是单播而不是 mDNS，边界成立。若这里返回了一条地址，说明有人给 Unbound 塞了 `.local` 数据，或者上游单播 DNS 占了 `.local`——立刻按下一节排查。回滚点：删掉相应 `local-zone` / `local-data` 行并 reload，配置管理侧同步回滚源文件。

### 验证命令汇总

```bash
# 1. 谁在监听，确认 Unbound 不在 5353 上
ss -lunp | grep -E '(:53|:5353)'

# 2. 存根解析器的两个开关是不是各自独立
resolvectl status | grep -iE 'MulticastDNS|LLMNR'

# 3. 多播侧能不能看到实例与地址
avahi-browse -art
avahi-resolve -n "gw-01.local"

# 4. 单播侧能不能拿到内部名字
dig @192.0.2.53 gw-01.home.arpa. A +short
dig @192.0.2.53 _demo._tcp.example.org. PTR +short

# 5. 抓包看 5353 与 224.0.0.251 上有没有真实流量
tcpdump -ni any 'udp port 5353'
```

第 5 条最有说服力：把一台客户端接进 LAN 后触发一次发现，若 `-ni any` 里完全没有 5353 包，那么无论 `avahi-browse` 报什么，问题都在链路或防火墙，不在 DNS 记录本身。

## 边界与排障：企业里最常见的三种「时通时不通」

### 一、`.local` 被企业单播 DNS 占用

[RFC 6762] 把 `.local` 留给 mDNS；但历史上不少企业内部单播区也叫 `.local`。结果同一名字有两条通道都可能应答：存根解析器按实现与路由域的不同，有时把查询送到 5353、有时送到 53。表现就是典型的时通时不通——同一台打印机，上午能连、下午 NXDOMAIN；同一条命令，在 A 机器上成功、在 B 机器上失败。

排查顺序：

```bash
# 看这个名字被判定到哪条通道
resolvectl query gw-01.local

# 看单播侧是否真的有人在应答 .local
dig @192.0.2.53 gw-01.local. A +short

# 看多播侧有没有包
tcpdump -ni any 'udp port 5353'
```

结论只能有一个：**不要再让 `.local` 同时属于两套系统**。要么把内部单播区迁到 `home.arpa.` 或组织自己的子域（`example.org.` 下的内部子域），要么在存根解析器上把 `.local` 固定交给 mDNS 并保证单播侧不给它权威应答。不要试图用 Unbound 的 `local-zone` 去「补」这个洞，那只会再加一个应答者。

### 二、防火墙整体禁用 mDNS

企业里 mDNS 常被防火墙整体禁用。微软文档给出的做法是禁用入站 `mDNS (UDP-In)` 规则（按标题在 Microsoft Learn 检索可见，具体规则名与路径见官方资料）。这类策略一开，链路上 5353 的入向应答全被丢掉，`avahi-browse` 列表为空，但单播 `dig` 完全正常。

要分清两种「看不到」：

| 现象 | 单播 `dig` | 5353 抓包 | 判断 |
|---|---|---|---|
| 防火墙禁 mDNS | 正常 | 有查询、无应答 | 链路被剪，名字本身没错 |
| 没有 mDNS 应答者 | 正常 | 无查询、无应答 | 本机压根没开多播解析 |
| `.local` 被单播占用 | 有时返回地址 | 时有时无 | 两条通道在抢解析 |

**跨子网才靠单播 DNS-SD**：这一条要和 TTL=1 连起来读。企业禁掉 mDNS 之后，跨子网的发现需求不会消失，它会转到 Wide-Area DNS-SD（UPDATE + TSIG 写权威区）、Discovery Proxy（翻译已有设备）或 SRP（纯单播注册）上。也就是说，禁多播不是把服务发现关掉，而是把它从「不可控的链路行为」搬回「可审计的单播区」——这正是本书主张的架构方向。

### 三、跨子网还想发现：不是打通多播

第三类误判是把「跨 VLAN 发现不了」当成交换机配置问题，于是去开组播反射、去调 snooping、去放通 `224.0.0.251`。这些做法即使短期有效，也把 mDNS 的链路本地前提破坏了：同一名字会在多条链路上被不同主机争用，goodbye 包又跨不过去，残留记录越积越多。

正确做法按设备类型分：

- 改得了协议栈的新设备 → 直接 SRP 纯单播注册，或 Wide-Area DNS-SD 的 `UPDATE` + TSIG。
- 只会上多播的老设备 → 在它所在链路放 Discovery Proxy [RFC 8766]，单播侧共用权威区与 Unbound。
- 只想浏览、不需要注册 → 单播侧查 PTR / SRV / TXT 三件套即可，不需要任何 5353 流量。

### `home.arpa.` 才是该用的名字

`home.arpa.` [RFC 8375] 是给家庭与小型内网专门留的名字，不会与 mDNS 的 `.local` 抢解析，也不会与公网可解析的域混淆。它和第 9 章的默认本地区清单是同一件事的两面：第 9 章管「哪些名字不该出网」，这里管「内部名字该叫什么」。

落地时的三条建议：

- 内部单播区统一用 `home.arpa.`（家庭/小型）或组织自有域的内部子域，不用 `.local`。
- Unbound 侧用 `local-zone` + `local-data` 静态承载，或按第 25 章用 `stub-zone` 指向 NSD / 微软 DNS 权威区；`home.arpa.` 不该出现在公网递归路径上。
- 存根解析器的 `Domains=` 只列单播名字，`.local` 留给 mDNS，两边互不越界。

把名字选对，前面三类「时通时不通」里至少两类在部署阶段就消失了。剩下的那一类——防火墙策略——反而是最容易验证的：一条 `tcpdump -ni any 'udp port 5353'` 就能给出答案。

## 本章要点

1. **Unbound 不参与 mDNS**：不监听 5353 多播、不应答 `.local`；分流由存根解析器（systemd-resolved / mDNSResponder / Avahi）在客户端完成，Unbound 只收到单播那一半。
2. **mDNS 是链路本地、无递归的另一套名字系统**：`224.0.0.251` / `FF02::FB` / UDP 5353 / TTL=1；wire format 复用但语义不同（Message ID 恒 0、AA=1、RD/RA=0、RCODE 恒 0、非 0 OPCODE 静默忽略、TC 表示 Known-Answer 续包），名字靠 probing / announcing 争用、靠 goodbye（TTL=0）撤销。
3. **DNS-SD 不依赖 mDNS**：PTR / SRV / TXT 三件套可以完整跑在单播 DNS 上；跨链路用 Wide-Area DNS-SD（[RFC 2136] UPDATE + TSIG）、Discovery Proxy（[RFC 8766]）或 SRP（[RFC 9665] 纯单播、SIG(0) 公钥、FCFS 命名），LLQ 见 RFC Editor 索引。
4. **`.local` 只能属于一条通道**：被企业单播 DNS 占用就会与 mDNS 抢解析，表现为时通时不通；家庭与小型内网该用 `home.arpa.` [RFC 8375]，与第 9 章默认本地区清单一起规划。
5. **企业禁用 mDNS 是常态**：禁用入站 `mDNS (UDP-In)` 规则后，跨子网发现应转到单播 DNS-SD，而不是去打通多播；排障先用 `ss -lunp`、`resolvectl status`、`tcpdump -ni any 'udp port 5353'` 分清是链路、开关还是名字冲突。
6. **LLMNR 不是 mDNS**：两者端口、报文与发现模型都不同，开关必须分别设置、分别排查，别把「关掉了 LLMNR」当成「关掉了 mDNS」。

## 参考资料

1. RFC 6762，*Multicast DNS*：https://www.rfc-editor.org/rfc/rfc6762
2. RFC 6763，*DNS-Based Service Discovery*：https://www.rfc-editor.org/rfc/rfc6763
3. RFC 2136，*Dynamic Updates in the Domain Name System (DNS UPDATE)*：https://www.rfc-editor.org/rfc/rfc2136
4. RFC 8766，*DNS-SD Discovery Proxy*：https://www.rfc-editor.org/rfc/rfc8766
5. RFC 9665，*Service Registration Protocol for DNS-Based Service Discovery*：https://www.rfc-editor.org/rfc/rfc9665
6. RFC 8375，*Special Use Domain 'home.arpa.'*：https://www.rfc-editor.org/rfc/rfc8375
7. Unbound 官方手册（章节与默认值以此为准）：https://unbound.docs.nlnetlabs.nl/en/latest/
8. Microsoft Learn（按标题检索 `mDNS (UDP-In)` 入站规则与禁用方式）：https://learn.microsoft.com/
