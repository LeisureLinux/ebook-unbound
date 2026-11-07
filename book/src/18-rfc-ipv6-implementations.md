# IPv6 / RA / RDNSS RFC 族与实现差异

> 本章只讨论条文为什么这样写、以及不同实现对同一条条文的处理差异。RA / RDNSS 的部署、脚本、验证命令与回滚点在第 19–21 章，BIND / dnsmasq 等产品的功能横评在第 2 章，此处都不重复。文中 `2001:db8::/32`、`fd00::/8` 段地址、`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24` 与 `example.net`、`example.org` 均为教学用示例地址与域名，不对应任何真实环境。年份、版本对应关系一律见 RFC Editor 的官方页面。

IPv6 主机上网之前要回答四个问题，而 IPv4 时代这四个问题大多由一个 DHCP 报文包办：

1. 我该用哪个地址（SLAAC 与 PIO）；
2. 这个地址能用多久（Preferred / Valid Lifetime）；
3. 出网的默认路由器是谁、还有哪些更具体的前缀要走谁（RA、默认路由器优先级、RIO）；
4. 域名该问谁（RDNSS / DNSSL）。

它们的答案全部装在 ICMPv6 路由器广告（Router Advertisement，RA）及其选项里。DNS 部分之所以值得单独写一章，是因为本书的主角是递归解析器：**一个 Unbound 实例再怎么加固，只要客户端的存根解析器（stub resolver）把查询发给了别人，加固就等于没做**。RA 是决定「客户端把查询发给谁」的那一层协议，而这一层的语义由一组彼此咬合的 RFC 规定。本章按「解决什么 → 关键条文 → 对实现的约束 → 常见误读」逐个拆开。

## 底盘：[RFC 4291] 地址架构与 [RFC 8200] 1280 字节最小 MTU

### 它要解决什么

[RFC 4291] 定义 IPv6 的地址模型：128 位地址、单播 / 组播 / 任播三类、地址文本表示、以及必需的地址（每个接口的链路本地地址、环回、未指定地址、被请求节点组播地址等）。它把「地址」和「接口」解耦：一个接口可以同时挂链路本地、ULA、一个或多个全局地址，每个地址还带 preferred / deprecated 状态。后面的源地址选择、临时地址、ULA 都建立在这个多地址模型之上。

[RFC 8200] 是 IPv6 基本首部规范。它把分片责任从路由器收回到源端：中间路由器不重组、不分片，超出路径 MTU 的报文由源端通过 Packet Too Big 学习后重发。

### 关键条文

- [RFC 4291] 规定地址前缀长度写法、链路本地前缀 `fe80::/10`、组播前缀 `ff00::/8`，以及接口标识符与前缀拼成 128 位地址的规则；链路本地地址只在同一链路上有意义，因此带链路本地地址时必须带 zone / scope 信息。
- [RFC 8200] 第 5 节规定：**每条链路的 MTU 必须至少为 1280 字节**，这就是 IPv6 最小链路 MTU。链路层不足 1280 时必须在 IPv6 之下自己做适配。源端可以只发不超过 1280 字节的报文并省略 PMTUD 实现，但那样就不能利用更大 MTU。

### 对实现的约束

1280 不是「建议值」，是所有上层协议必须能落脚的地板。DNS 走 UDP 时，IPv6 报文 = 40 字节 IPv6 首部 + 8 字节 UDP 首部 = 48 字节开销，`1280 − 48 = 1232`，这正是第三篇反复出现的 `edns-buffer-size: 1232` 的条文根因。把值调到 1472 会在 1280 MTU 的链路上分片；而 [RFC 8200] 的分片只有源端能做，被中间设备丢弃的分片不会有任何路由器替你重组，表现就是等超时再退到 TCP。

链路本地地址与 zone 的约束同样会漏到实现里：RA 里若广告链路本地 RDNSS 地址，[RFC 8106] 要求把它连同源接口的 zone index 一起登记进解析器仓储，否则同一个 `fe80::1` 在多接口主机上无法区分该从哪个接口发出。

### 常见误读

- **「IPv6 不分片，所以不会丢包」**：不分片的是中间路由器，源端仍能发分片首部；分片丢失照样发生，且更难排查。
- **「1280 是推荐 MTU」**：是下限，不是性能指标；链路 MTU 更大不代表 UDP 载荷能自动放大。
- **「文档地址 `2001:db8::/32` 可以随便配到接口上」**：它是 [RFC 3849] 保留的文档前缀，只用于写作与教学，不是可路由地址。

## [RFC 4861] NDP 与 RA：报文为什么必须是 hop limit 255

### 它要解决什么

IPv6 取消了 ARP，把「地址解析、邻居不可达检测、路由器发现、重定向、前缀与参数通告」合并为邻居发现协议（Neighbor Discovery Protocol，NDP），承载在 ICMPv6 上。RA 是其中的路由器广告消息：路由器周期性组播到 `ff02::1`（所有节点），或应答主机发往 `ff02::2`（所有路由器）的路由器请求 RS。

### 关键条文

- 所有 NDP 消息的 IP Hop Limit **必须为 255**；接收方必须校验该值为 255，否则静默丢弃。[RFC 4861] 给的理由很直白：255 意味着「该报文不可能被路由器转发过」，因此 NDP 天然被限制在本链路内，不需要额外信任机制就能挡住来自链路外的伪造 RA / NA。这是一条**协议级安全边界**，不是可协商参数。
- RA 的接收有效性检查还包括：ICMP 校验和有效、Code 为 0、ICMP 长度不小于规定值、所有已含选项长度非零。
- RA 首部带 `Cur Hop Limit`、`M` / `O` 位、`Router Lifetime`、`Reachable Time`、`Retrans Timer`；`Router Lifetime` 非零才表示该路由器可作为默认路由器，为零表示「撤销我作为默认路由器」。
- 源链路层地址选项（Source Link-Layer Address Option，SLLAO）：路由器在 RA 中**可以**携带发送接口的链路层地址（为便于入向负载均衡等情况也允许省略）；NS 发往被请求节点组播地址时则**必须**携带，好让目标有地方回 NA。
- 前缀信息选项（Prefix Information Option，PIO）带 `L`（on-link）、`A`（autonomous，即允许 SLAAC）两个标志位、Valid Lifetime 与 Preferred Lifetime。
- 主机维护默认路由器列表、前缀列表、邻居缓存与目的地缓存；未识别选项必须忽略，保证向后兼容——这正是 [RFC 4191]、[RFC 8106] 能以后续 RFC 追加选项而不改动 [RFC 4861] 的原因。

### 对实现的约束

任何转发、隧道、桥接、Netfilter 规则都不许改写 NDP 报文的 hoplimit。第 19 章实测过的 iOS 硬性要求只是这条通用条文的一个可观测后果：抓包里若看到 254 或 64，客户端会整包丢弃，且不会给出「你的 hoplimit 错了」这种提示，只会表现为「拿不到 IPv6 默认路由 / 拿不到 DNS」。发送侧也别自作聪明把 hoplimit 交给应用填：由内核构造 RA 时置 255 最稳。

`Router Lifetime=0` 是「撤销默认路由器」，不是「关闭 RA」。一台路由器仍可以发带 PIO / RDNSS 选项、但 `Router Lifetime=0` 的 RA，用来只广告 DNS 或撤销旧前缀而不参与默认路由选举。实现若把两者混为一谈，会造成「改了 DNS 却把默认路由弄没了」。

### 常见误读

- **「hoplimit 255 只是防环」**：它首先是链路边界证明，用来在不引入 SEND / RA-Guard 的前提下把 NDP 攻击面限制在本链路。
- **「RA 只能组播」**：RS 触发的单播 RA 是 [RFC 4861] 的正常路径，关系到「刚连上 Wi-Fi 能不能立刻拿到配置」。
- **「SLLAO 是必填项」**：RA 里是 MAY；真正 MUST 的是发往被请求节点组播地址的 NS。把 MAY 当 MUST，会在多接口 / 负载均衡拓扑下误判合规。
- **「没收到 RA 就退回 DHCPv4 思维」**：`M` / `O` 位只表示「可用有状态 DHCPv6」，IPv6 默认路由仍然只能来自 RA，DHCPv6 本身不下发默认网关——这是它与 DHCPv4 最根本的差别。

## [RFC 4862] SLAAC：为什么 PIO lifetime=0 缩不掉一个长寿命地址

### 它要解决什么

SLAAC 规定主机如何把 RA 里的前缀与本接口标识符拼成地址、如何做重复地址检测（DAD）、以及地址从 preferred 到 deprecated 再到 invalid 的生命周期。它是无状态的：路由器不记录谁用了哪个地址，主机自己算、自己检测冲突。

### 关键条文

[RFC 4862] 第 5.5.3 节的 e) 步是本章最该逐字读的一段。当 RA 里的前缀与主机已有的某个 SLAAC 地址前缀相同时：

- **Preferred Lifetime 总是**重置为收到的值（这样管理员可以用短 preferred lifetime 主动把地址标记为 deprecated）；
- **Valid Lifetime 的处理分三种情况**，设已存在地址的剩余寿命为 `RemainingLifetime`：
  1. 收到的 Valid Lifetime 大于 2 小时，或大于 `RemainingLifetime`，就把有效寿命设为收到值；
  2. 若 `RemainingLifetime` ≤ 2 小时，则**忽略**该 PIO 对 valid lifetime 的修改，除非 RA 经过了认证（如 [RFC 3971] SEND）；
  3. 否则（即想缩短、且剩余寿命还长），把有效寿命重置为 **2 小时**，而不是收到的那个小值。

条文明说这是为了防一种拒绝服务攻击：一份伪造的、带极短 Valid Lifetime 的 RA 不该让全网主机的地址瞬间过期。合法路由器周期性发的 RA 会在短寿命真正生效之前把它「抵消」掉。注意 preferred lifetime 不受此保护，因为「把地址标记 deprecated」造成的危害相对小，且管理员确实需要这个能力。

### 对实现的约束

这直接解释了第 19 章那个最反直觉的观测：**运营商换前缀之后，旧前缀的地址不会因为你补发一份 `lifetime=0` 的 PIO 就立刻消失**。按第 3 条，一个原本还有几天寿命的地址最多被压到 2 小时；按第 2 条，剩余寿命已经很短的甚至直接忽略。也就是说「PIO lifetime=0 = 立即删除地址」在 [RFC 4862] 里是不存在的语义。

于是 RFC 族里出现了一个重要分工，后文会反复用到：

| 对象 | `lifetime=0` 的含义 | 依据 |
|---|---|---|
| PIO 中的前缀 | 把已有 SLAAC 地址的 valid lifetime 压到最多 2 小时（或忽略），不是立即删除 | [RFC 4862] §5.5.3 e) |
| RDNSS 选项中的解析器地址 | 从 DNS Server List 与 Resolver Repository 中**删除该条目** | [RFC 8106] §6.2 步骤 (b) |
| RA 首部 `Router Lifetime=0` | 撤销该路由器作为默认路由器 | [RFC 4861] |
| RIO 中的路由 | 撤销该更具体路由 | [RFC 4191] |

不同选项的「0」语义不同，是因为它们保护的对象不同：地址是长期状态，撤销必须给足在途连接收敛时间；DNS 服务器列表是配置指针，换掉它不影响已有 TCP 连接，可以即时生效。

### 常见误读

- **「PIO lifetime=0 就是让地址马上失效」**：条文给出的下限是 2 小时，且这条下限是防 DoS 设计，不是实现偷懒。
- **「前缀轮换后只要发撤销 RA 就完事」**：旧地址仍会存活，配合 [RFC 6724] 的临时地址优先，客户端可能继续拿旧前缀当源地址，上游不路由就全断。这正是第 19 章要设三处 sysctl 的机理。
- **「deprecated 地址会被立刻删掉」**：deprecated 只是不再用于**新**连接，已有通信 SHOULD 继续用它；要等 valid lifetime 到期才是 invalid。
- **「DAD 通过就万事大吉」**：DAD 只保证本链路上没有冲突，不保证前缀可路由、也不保证上游仍认这个前缀。

## [RFC 6106] → [RFC 8106]：RDNSS / DNSSL 与 lifetime=0

### 它要解决什么

RA 原本不带 DNS 信息，IPv6 主机只能靠有状态 DHCPv6 拿递归解析器地址，而不少客户端（尤其移动系统）根本不实现 DHCPv6 客户端。[RFC 6106] 定义了两个 RA 选项：RDNSS（Recursive DNS Server，携带一到多个递归解析器 IPv6 地址 + Lifetime）与 DNSSL（DNS Search List，携带搜索域 + Lifetime），让纯 SLAAC 网络也能配好 DNS。[RFC 8106] 废止并取代 [RFC 6106]，沿用选项格式与语义，主要做澄清与默认值调整。

### 关键条文

- RDNSS / DNSSL 选项各有自己的 Lifetime 字段，**与 RA 首部的 Router Lifetime 无关**；路由器不再是默认路由器，不代表它广告的 DNS 信息要一起过期。
- Lifetime 以秒计，是「自收到该 RA 起的有效时长」，主机为每条 DNS 记录维护 Expiration-time；到期后该条目不应再使用。
- [RFC 8106] §6.1 / §6.2 规定主机维护两张结构：DNS Server List（按 RA 顺序保存学到的地址与 Expiration-time）与 Resolver Repository（真正供存根解析器使用的有序列表）。§5.1 的字段定义把零值写为「该地址 MUST 不再被使用」；§6.2 步骤 (b) 把它落到实现上：**若某地址已在 DNS Server List 中、且该 RDNSS 选项的 Lifetime 字段为 0，则从两张表里删除该条目**，用于 DNS 服务终止或重编号场景。步骤 (c) 则规定同接口收到相同地址的非零 Lifetime 时只刷新 Expiration-time；§5.3.1 要求主机 SHOULD 保留来自多个来源（RA、DHCP）的 DNS 信息，保留多少属本地策略。
- [RFC 8106] §5.1 规定 RDNSS 的 Lifetime 是「自收到该报文起的秒数」，SHOULD 默认**至少**为 `3 × MaxRtrAdvInterval`；全 1 值（`0xffffffff`）表示无限，零值表示该地址 MUST 不再被使用。DNSSL 选项的 Lifetime 字段语义相同。
- [RFC 6106] 曾把 Lifetime 约束成 `MaxRtrAdvInterval <= Lifetime <= 2*MaxRtrAdvInterval` 的**双侧区间**。[RFC 8106] 附录 A 说明这次修订针对的是：高丢包链路上按旧上界取值会让选项频繁过期，于是改成以 `3 × MaxRtrAdvInterval` 为下界、不再设上界，同一节还列出了「至少能扛住连续三次含 DNS 选项的 RA 丢失」的理由。其余改动：删除「过期前主动发 RS 刷新」的建议以免增加组播流量；明确 RDNSS 地址可以是链路本地地址（须带 zone index）；取消「最多维护 3 个 RDNSS 地址 / 3 个搜索域」的推荐，交给实现者的本地策略；不再把 SEND 写成 SHOULD / RECOMMENDED 级别的安全机制。

### 对实现的约束

**Lifetime=0 是「删除」，不是「设为无限」、也不是「优先级最低」。** 但它只在「该地址已存在于 DNS Server List」时生效：对一个从未学过的地址发 `lifetime=0`，按步骤 (b) 的条件不成立，实现会走到步骤 (d) 的注册分支。因此网关若要压掉同二层域里另一台路由器广告的 DNS 地址，必须：

- 用**完全相同**的地址值构造撤销选项（前缀、压缩写法可以不同，数值必须一致）；
- **持续、周期性重复发送**，因为客户端重新联网或对方重新发 RA 后该地址会被重新注册；
- 自己的地址用正常 Lifetime 随每次 RA 续期。

还有一层实现自由度：主机把多个路由器广告的 RDNSS 合并成一张列表，而不是「后到覆盖先到」，且 [RFC 8106] 不再限制条目数。但「是否接受 A 路由器撤销 B 路由器广告的地址」在条文里并非强约束，各家网络栈行为不完全一致——这是第 19 章把「阻断 WAN 侧 RA」列为退路的原因。

DNSSL 与 RDNSS 的 lifetime 语义一致，但搜索域会改变**绝对域名被怎样追加后缀**，对查询放大与 QNAME 最小化的影响属于第三篇第 13 章的范围，此处不展开。

### 常见误读

- **「[RFC 6106] 和 [RFC 8106] 是两套可任选的机制」**：后者 obsoletes 前者，选项类型码沿用，引用时应以 [RFC 8106] 为准；旧资料里「最多 3 个 RDNSS」的说法来自 [RFC 6106]。
- **「lifetime=0 表示永不过期」**：与 TTL / DHCP lease 的直觉相反，它是显式撤销；无限寿命的编码是全 1 值 `0xffffffff`，两者别弄反。
- **「发一次撤销就够了」**：条目会被对方后续 RA 重新注册，撤销必须持续。
- **「RDNSS 只能广告全局地址」**：[RFC 8106] 明确允许链路本地地址；是否选 ULA 是稳定性考量（GUA 前缀会轮换，ULA 不变），不是条文限制。

## [RFC 4191]：RIO 与默认路由器优先级

### 它要解决什么

[RFC 4861] 只允许主机从多条默认路由器里挑一条，没有「哪条更优先」和「某个具体前缀要走谁」的表达能力。多宿主、双上行、以及「网关后面还有一段内网」的场景都需要更细的路由信息。[RFC 4191] 在 RA 首部保留位里加入 2 位 `Prf`（Default Router Preference），并新增类型 24 的路由信息选项（Route Information Option，RIO）。

### 关键条文

- `Prf` 是 2 位有符号整数（Low / Medium / High）；`Router Lifetime=0` 时发送方 MUST 置 (00)、接收方 MUST 忽略它；保留值 (10) 按 (00) 处理。
- RIO 带 Prefix Length、独立的 `Prf`（Route Preference）、Route Lifetime 与可变长 Prefix；`Prefix Length > 64` 时 Length 必须为 3；收到 (10) 保留值时整个 RIO MUST 被忽略。
- 同一 RA 里不得出现两个前缀相同的 RIO。RIO 只在多个路由器广告**相同前缀**时参与比较，它不是默认路由的替代品。
- [RFC 4191] 把主机分为 Type A / B / C：A 完全忽略优先级与 RIO，B 用默认路由器优先级但忽略 RIO，C 两者都实现。也就是说 RIO 支持度是**实现可选的能力**，条文自己就预留了不支持的合规主机。

### 对实现的约束

Linux 内核把 RIO 的处理交给 `accept_ra_rt_info_min_plen` 等 sysctl 控制；Android 侧的 `accept_ra_rt_info_min_plen=48` 要求意味着**前缀长度小于 48 的 RIO 会被静默丢弃**。注意它是「最小前缀长度」的下限：/32、/40 这类更粗的聚合不满足，/48 或更细才进路由表。这不是条文规定，而是某个具体网络栈的本地策略，两者别混为一谈。

由此产生一个粒度分工：RIO 用于**路由聚合**（/48），PIO 里的 /64 用于**地址配置与前缀变更检测**。聚合前缀在运营商轮换 LAN /64 时通常不变，因此拿 RIO 的 /48 去判断「前缀是否变化」会把两次故障的根因藏成同一个变量——这个机理在第 19 章展开，本章只标出条文依据：PIO 与 RIO 是不同选项、不同 Lifetime、不同用途，[RFC 4191] 明确 RIO 不能取代 on-link / SLAAC 前缀判定。

### 常见误读

- **「设了 High 优先级就一定能抢到默认路由」**：`Prf` 只在路由器都有效（Router Lifetime 非零）时参与比较；且 Type A 主机完全忽略它。
- **「RIO 可以当 PIO 用」**：RIO 只装路由，不给主机配地址；反过来 PIO 也不能替代 RIO 表达「去这个前缀走我」。
- **「Android 的 48 是 RFC 规定的最小值」**：是具体实现策略，条文里的 Prefix Length 范围是 0–128。

## [RFC 4941] 临时地址与它的后续修订

### 它要解决什么

SLAAC 默认把 64 位接口标识符（传统上由链路层地址派生，或按 [RFC 4291] 相关规则生成）长期固定，配合全局前缀就能跨网络、跨时间关联同一台设备。[RFC 4941] 定义临时地址扩展：在稳定的「公共地址」之外，额外生成随机接口标识符、寿命有限、周期性重新生成的临时地址，用于对外发起的新连接，降低被动流量分析的关联能力。

### 关键条文

- 临时地址由随机接口标识符 + 当前前缀生成，生成后仍要做 DAD。
- 它带 `TEMP_VALID_LIFETIME` 与 `TEMP_PREFERRED_LIFETIME`；[RFC 4941] 给出的 `TEMP_VALID_LIFETIME` 默认值是**一周**，并明确该值未必适合所有场景、用户应能调整。
- 实现必须提供让应用反转偏好、改用公共地址的机制（例如 [RFC 5014] 一类的 API 扩展），且该机制只影响调用它的应用。
- 临时地址与「隐私」的边界要说清：它降低的是**按地址做长期关联**的能力，不是加密、也不是匿名；上层协议里的标识符、DNS 查询名、时序仍可被关联。
- 版本说明：[RFC 8981] 废止并取代 [RFC 4941]（同系列、标题相近的 *Temporary Address Extensions for Stateless Address Autoconfiguration in IPv6*）。本书提到「临时地址」这一机制时沿用 [RFC 4941] 这个名字以和第 19 章的实测结论对齐；具体默认参数、生成算法与弃用条件以 [RFC 8981] 及官方手册对应章节为准。

### 对实现的约束

临时地址的寿命是**独立于前缀寿命**的另一只时钟。前缀轮换后，旧前缀上已生成的临时地址并不会因为前缀撤销而立刻消失（见 18.3），它的 `TEMP_VALID_LIFETIME` 还在走；Linux 侧 `temp_valid_lft` 默认 48 小时这类取值是实现默认值，不是 [RFC 4941] 的一周，两者不要互相套用。

对服务器侧还有一个常被忽略的约束：临时地址不适合做**被访问**的端点。作为 RDNSS 里广告的递归解析器地址，应当用不随前缀轮换、也不随隐私时钟变化的 ULA；让客户端的解析器地址自己每两天变一次，等于把第 9 章的访问控制列表变成一张永远过期的表。

### 常见误读

- **「开了临时地址就不用管 DNS 隐私」**：它只管源地址，与 DNSSEC 的数据完整性、DoT / DoH / DoQ 的传输加密是三件事。
- **「临时地址会让服务器连不上我」**：它用于主动出连接；监听、被访问应绑定稳定地址。
- **「旧前缀的临时地址会随 RA 撤销一起消失」**：见 18.3，PIO `lifetime=0` 压不掉长寿命地址。

## [RFC 6724] 源地址选择：规则编号的迁移与两条关键偏好

### 它要解决什么

一台主机同时有链路本地、ULA、一个或多个 GUA、临时地址、deprecated 地址，甚至还有 IPv4 地址。给定目的地址，选哪个做源？[RFC 3484] 定义了默认源地址选择与默认目的地址选择算法及策略表（policy table）；[RFC 6724] 废止并取代它，规则排序与内容有调整。这个选择发生在应用 `connect()` / 发包之前，普通用户几乎看不到，却直接决定「流量从哪个前缀出去」。

### 关键条文

先说编号迁移，因为运维资料里常把两代规则混着引用：

| 语义 | [RFC 3484]（旧） | [RFC 6724]（现行） |
|---|---|---|
| 避开 deprecated 地址 | Rule 3: Avoid deprecated addresses | **Rule 3: Avoid deprecated addresses**（编号未变） |
| 优先出接口 | Rule 5: Prefer outgoing interface | **Rule 5: Prefer outgoing interface** |
| 优先匹配标签 | Rule 6 | **Rule 6: Prefer matching label**（编号未变） |
| 临时地址优先 | 旧版 Rule 7 是 *Prefer public addresses*（**相反**） | **Rule 7: Prefer temporary addresses** |

也就是说「规则 5 非 deprecated」是旧资料里常见的说法混淆：[RFC 3484] 里避开 deprecated 是 Rule 3、优先出接口是 Rule 5；[RFC 6724] 保留了这两个编号，真正反转的是 Rule 7——从「优先公共地址」改成「优先临时地址」。此外 [RFC 6724] 新增了 Rule 5.5（优先由所选 next-hop 广告的前缀所分配的地址），并明确该规则**只适用于会追踪「哪个 next-hop 广告了哪个前缀」的实现**，不要求所有实现都记这份信息。

两条与本书主线最相关的偏好：

- **避开 deprecated**（Rule 3）：preferred 与 deprecated 二选一时取 preferred。这是「旧前缀地址被标记 deprecated 后不再用于新连接」的条文依据。
- **临时地址优先**（Rule 7）：临时地址与公共地址二选一时取临时地址；实现 MUST 提供让应用反转该偏好的机制。

策略表同样关键：默认表里 `::/0` 优先级 40、`fc00::/7`（即 ULA 段）优先级 3、`::ffff:0:0/96`（IPv4 映射）35。低优先级 + 标签不匹配，会让「有 IPv6 全局地址时优先不用 ULA 做源」成为默认行为——这正是「ULA 做 DNS 地址、GUA 做出网地址」能共存的原因：RDNSS 里是 ULA，但客户端发起查询时选的源地址未必是它，防火墙与访问控制要按这个现实写。

### 对实现的约束

把 18.3、18.6、18.7 三段连起来就是那条著名的故障链：运营商换 /64 → 旧 PIO 的长寿命地址只能被压到 2 小时 → 旧前缀上的临时地址仍在 `TEMP_VALID_LIFETIME` 内且仍是 preferred → Rule 7 让它优先于公共地址 → Rule 3 不把它排除（它还没 deprecated）→ 客户端拿旧前缀当源 → 上游不路由 → 全断。任何一环的条文单看都合理，组合起来才成为故障。

实现还 MUST / SHOULD 提供策略表可配置能力（如 `gai.conf` 一类机制），且自动添加的行不得覆盖管理员显式配置的行。这条约束的含义是：策略表是本地策略，不是网络下发的，排查跨机器行为差异时先比对各自策略表。

### 常见误读

- **「Rule 7 一直都是临时地址优先」**：[RFC 3484] 的 Rule 7 恰好相反；引用旧抓包资料或旧内核文档时要先看它依据哪一代 RFC。
- **「Rule 5 是避开 deprecated」**：编号对应的是优先出接口；避开 deprecated 是 Rule 3。
- **「源地址选择由路由器决定」**：算法在主机本地跑，路由器只能通过 RA / RIO / 策略表默认值间接影响。
- **「ULA 优先级低说明 ULA 没用」**：低优先级针对的是选源；ULA 稳定不变，适合做被访问的解析器地址。

## [RFC 4193] ULA：稳定的「内部编号」，不是可路由公网地址

### 它要解决什么

RFC 1918 私有地址在 IPv6 里需要一个对应物，但设计目标更严格：既要在站点内部稳定、不随运营商前缀轮换变化，又要尽量避免两个站点合并 / 建 VPN 时前缀撞车。[RFC 4193] 定义唯一本地地址（Unique Local Address，ULA）。

### 关键条文

- 前缀 `fc00::/7`；其中 L 位为 1 的 `fd00::/8` 用于本地分配，`fc00::/8` 的分配方式见官方手册。实际书里写的 `fd00:…` 属于前者。
- 结构 = 前缀 + **40 位 Global ID** + 16 位 Subnet ID + 64 位接口标识符。Global ID 用伪随机算法生成，目的是让不同站点「碰巧选到同一个 /48」的概率极低，而不是靠中央机构分配。
- 条文明确 ULA **不预期在全局互联网上被路由**，也不应与全局地址做等价的路由通告；站点边界应过滤。它是「本地唯一」，不是「全球唯一」的同义词。
- [RFC 6724] 默认策略表给 `fc00::/7` 优先级 3，低于 `::/0` 的 40，见上一节。

### 对实现的约束

ULA 的价值在于**生命周期与运营商无关**：GUA /64 会被轮换，ULA 不会。因此把递归解析器的地址选在 ULA 段、通过 RDNSS 发布，是让「DNS 配置不随前缀轮换漂移」的条文级理由。它同时影响访问控制：第 9 章的 ACL 可以写成一个长期不变的 ULA 网段，不必每次前缀轮换都重写。

代价是实现必须自己管好两件事：一是边界过滤，别把 ULA 路由通告到公网侧；二是 Global ID 不要手工写成 `fd00::1/64` 这种人人相同的「伪 ULA」——那正是 40 位随机 Global ID 要防的冲突。教学示例里写 `fd00:53::1` 只为可读性，真实部署应生成随机 Global ID。

### 常见误读

- **「ULA 就是 IPv6 版的 192.168」**：机制类似，但 40 位 Global ID 的防冲突目标、以及策略表里的低优先级，都是 RFC 1918 没有的。
- **「ULA 可以 NAT66 出去上网」**：条文不预期全局路由；出网上网靠 GUA。
- **「ULA 与 GUA 同时存在时一定用 ULA」**：默认策略表相反，见 Rule 6 与优先级 3。

## PIO 的后续修订：编号核对与 [RFC 8978] / [RFC 9096]

### 它要解决什么

18.3 讲的那条 2 小时保护，在「前缀突然失效」的场景里会变成麻烦：CE 路由器崩溃重启、或运营商瞬间换发 DHCPv6-PD 前缀，主机却仍按旧 PIO 的 Valid Lifetime 认为旧前缀有效，于是继续拿旧地址当源，出现长时间的全断。这组文档处理的正是**重编号事件（renumbering event）**下 SLAAC 的收敛问题。

### 关键条文与编号说明

先做一次编号核对。这几个编号在二手资料里常被混用，按「拿不准的编号不编、以 RFC Editor 官方页面为准」的原则，这里给出可核验的对应关系：

| 文档 | 主题 |
|---|---|
| [RFC 8971] | *Bidirectional Forwarding Detection (BFD) for Virtual eXtensible Local Area Network (VXLAN)*；属 BFD/VXLAN 范畴，**不涉及 PIO** |
| [RFC 8978] | *Reaction of IPv6 Stateless Address Autoconfiguration (SLAAC) to Flash-Renumbering Events*；分析主机在突发重编号下的行为与成因 |
| [RFC 9096] | *Improving the Reaction of Customer Edge Routers to IPv6 Renumbering Events*（BCP）；给出 CE 路由器侧的规范性建议 |
| [RFC 4862] §5.5.3 | 前述 2 小时下限，是所有这些修订要绕开/配合的原始条文 |

值得专门提醒的是：**[RFC 8971] 与 PIO 无关**，它的主题是 VXLAN 上的 BFD；讨论「PIO 与前缀轮换」时应查 [RFC 8978] 与 [RFC 9096]，不要按相近编号误引。下表把四者的位置一次列清，避免读者照着错误编号去找条文。年份、BCP 编号与「是否 obsoletes / updates 其他 RFC」以 RFC Editor 官方页面为准。

[RFC 9096] 与 PIO 直接相关的建议集中在第 3.4 / 3.5 节：

- PIO 的 Preferred / Valid Lifetime **不得超过**从上游 DHCPv6-PD 学到的对应剩余寿命；CE 路由器 SHOULD 用**较短**的 PIO 寿命，因为后续 RA 会不断续期，效果等价于上游长寿命，同时把「主机可能持有陈旧配置」的窗口缩短。
- 一旦前缀变为陈旧（stale），CE 路由器 MUST 继续以 PIO 广告它，且把 **Valid Lifetime 与 Preferred Lifetime 都置 0**、A / L 位保持不变；这类广告 MUST 持续至少先前用过的 Valid Lifetime 那么久，并用非请求 RA 发送。
- 收到上游 `valid-lifetime=0` 的 IA Prefix 时，也要把对应子前缀以双 0 的 PIO 广告出去。

### 对实现的约束

把这一节与 18.3 并排读，才看得出 RFC 族的完整设计意图：**撤销不是「发一次就删」，而是「在足够长的时间里持续声明它已作废」**。因为 [RFC 4862] §5.5.3 保证单次短寿命 RA 压不掉长寿命地址，唯一可靠的做法是从源头把 PIO 寿命设短（让地址本来就不会有几天寿命），并且把撤销声明持续发到覆盖旧寿命。这与 18.4 里 RDNSS `lifetime=0` 必须持续重复发送，是同一个工程结论在两种选项上的投影。

对 DNS 侧的约束是：解析器地址若用 GUA，前缀轮换时 RDNSS 里那个地址会随前缀一起失效，客户端的 DNS Server List 里留着一条不可达条目；改用 ULA 就绕开了整条链。这是「ULA 做 DNS 地址」在 RFC 层面的第二个理由。

### 常见误读

- **「PIO lifetime=0 能立刻清掉旧地址」**：[RFC 4862] 不允许；[RFC 9096] 的对策是缩短 PIO 寿命 + 持续撤销，而不是依赖单次双 0。
- **「把 PIO 寿命设得跟上游一样长最稳妥」**：[RFC 9096] 的建议正相反，长寿命会放大陈旧配置窗口。
- **「重编号只影响地址，不影响 DNS」**：RDNSS 里若放 GUA，客户端的解析器地址同样失效。

## 横向对比：同一组 RFC 在六种实现里的行为差异

这一节只比较**协议行为**：谁构造并发送 RA、谁消费 RA 里的 RDNSS、谁做 DNSSEC 验证、谁参与 DDR（Discovery of Designated Resolvers，[RFC 9462]）。产品的定位、性能、功能清单在第 2 章已有对照表，这里不重复，也不贴配置。

先立一条最容易被忽略的划分：**DNS 服务器软件与 RA 发送者是两个完全不同的角色**。RA 是 ICMPv6 消息，由内核网络栈或专门的 RA 守护进程（如 `radvd` 一类）构造；BIND、Unbound、CoreDNS 这类进程工作在 DNS 层，既不解析 RA，也不发 RA。把「我装了 Unbound」当成「客户端就会用它」，是这一章所有误读里代价最大的一个。

### 谁发 RA / 谁消费 RDNSS

| 实现 | 发 RA（构造 PIO / RIO / RDNSS 选项） | 消费 RDNSS（把 RA 里的 DNS 地址装进存根解析器） | 条文落点 |
|---|---|---|---|
| **BIND** | 否，纯 DNS 进程 | 否；其所在主机由操作系统的网络栈决定 resolver | [RFC 8106] 的主机侧行为不在 BIND 内 |
| **dnsmasq** | **可发**：内置 RA 能力，可广告 RDNSS / DNSSL / PIO | 否（作为本机进程它读系统 resolver 配置，不是 RA 消费者） | 少数把「DNS 服务」与「RA 广告」放在同一进程的实现 |
| **systemd-resolved** | 否 | **是**（配合 systemd-networkd / NetworkManager 接收 RA，维护 per-link DNS 服务器列表） | 直接实现 [RFC 8106] §6.1/§6.2 的 DNS Server List 语义 |
| **CoreDNS** | 否 | 否；集群内作为被指向的 DNS 端点 | Pod 的 DNS 由 Kubernetes 注入，与 RA 无关 |
| **Unbound** | 否 | 否；它是被 RDNSS 广告的**目标地址** | 依赖外部 RA 发送者把 ULA 地址写进客户端 |
| **路由器固件** | **是**（内核 / `radvd` / 厂商自研 RA 守护进程） | 通常否（作为路由器角色） | [RFC 4861] / [RFC 4191] / [RFC 8106] 的主要实现方 |

由此产生一个很实际的差别：dnsmasq 把「发 RA」和「跑 DNS」绑在一起，改一个进程的生命周期会同时影响客户端的地址配置与 DNS；而 Unbound / BIND / CoreDNS 必须与独立的 RA 发送者配合，两者可以分别升级、分别回滚。第 19 章选择「独立的 RA 发送进程 + 独立的 Unbound 实例」，正是利用了这层解耦。

**谁真正把 RDNSS 装进内核之外的 resolver 仓储**，各家网络栈差异最大：Linux 的 systemd-resolved 按 per-link 维护 DNS 服务器并做路由域分流；Android、iOS、Windows、macOS 各自有独立的 RA 消费实现，对「由第三方撤销别人的 RDNSS 条目」「是否接受链路本地 RDNSS」「hoplimit 校验的严格程度」处理并不完全一致。这些属于实现本地策略，RFC 条文只规定了 MUST 的最小集合，不保证跨栈一致性——所以 18.4 才强调撤销要持续发、验证要按客户端分别做。

### 谁做 DNSSEC 验证

这里的分界必须和全书口径一致：DNSSEC = **数据完整性与来源认证**，DoT / DoH / DoQ = **传输加密与隐私**，互不替代。

| 实现 | DNSSEC 验证角色 | 与 RA / RDNSS 的关系 |
|---|---|---|
| **Unbound** | 完整验证器（validator），信任锚 + 签名校验，坏签名 → SERVFAIL | 自己不收 RA；作为 RDNSS 广告地址的目标 |
| **BIND** | 可作验证器（也可作权威服务器） | 同上，与 RA 无耦合 |
| **dnsmasq** | 支持 DNSSEC 验证（通常作为转发侧的小型验证器，能力定位见官方手册） | 是唯一同时发 RA 且能做验证的常见组合 |
| **systemd-resolved** | 支持 DNSSEC，但默认行为与降级策略按发行版配置而异，见官方手册 | 它是 RDNSS 的消费者，验证发生在同一进程内 |
| **CoreDNS** | 本身不做完整递归验证；可由插件链把验证交给上游 | 集群内服务端点，与 RA 无关 |
| **路由器固件** | 绝大多数只做转发 / 代理，不做验证 | 它是 RDNSS 的**广告者**，广告谁就决定了客户端把验证托付给谁 |

关键结论：**RDNSS 选项里只放得下 IPv6 地址，放不下「这个解析器会不会验证」这种能力声明**。客户端无法从 RA 里得知对端是不是验证器。所以「全屋 DNSSEC 验证」必须由网关侧保证 RDNSS 指向的那个实例确实在做验证，而不是靠客户端选择；第 10 章的三种验证状态在这里同样适用。

同理，RA 里也没有「加密传输」能力字段。DDR（[RFC 9462]）与 DoT / DoH / DoQ 解析器的自动发现是 DNS / HTTP 层的另一套机制（第 17 章讲协议对比，第 13 章讲压制），与 RDNSS 是两条独立通道。

### 谁支持 DDR

| 实现 | DDR 相关行为 | 说明 |
|---|---|---|
| **路由器固件** | 一般不参与；DDR 是主机与指定解析器之间的发现机制 | 但固件若提供 DNS 代理，可能成为客户端绕过本地解析器的路径 |
| **systemd-resolved** | 与网络级「指定解析器 / 每个链路的加密解析器配置」关联最紧，具体支持版本见官方手册 | 它同时是 RDNSS 消费者，因此是两条通道的交汇点 |
| **BIND / Unbound / CoreDNS / dnsmasq** | 是否为 DDR 提供发现侧响应取决于版本与配置，见各自官方手册 | 这些进程主要扮演「被发现的解析器」，不是发起发现的客户端 |

DDR 与本章主线的关系是一处**绕过风险**：RDNSS 把客户端指向了网关 ULA 上的 Unbound，但客户端若通过 DDR 自动发现某个加密解析器并改用 DoH / DoT，网关侧的过滤、日志与验证就一起被绕过。第 13 章的 `local-zone` 压制与 canary 域是工程对策；本章只指出协议层的成因——**RA 只规定了「问谁」，没有规定「必须用明文 53 问、且不许另找别人」**。

### 三组条文 × 实现职责速查

| 条文 | 由谁实现 | 常见缺口 |
|---|---|---|
| [RFC 4861] hop limit 255 / 有效性检查 | 主机内核 + 路由器固件 / RA 发送者 | 中间设备改写 hoplimit；SLLAO 被误当必填 |
| [RFC 4862] §5.5.3 的 2 小时下限 | 主机内核 | 误以为 PIO `lifetime=0` 立即删地址 |
| [RFC 8106] RDNSS + Lifetime=0 撤销 | systemd-resolved 等主机网络栈（消费）、dnsmasq / 固件（发送） | 撤销只发一次；跨栈对第三方撤销接受度不一 |
| [RFC 4191] RIO / `Prf` | 主机网络栈（Type A/B/C 支持度不同）、路由器固件 | Android `/48` 下限被当成 RFC 条文 |
| [RFC 4941] / [RFC 8981] 临时地址 | 主机内核 | 前缀轮换后旧临时地址仍在 preferred 状态 |
| [RFC 6724] Rule 3 / 7 | 主机内核 + 本地策略表 | 把旧版 [RFC 3484] 的规则编号套到现行实现 |
| [RFC 8200] 1280 最小 MTU | 全链路 | EDNS 载荷超过 1232 导致分片被丢 |
| [RFC 9462] DDR | 客户端网络栈 + 解析器侧 | 与 RDNSS 并行造成绕过 |

这张表要传达的不是「谁功能多」，而是**一条 RA 从构造到被消费，中途至少跨了三四个互不知情的实现**。RA 发送者按 [RFC 4861] 置 255，主机内核按 [RFC 4862] 决定地址寿命，用户态 resolved 按 [RFC 8106] 装 DNS 列表，最后应用按 [RFC 6724] 选源地址——任何一环的本地策略差异，都不会在别处留下显眼报错。排查 IPv6 DNS 问题时先看报文、再看客户端状态，而不是先怀疑 Unbound 配置，原因就在这里。

部署侧的可复制命令、验收清单与回滚点在第 19 章；第 20、21 章把这套分工搬到集群与「一台家庭网关 + 一台云主机」的实验环境；真实故障的教学案例集中在第 22 章。本章到此只回答「为什么这么规定」。

## [RFC 6724] 的另一面：`getaddrinfo()` 的返回顺序

本章前面谈 [RFC 6724] 时，重点在**选哪个源地址**。但同一套策略还决定了另一件更容易被忽视的事：应用调用 `getaddrinfo()` 时，拿到的**候选目的地址列表是有顺序的**，双栈域名下 v6 往往排在 v4 前面。程序若只取前几个（例如常见写法 `infos[:3]`）就只试了 v6，于是「解析成功」和「能连上」变成两件事。

一个已脱敏的真实形态：某发片脚本在开工前对某国内平台域名做 `socket.getaddrinfo(host, 443)` + TCP 探测，只试前三个返回地址；该域名前 4 个全是 IPv6 且在这条链路上被 RST（`ECONNREFUSED`），后面 5 个 IPv4 全通。结果探测误报「TCP 连不上」，而浏览器走 v4 正常打开页面 —— 日志于是出现「报故障 → 四秒后网络已恢复」的假振荡。**恢复并没有发生，只是上层页面走了另一条路。**

三条结论要记住：① 探测必须**去重后全量遍历**（或至少 v4 优先），任一地址连通即视为通；② 「已恢复」这类日志只能基于重探测结果，不能只看上层页面能否打开；③ `getaddrinfo` 的顺序是**策略排序**，不是「第一个最可用」。第 36 章会把这个案例放进完整的排查决策表，第 19、22 章分别对应源地址选择与前缀轮换，第 31 章对应告警误报治理。

## 本章要点

- [RFC 4861] 强制所有 NDP 报文 hop limit 为 255 并由接收方校验，这是把 NDP 限制在本链路的协议级安全边界；`Router Lifetime=0` 只撤销默认路由器，不等于关闭 RA；SLLAO 在 RA 中是 MAY。
- [RFC 4862] §5.5.3 e) 明确规定 PIO 的短 / 零 Valid Lifetime 最多把已有的长寿命地址压到 2 小时（或忽略），防的是伪造 RA 造成全网地址瞬间过期的 DoS；因此「发一次 `lifetime=0` 就清掉旧前缀」不成立。[RFC 9096] 的对策是缩短 PIO 寿命并持续双 0 撤销。
- [RFC 8106] 取代 [RFC 6106]：RDNSS / DNSSL 有独立 Lifetime，§6.2 步骤 (b) 规定已存在地址配 `Lifetime=0` 时从 DNS Server List 与 Resolver Repository 中删除；这是「压掉光猫广告的 DNS」的条文依据，但撤销必须持续重复发送。
- [RFC 4191] 的 RIO 与默认路由器 `Prf` 是实现可选能力（Type A/B/C 主机）；Android `accept_ra_rt_info_min_plen=48` 是本地策略而非 RFC 条文，/48 用于路由聚合、/64 用于地址变更检测，两者不可混用。
- [RFC 6724] 里避开 deprecated 是 Rule 3、优先出接口是 Rule 5，Rule 7 已从 [RFC 3484] 的「优先公共地址」反转为「优先临时地址」；配合 [RFC 4941] / [RFC 8981] 的独立寿命时钟，构成前缀轮换后旧临时地址仍被选为源的故障链。
- [RFC 4193] ULA 不随运营商前缀轮换变化，适合作为 RDNSS 里发布的解析器地址；[RFC 8200] 的 1280 字节最小 MTU 是 EDNS 1232 取值的根因。横向对比的关键是角色划分：BIND / Unbound / CoreDNS 既不发 RA 也不消费 RDNSS，dnsmasq 与路由器固件发 RA，systemd-resolved 消费 RDNSS，DNSSEC 验证与 DDR 支持各自独立，互不替代。

## 参考资料

- RFC Editor，[RFC 4291] *IP Version 6 Addressing Architecture*：<https://www.rfc-editor.org/rfc/rfc4291>
- RFC Editor，[RFC 4861] *Neighbor Discovery for IP version 6 (IPv6)*：<https://www.rfc-editor.org/rfc/rfc4861>
- RFC Editor，[RFC 4862] *IPv6 Stateless Address Autoconfiguration*：<https://www.rfc-editor.org/rfc/rfc4862>
- RFC Editor，[RFC 8106] *IPv6 Router Advertisement Options for DNS Configuration*：<https://www.rfc-editor.org/rfc/rfc8106>
- RFC Editor，[RFC 6724] *Default Address Selection for Internet Protocol Version 6 (IPv6)*：<https://www.rfc-editor.org/rfc/rfc6724>
- RFC Editor，[RFC 9096] *Improving the Reaction of Customer Edge Routers to IPv6 Renumbering Events*：<https://www.rfc-editor.org/rfc/rfc9096>
- RFC Editor，[RFC 8200] *Internet Protocol, Version 6 (IPv6) Specification*：<https://www.rfc-editor.org/rfc/rfc8200>
- RFC Editor 官方 RFC 索引（编号、年份、obsoletes / updates 关系以此为准）：<https://www.rfc-editor.org/rfc-index.html>
