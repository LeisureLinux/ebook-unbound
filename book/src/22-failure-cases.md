# 故障案例集

> 本章所有地址、域名、主机名均为**教学用示例**：IPv4 用 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`（[RFC 5737]），IPv6 用 `2001:db8::/32`（[RFC 3849]），ULA 用 `fd00:53::/48`（[RFC 4193]）。`gw-01`、`cloud-01`、`resolver.example.net`、`dns.example.org` 不对应任何真实环境，token 一律写作 `<TOKEN>`。实验环境统一表述为「一台家庭网关 + 一台境外云主机」。

## 案例索引与排查纪律

| # | 一句话现象 | 根因类别 | 关联章节 |
|---|---|---|---|
| ① | 加了第二个上游，过滤表失效 | 转发器语义误读 | 第 5 章 |
| ② | 上游超时后整条查询流裸奔 | 允许静默明文降级 | 第 5、14 章 |
| ③ | 某站 CSS/JS 全部解析失败 | strict QMIN 撞违反 [RFC 8020] 的区 | 第 13、17 章 |
| ④ | 间歇性全量 SERVFAIL | 转发器上配了信任锚 | 第 6、10 章 |
| ⑤ | 权威修好了客户端仍失败 | 负缓存按 SOA minimum 续命 | 第 7 章 |
| ⑥ | 根区镜像永远不更新、AXFR 失败 | 本机解析器出向 53 被自己劫持 | 第 9 章 |
| ⑦ | WireGuard 隧道起不来 | 回包被 `output-tproxy` 打标 | 第 14 章 |
| ⑧ | 换了前缀但 IPv6 全断 | 临时地址寿命 × [RFC 6724] 源地址选择 | 第 19 章 |
| ⑨ | DDNS 与 RA 通知同一天各失效一次 | /48 与 /64 粒度混用 | 第 19 章 |
| ⑩ | 跨境 DNS 时快时慢、UDP 几乎不通 | 跨境 UDP QoS | 第 14、19 章 |

贯穿全部案例的一条经验，先写在最前面：

> **排查顺序：先看 nftables，再怀疑别的。**

十根因里有三根（⑥、⑦，以及 ③ 里「刷了缓存却没生效」的一部分）在规则表里，还有两根（②、⑩）的第一反应会把人引到 DNS 配置上，而真正要核的是规则与链路。策略网关上的一次 `nft list ruleset` 加上计数器清零重测，成本远低于重启解析器、换上游、刷缓存这三件套。下文每个案例的「实测定位」都按这个顺序走。

## 案例 ① 单上游变竞速

- **现象**：为了「冗余」，在策略转发器里加了第二个 `forward-addr`。改完之后，一部分本该被过滤的名字返回了应答，日志里查询出现在两个上游中的任意一个；过滤表时灵时不灵。

- **第一反应（错误判断）**：以为这是主备切换生效——「第二个上游顶上了，说明高可用做对了」。也有人怀疑是过滤表清单加载失败，反复重载清单。

- **实测定位**：先 `nft list ruleset` 确认没有规则把包分流到别处；再在两个上游各自开查询日志，用同一个名字连查十次，发现两边都收到了查询，且哪边先回就用哪边的应答。这不是切换，是并发。

- **根因**：给转发器配第二个 `forward-addr`，Unbound 是**并发竞速取最快**而非主备。查询会被送到不该去的解析器，过滤表只在「恰好选中那个上游」时生效。冗余的语义被误当成故障转移的语义。

- **修复**：转发器只留一个上游，切换交给外部巡检脚本改配置 + reload。

```conf
# /etc/unbound/instance-b.conf —— 转发器只留一个上游
forward-zone:
    name: "."
    forward-addr: 192.0.2.53
    forward-first: no
```

**为什么这么设**：`forward-first: no` 保证转发失败时不退回普通递归（案例 ②），单条 `forward-addr` 保证策略边界只有一个出口。

**怎么验证**：`unbound-control -c /etc/unbound/instance-b.conf stats_noreset | grep num.query`，并在两个候选上游分别抓包，确认只有一个收到查询。

**失败模式 / 回滚点**：只留一个上游意味着该上游故障时这条链路全停，这是有意的取舍，靠外部脚本切换而不是靠竞速。回滚点：`cp -a /etc/unbound/instance-b.conf /var/tmp/instance-b.conf.bak`，改坏时还原并 reload。

- **验证**：连续查 20 次同一个名字，两个上游的查询计数应为 `20 / 0`；过滤域始终返回预设应答。

- **如何避免**：把「多上游」写进变更评审的禁用项；任何「为了冗余加一行 `forward-addr`」的提案，先问一句「这是竞速还是切换」。第 20 章 CoreDNS 的 `forward` 同样适用。

## 案例 ② 静默明文降级

- **现象**：`cloud-01` 上的 DoT 上游超时后，客户端查询仍能成功，只是变慢了一点；抓公网侧的包发现出现了明文 53 的查询，里面是完整 QNAME。没有任何告警。

- **第一反应（错误判断）**：「上游挂了但服务还活着，说明容错做得不错」。甚至有人把「自动回退」当成设计特性写进文档。

- **实测定位**：`nft list ruleset` 看是否有规则放行明文 53 出向；在 WAN 口 `tcpdump -ni wan0 'port 53 or port 853'`；然后 `dig @fd00:53::1 dns.example.org A` 同时停掉 DoT 服务端，捕获到 :53 流量与成功应答同时出现。

- **根因**：上游超时后允许回退到明文 53（或 `forward-first: yes` 之类的宽松设置），fail-closed 没落实。**加密传输（DoT / DoH / DoQ）与 DNSSEC 是两件事**：这里丢的是传输加密与隐私，DNSSEC 的 `ad` 标志可能还在，于是监控看不出异常。

- **修复**：`forward-first: no`、加密上游两波都失败就返回 SERVFAIL，并在 nft 侧拒绝本机解析器之外的明文 53 出向。

```conf
# 转发器：禁止一切向明文递归的逃逸
forward-zone:
    name: "."
    forward-addr: 198.51.100.2@853
    forward-tls-upstream: yes
    forward-first: no
```

**为什么这么设**：`forward-tls-upstream: yes` 把这条上游钉在 TLS 上，`forward-first: no` 关掉「转发失败就退回普通递归」的出口；两者合起来才是 fail-closed。

**怎么验证**：停掉 `cloud-01` 的 DoT 服务后 `dig @fd00:53::1 dns.example.org A` 应返回 SERVFAIL，且 `tcpdump -ni wan0 'port 53'` 无任何新包。

**失败模式 / 回滚点**：fail-closed 的代价是上游故障时**业务真的会失败**，这是设计意图，不要为了可用性再开回退。回滚点：保留原始配置快照；若临时需要应急，用一条有明确到期时间的规则而不是改 `forward-first`，并设 `systemd-run --on-active=10m` 自动撤销。

- **验证**：注入上游断开，确认「无明文 53 出公网 + 客户端 SERVFAIL」两条同时成立；恢复 DoT 后查询恢复且 `ad` 标志正常。

- **如何避免**：把「明文 53 出向包数」做成常驻告警；任何健康检查只测「能否解析」而不测「是否加密」，都会漏掉这类降级。

## 案例 ③ strict QMIN 撞违反 RFC 8020 的 CDN 区

- **现象**：开启 `qname-minimisation-strict: yes` 后，某大型 CDN 承载的整站 CSS / JS / 图标全部解析失败，页面只剩纯文本；主域却能解析。关掉 strict 之后**仍然失败**，重启解析器之后**还是失败**。

- **第一反应（错误判断）**：「CDN 节点故障」「运营商劫持」「strict 关了没生效，配置没重载」。于是反复 reload、换上游、清客户端 DNS 缓存，问题依旧。

- **实测定位**：`nft list ruleset` 排除规则因素；然后逐层 `dig`：`dig @<NodeLocal> +short asset.dns.example.org.`、`dig @<CoreDNS> ...`、`dig @<Unbound> ...`，发现**每一层**都返回 NXDOMAIN，但最外层向权威直接查却能得到正确 CNAME。定位到缓存，而不是配置。

- **根因**：查询名最小化（QNAME minimisation，QMIN）在 strict 模式下，问中间层拿到的 NXDOMAIN 不回退到完整域名。而该 CDN 的 CNAME 区违反 [RFC 8020]（父名返回 NXDOMAIN、子名却存在），于是整片静态资源被判为不存在。更关键的是：NXDOMAIN 已经作为负缓存写进了**链路上每一层**——NodeLocal、中心 CoreDNS、Unbound、以及应用自身的解析器。只改配置、只刷一层，前一层照样吐缓存的 NXDOMAIN。

- **修复**：把 `qname-minimisation-strict` 改回 `no`（保留 QMIN 本身），然后按 `NodeLocal → CoreDNS → Unbound` 的顺序逐层刷负缓存。

```conf
# 兼容优先：不开 strict
server:
    qname-minimisation: yes
    qname-minimisation-strict: no
```

**为什么这么设**：QMIN 仍保留（减少发给上级的 QNAME 标签数，抗元数据聚合），但允许在遇到坏区时回退到完整域名。注意 QMIN **不是加密**：它不抗全路径嗅探；本地根镜像时根这一跳本来就不出口，收益落在 TLD 层。

**怎么验证**：`dig @192.0.2.53 +short asset.dns.example.org.` 返回 CNAME；`unbound-control -c /etc/unbound/instance-d.conf get_option qname-minimisation-strict` 显示 no（若 `get_option` 不支持则直接查配置文件）。

**失败模式 / 回滚点**：关 strict 会略微增加发送给上级的标签数，这是兼容性代价。回滚点：配置文件快照 + 逐层刷缓存脚本；**不要**用「重启单个实例」代替逐层刷。

```bash
# 逐层刷，顺序从离客户端最近的一层开始
kubectl -n kube-system rollout restart ds/node-local-dns     # 若有 NodeLocal（第 20 章）
kubectl -n kube-system rollout restart deploy/coredns        # 若有 CoreDNS
unbound-control -c /etc/unbound/instance-a.conf flush_negative
unbound-control -c /etc/unbound/instance-d.conf flush_negative
unbound-control -c /etc/unbound/instance-d.conf flush_zone dns.example.org
```

**为什么这么设**：从离客户端最近的一层开始清，避免清完最外层后近端缓存又把旧 NXDOMAIN 吐出来（案例 ③、⑤ 的共同要求）。

**怎么验证**：`dig @192.0.2.10 +short asset.dns.example.org.`、`dig @<CoreDNS-ClusterIP> +short asset.dns.example.org.`、`dig @192.0.2.53 +short asset.dns.example.org.` 三层都返回 CNAME。

**失败模式 / 回滚点**：`unbound-control` 未启用远程控制时只能重启实例，且每个实例都要刷；`rollout restart` 会让该层短暂降级，滚动窗口内要保证 `serve-expired` 开着。回滚点：刷缓存前的 `dump_cache` 输出与 ConfigMap 快照。

- **验证**：每一层分别 `dig @<该层地址> +short asset.dns.example.org.` 都返回 CNAME；页面静态资源加载完整。

- **如何避免**：默认不开 strict；把它列为「只在确认全链路名字空间都合规时才考虑」的选项。凡是出现「NXDOMAIN 修不好」，先列出链路上所有缓存层再动手，逐层刷、逐层验。

## 案例 ④ 转发器上配 trust anchor 导致 priming 失败

- **现象**：某个实例间歇性全量 SERVFAIL，日志里持续刷 `failed to prime trust anchor`；重启后短暂正常，几分钟后又坏。

- **第一反应（错误判断）**：「根区镜像坏了」「信任锚文件过期」「上游 DNSKEY 应答被丢」，于是反复换 `root.hints`、重新下载锚文件。

- **实测定位**：`nft list ruleset` 排除出向 53 被劫持（那是案例 ⑥）后，看该实例的 `forward-zone` 与 `server:` 段，发现两者同时存在：`forward-zone "."` 把一切交给上游，而 `auto-trust-anchor-file` 要求自己向根 priming DNSKEY。抓包确认该实例从未向根发过 DNSKEY 查询。

- **根因**：信任锚（trust anchor）需要验证器自己向根 priming DNSKEY，而纯转发器把全部查询交给上游，无法完成 priming。**转发器与验证器二选一**，不能在同一实例里既全量转发又做验证。

- **修复**：拆成两个实例——纯转发器去掉所有验证器选项，递归验证器去掉 `forward-zone "."` 并自己持有信任锚（配置见下方）。

```conf
# 方案 A：纯转发器，去掉所有验证器选项
# server: 段里不要出现 auto-trust-anchor-file / val-permissive-mode
forward-zone:
    name: "."
    forward-first: no
    forward-addr: 192.0.2.53

# 方案 B：递归验证器，去掉 forward-zone "."，自己迭代并持有信任锚
# server:
#     auto-trust-anchor-file: "/var/lib/unbound/root.key"
# auth-zone:
#     name: "."
#     ...（本地根镜像见第 6 章）
```

**为什么这么设**：把两个角色拆到不同实例（第 7 章的多实例分工），各自只需满足自己那套前提；方案 B 的递归能力由本地根镜像支撑，不依赖转发上游。

**怎么验证**：`journalctl -u unbound@instance-a --since '10 min ago' | grep -i 'prime\|bogus'` 无新记录；`dig @192.0.2.53 +dnssec badsig.dns.example.org A` 在验证器侧返回 SERVFAIL、在转发器侧不假装验证。

**失败模式 / 回滚点**：拆成两个实例后，转发器的应答不带 `ad` 标志是**正常的**（它没有验证），别误判成 DNSSEC 失效。回滚点：两个实例的配置文件快照，以及 `/var/lib/unbound/root.key` 的备份。

- **验证**：连续观察 30 分钟无 `failed to prime`；坏签名域只在验证器那条链路上被拒。

- **如何避免**：配置模板按角色分离，转发器模板里物理删除验证器选项，不要靠注释「以后可能要用」。第 20 章强调的「DNSSEC 验证放在 Unbound 那一层」在裸机拓扑里同样成立。

## 案例 ⑤ 负缓存放大

- **现象**：上游权威瞬时返回过一次 NXDOMAIN，运维修好权威之后，客户端仍然持续失败约一小时；不同客户端「有的好了有的没好」。

- **第一反应（错误判断）**：「权威没修干净」「传播延迟」「客户端自己缓存了」，于是反复改权威记录、清浏览器缓存。

- **实测定位**：`nft list ruleset` 排除规则后，逐层 `dig +nssearch` 或直接 `dig @<每层> no-such.dns.example.org.`，看哪一层还在返回 NXDOMAIN；再 `unbound-control ... dump_cache | grep -A3 no-such` 看到带 SOA 的负缓存条目及其剩余 TTL。

- **根因**：上游瞬时 NXDOMAIN 被按 SOA minimum 缓存（常见 3600 秒），`cache-max-negative-ttl` 官方默认也是 3600。修好权威不会让已有负缓存失效。且**链路上每个实例都有自己的负缓存**，只刷一个实例就会看到「有的客户端好了」。

- **修复**：把 `cache-max-negative-ttl` 压到 60 秒，并用 `flush_negative` / `flush_zone` 逐实例刷掉已经写进去的负缓存（配置见下方）。

```conf
# 缩短负缓存上限；不要指望关掉它（NXDOMAIN flood 需要它）
server:
    cache-max-negative-ttl: 60
    # 出问题时用 unbound-control flush_negative / flush_zone 逐实例刷
```

**为什么这么设**：把负缓存从 3600 秒压到 60 秒，让「修好权威」到「客户端恢复」的窗口可控；仍然保留负缓存以吸收不存在的名字查询。

**怎么验证**：`dig @192.0.2.53 +short no-such.dns.example.org.` 后立刻 `unbound-control -c /etc/unbound/instance-d.conf flush_negative`，再查应改为向上游发起新查询；`unbound-control ... dump_cache` 里该条目消失。

**失败模式 / 回滚点**：`unbound-control` 没启用远程控制时**只能重启实例刷缓存**，且每个实例都要刷。压太短（例如 1 秒）会让不存在的名字查询打穿到上游，遇扫描流量时放大。回滚点：配置快照 + 一份 `dump_cache` 输出便于对照。

- **验证**：修好权威后 60 秒内所有实例都返回正确应答；逐层 `dig` 结果一致。

- **如何避免**：把「逐层刷缓存」写成固定 runbook 步骤；第 20 章的 CoreDNS `cache { denial ... 5 }` 与 Unbound 的 `cache-max-negative-ttl` 要**分别**设置，别以为设了一处就全链路生效。

## 案例 ⑥ 本机解析器出向 53 被自己 hijack

- **现象**：本地根区镜像永远不更新，AXFR 失败；递归查询时好时坏。同一台网关上，LAN 客户端的 DNS 却完全正常。

- **第一反应（错误判断）**：「上游根服务器不通」「AXFR 的 TSIG 密钥错了」「镜像源站改了地址」。有人甚至把 `<TOKEN>` 换了三遍。

- **实测定位**：**先看 nftables**——`nft list ruleset | grep -A6 hijack-dns`，发现 LAN/WG 的 :53 一律送进 tproxy，而规则里没有对本机解析器进程的 uid 豁免。再 `nft reset counters` 后发起一次递归，看到 hijack 计数增加，而 WAN 口 :53 出向计数为零：自己的递归查询被自己吃掉了。

- **根因**：策略网关把 LAN/WG 的 :53 送进 tproxy 是为了兜住写死 :53 的存根解析器，但**本机解析器进程（unbound、nsd）的出向 53 必须按 uid 豁免**，否则它自己的递归查询又被劫持回自己，形成环。

- **修复**：在 `hijack-dns` 链的 tproxy 规则之前按 uid 插入豁免，例如 `meta skuid $UNBOUND_UID accept` / `meta skuid $NSD_UID accept`（`$*_UID` 为运行时查到的数值 uid，骨架见下方）。

```text
# nft 策略层骨架：uid 豁免必须写在 tproxy 之前
table inet mangle {
  chain hijack-dns {
    meta skuid 953 accept      # 示例：unbound 服务用户的数值 uid，按实际替换
    meta skuid 954 accept      # 示例：nsd 服务用户的数值 uid，按实际替换
    # 其余 LAN/WG 的 :53 才进 tproxy
    udp dport 53 meta l4proto udp tproxy to :5355 meta mark set 0x2333
  }
  chain output-tproxy {
    # 链首：WG 自己的出包显式放行（案例 ⑦）
    udp sport 51820 accept
    meta skuid 953 accept      # 与 hijack-dns 同一数值 uid 示例
    # ... 其余规则
    meta mark set 0x2332
  }
}
```

**为什么这么设**：策略网关把 LAN/WG 的 53 送进 tproxy 是为了兜住写死 :53 的存根解析器，但**本机解析器进程（unbound、nsd）的出向 53 必须按 uid 豁免**，否则它自己的递归查询又被劫持回自己，形成环。

**怎么验证**：`nft reset counters` 后 `dig @127.0.0.1 -p 5359 +short dns.example.org A`，hijack-dns 计数不增加，WAN 口 `tcpdump -ni wan0 'port 53'` 能看到出向查询。

**失败模式 / 回滚点**：`meta skuid 0` 匹配不到内核发出的包（案例 ⑦ 会再踩一次）；`meta skuid` 匹配的是数值 uid，上线前用 `id -u unbound` / `id -u nsd` 查真实值替换上面的示例数字，写错会静默不命中。回滚点：`nft list ruleset > /etc/nftables.conf.bak`，出问题时 `nft -f /etc/nftables.conf.bak`。

- **验证**：根区镜像的 SOA 序列号随权威更新；AXFR 成功；递归查询稳定。

- **如何避免**：把 uid 豁免写进 hijack 规则模板的第一条，任何新增本机解析服务时同步加豁免。这条经验本身就是本章开头那句「先看 nftables」的由来。

## 案例 ⑦ WG 回包被 output-tproxy 打标

- **现象**：WireGuard 隧道起不来，握手反复重试；`wg show` 里 latest handshake 一直是空。ping 隧道对端地址不通，但网关本身出公网正常。

- **第一反应（错误判断）**：「云主机防火墙没放行 UDP」「密钥配错了」「运营商封了 UDP」。于是换端口、换密钥、换云主机区域。

- **实测定位**：**先看 nftables**——`nft list chain inet mangle output-tproxy`，发现链尾有 `meta mark set 0x2332`，把 WG 自己的 `udp sport 51820` 出包也一并打标送进代理。抓包看到握手包发出去了，但响应回不来；对端日志显示从未收到可用的握手响应。

- **根因**：策略网关的 `output-tproxy` 链尾统一打标，连内核为 WG 发出的报文一起命中。试过用 `meta skuid 0` 排除，**匹配不到内核发出的这类包**，因为这类包没有用户态 socket 归属。必须在链首按 `udp sport` 显式 accept（见案例 ⑥ 的规则骨架）。

- **修复**：把 `udp sport 51820 accept` 放到 `output-tproxy` 链首，放在链尾的 `meta mark set 0x2332` 之前（骨架见下方）。

```text
# output-tproxy 链首，顺序不可交换
udp sport 51820 accept
# 下面才是普通的本机出向策略与链尾打标
meta mark set 0x2332
```

**为什么这么设**：按源端口做无状态例外，不依赖 socket 归属，能覆盖内核发出的握手响应；放在链首保证不会被后面的打标规则追上。

**怎么验证**：`nft reset counters` 后 `wg show wg0 | grep -i handshake` 应出现时间戳；`tcpdump -ni wan0 'udp port 51820'` 看到双向报文。

**失败模式 / 回滚点**：把 `udp sport 51820 accept` 放到链尾就没用了，链尾打标会先命中。若隧道端口是变量，用 `define WG_PORT = 51820` 并在规则里引用，避免改端口时漏改。回滚点：`/etc/nftables.conf.bak`。

- **验证**：`wg show` 有 latest handshake；`ping` 隧道对端通；隧道内 DoT 查询成功。

- **如何避免**：任何「统一打标」的链尾规则上线前，列出本机所有由内核发起的出向协议（WG、IPsec、NDP 相关）逐条做链首例外。案例 ⑥ 和 ⑦ 是同一条纪律的两个侧面：**先看 nftables，再怀疑别的**。

## 案例 ⑧ 临时地址寿命 × 前缀轮换

- **现象**：运营商换 IPv6 前缀之后，客户端拿到了新前缀、配了新地址、默认路由也正确，但 IPv6 出站全断；`ping6` 网关 ULA 却通。约两天后自行恢复。

- **第一反应（错误判断）**：「运营商路由没收敛」「上游不回程路由」「新前缀没下发完整」。于是等、重启光猫、改默认路由，都没用。

- **实测定位**：`nft list ruleset` 排除出向过滤后，`ip -6 addr show dev lan0 scope global` 看到新旧两个 /64 地址并存；`ip -6 route get 2001:db8::1` 显示**源地址仍是旧前缀**。抓包确认出向包源地址属于已撤销的旧 /64。

- **根因**：[RFC 4941] 隐私扩展生成的临时地址，Linux 上 `temp_valid_lft` 默认 48 小时。换成新前缀后旧前缀的临时地址仍存活，而 [RFC 6724] 的地址选择规则**优先选临时地址作源地址**，于是上游不路由回包。所谓「两天后自行恢复」正是旧地址寿命耗尽。

- **修复**：把 `temp_valid_lft` 压到 1800 秒，且 `all.*` / `default.*` / 接口级三处都要设（配置见下方，完整脚本见第 19 章）。

```bash
# /etc/sysctl.d/90-ipv6-privacy.conf
net.ipv6.conf.all.use_tempaddr       = 2
net.ipv6.conf.default.use_tempaddr   = 2
net.ipv6.conf.all.temp_valid_lft     = 1800
net.ipv6.conf.default.temp_valid_lft = 1800
```

**为什么这么设**：`use_tempaddr = 2` 生成并优先使用临时地址；`temp_valid_lft = 1800` 把旧前缀残留从 48 小时压到 30 分钟，把故障窗口收进可接受范围。

**怎么验证**：`sysctl net.ipv6.conf.lan0.temp_valid_lft` 必须返回 `1800`——**只查 `all.*` 不算数**。

**失败模式 / 回滚点**：这份文件只对之后新建的接口生效，`all.*` 不回溯改写已存在的接口；NetworkManager 连接激活时还会把 `use_tempaddr` 写成 `-1` 覆盖。因此 `all.*` / `default.*` / **接口级**三处都要设（完整脚本见第 19 章）。回滚：删除文件 + `sysctl --system`，并把接口级运行时值写回。

- **验证**：轮换演练后 `ip -6 route get 2001:db8::1` 的源地址属于新 /64；30 分钟内旧地址消失。

- **如何避免**：把接口级 sysctl 写进 NetworkManager 的激活钩子，别只依赖 `/etc/sysctl.d/`。注意发零寿命 PIO 清不掉旧地址——[RFC 4862] 第 5.5.3 节不允许把已存在的长寿命地址立刻缩短（STALE PIO 清理在 Linux 上无效），只能靠短寿命加客户端侧主动清理。

## 案例 ⑨ /48 与 /64 粒度

- **现象**：同一天出现两次互不相干的故障。第一次：运营商换前缀后 DDNS 记录没更新，外部访问断。第二次：RA 通知脚本没触发，客户端迟迟拿不到新配置。两次的现象、报错、影响面完全不同。

- **第一反应（错误判断）**：第一次判为「DDNS 服务商 API 限流」，检查 `curl` 与 `<TOKEN>`；第二次判为「RA 发送进程卡死」，重启进程。都短暂好转，很快复发。

- **实测定位**：`nft list ruleset` 排除后，检查两个脚本共用的变量，发现都取自 RIO（Route Information Option）广告的 `2001:db8:1::/48`。而运营商轮换的是 LAN 的 **/64**（`2001:db8:1:1::/64` → `2001:db8:1:9::/64`），/48 聚合始终不变，于是两个「前缀是否变化」的判断都恒为假。

- **根因**：Android 的 `accept_ra_rt_info_min_plen=48` 会丢弃前缀长度小于 48 的 RIO，所以 **RIO 必须用 /48**（或更细）；但**前缀变更检测必须用 PIO 的 /64**。把「路由聚合粒度」和「地址变更粒度」共用一个变量，导致一处错、两处同时错。

- **修复**：把两个脚本的数据源拆开——DDNS 与 RA 通知只读 PIO 的 /64，RIO 的 /48 只用于生成下游路由，命名上就区分 `LAN_PREFIX_64` 与 `ROUTE_AGG_48`。

```bash
# 只从 PIO 取 LAN /64，绝不从 RIO 推导
ip -6 addr show dev lan0 scope global | grep '^    inet6 2001:db8'
# 变更检测的输入：当前 /64；RIO 的 /48 只用于下游路由
```

**为什么这么设**：把两个数据源彻底拆开，DDNS 与 RA 通知各自读自己的那一份，避免一个 /48 变量被两处复用。

**怎么验证**：轮换后 `ip -6 addr show dev lan0 scope global` 的 /64 变化、`ip -6 route show | grep 2001:db8:1::/48` 保持不变，同时 DDNS 记录更新、RA 通知触发——三者同时成立才算修好。

**失败模式 / 回滚点**：若脚本已把 /48 写进 DDNS，先手工更新一次记录再回滚脚本变量；不要靠清缓存解决，负缓存（案例 ⑤）会让旧值续命。回滚点：两个脚本的快照与 DDNS 记录的历史值。

- **验证**：连续两次轮换演练，DDNS 与 RA 通知都各自触发一次，且 RIO 生成的 /48 路由不抖动。

- **如何避免**：变量名里就写清粒度（`LAN_PREFIX_64` 与 `ROUTE_AGG_48`），禁止用泛化的 `PREFIX`。第 19 章把这个坑记为「同一天栽两次」；这类「两个故障一个根因」的情形，值得专门查一次共用变量。

## 案例 ⑩ 跨境 UDP QoS

- **现象**：跨境链路上 DNS 时快时慢，UDP 承载的加密通道几乎不可用；大一点的应答直接超时后走 TCP 重试。同一时间 TCP 上的 SSH 却很流畅。

- **第一反应（错误判断）**：「云主机解析器性能不够」「递归层数太多」「DoT 服务端线程数太小」。于是加副本、调 `num-threads`、换上游，UDP 依旧。

- **实测定位**：`nft list ruleset` 确认没有 mark / 限速规则后，在同一条链路上做对照实测：UDP/QUIC 下载 1MB 只到约 300 B/s，而 SSH SOCKS（TCP）达约 660 KB/s。差别不是解析器，是链路对 UDP 的 QoS。再抓包看是否有 IPv6 分片被丢（叠加 `edns-buffer-size: 1472` 时更明显）。

- **根因**：该链路对跨境 UDP 做了限速/劣化。DNS 与 QUIC 都走 UDP，首当其冲；而 TCP 承载的通道有重传与拥塞控制，反而稳。DNS 隧道同理：**加密传输优先选 TCP 承载的通道**。另外 `edns-buffer-size: 1472` 会在 MTU 1280（WireGuard / IPv6 最小 MTU）与 PPPoE 1492 上分片，分片被丢后等超时重试 TCP，与 QoS 叠加成「双重慢」；官方默认 1232（DNS Flag Day 2020）才是对的，且 **`max-udp-size` 只管入向，出向靠 `edns-buffer-size`**。

- **修复**：加密传输选 TCP 承载的通道（DoT），出向 `edns-buffer-size` 钉在 1232（配置见下方），不要拿 `max-udp-size` 当出向修复。

```conf
# 验证器 / 递归解析器出向尺寸
server:
    edns-buffer-size: 1232
    # max-udp-size 只影响入向，不要拿它当出向修复
```

**为什么这么设**：1232 让大应答在 MTU 1280 的隧道里不分片，去掉「超时→TCP 重试」这一层额外延迟；配合 TCP 承载的加密通道，绕开 UDP QoS。

**怎么验证**：`dig @192.0.2.53 +bufsize=4096 +dnssec dns.example.org DNSKEY` 一次返回，抓包里无分片头、无 53/TCP 重试；`unbound-control ... get_option edns-buffer-size` 为 1232。

**失败模式 / 回滚点**：把 1232 再往下压（例如 512）会产生大量 TCP fallback，属于过度修正。回滚点：配置快照；若临时必须走 UDP，给它单独一条链路而不是改全局尺寸。

- **验证**：同一链路上 DNS 平均时延稳定；大 DNSKEY 应答不触发 TCP 重试。

- **如何避免**：第 14 章的加密通道选路把「链路对 UDP 的待遇」当作一等输入；选 DoT（TCP 853）而非 QUIC/DoQ 承载的 UDP 通道时，先做一次 1MB 对照实测，别凭协议理论下结论。第 19 章那次「为改 DNS 去改光猫工作模式」踩到的「ICMP 通、TCP 握手通、数据阶段静默丢包」也是同一类教训：**可控性交给别人的数据面，排查成本会翻倍**。

## 跨案例的三条通用纪律

1. **先看 nftables，再怀疑别的。** 十例里 ⑥⑦ 直接是规则表问题，②⑩ 的第一反应会被规则/链路误导，③⑤ 的「刷了没生效」也要先确认没有规则把查询引回旧路径。`nft list ruleset` + `nft reset counters` 重测，是成本最低的第一步。
2. **缓存是有层的，修复必须逐层。** ③（NXDOMAIN）与 ⑤（负缓存）都要求 NodeLocal / CoreDNS / Unbound / 应用解析器逐层刷、逐层验；只刷最外层会看到「明明修好了却还在失败」。
3. **先分清两种「安全」，再谈降级。** DNSSEC 是数据完整性/来源认证，DoT/DoH/DoQ 是传输加密/隐私，二者不互相替代。案例 ② 里 `ad` 标志还在却已经明文裸奔，案例 ④ 里转发器假装验证反而全量 SERVFAIL——都是把两件事混成一件的结果。

另有一条配置层面的底线：`harden-below-nxdomain` 只对 DNSSEC-secure 的 NXDOMAIN 生效（官方 man page 的 NOTE），无签名区的 NXDOMAIN 不触发剪枝，别把它当成「已生效」的防护；`harden-glue` 与 DNSSEC 无关，域外夹带的 glue 是经典缓存投毒面，任何模式都该开。这两条不改变本章任何案例的结论，但会影响你对「防护是否到位」的判断。

## 本章要点

1. **排查顺序：先看 nftables，再怀疑别的**——规则表能解释案例 ⑥（本机出向 53 被自己 hijack）、⑦（WG 回包被 `output-tproxy` 打标）以及多处「刷缓存不生效」。
2. **转发器只留一个上游、fail-closed**：多上游是并发竞速不是主备（①）；允许回退明文 53 会造成无人告警的静默降级（②）。
3. **缓存分层**：strict QMIN 撞违反 [RFC 8020] 的 CDN 区（③）与负缓存按 SOA minimum 续命（⑤），都要求逐层刷、逐层验；`serve-expired-client-timeout` 单位是毫秒，`max-udp-size` 只管入向。
4. **角色二选一**：转发器上配 `auto-trust-anchor-file` 无法完成 priming（④）；DNSSEC 验证放在验证器那一层，与加密传输不是一回事。
5. **IPv6 两处粒度**：临时地址默认 48h 寿命叠加 [RFC 6724] 源地址优选，前缀轮换后要三处 sysctl 都设（⑧）；RIO 用 /48、变更检测用 /64，共用变量会一天栽两次（⑨）。
6. **链路实测优先于协议推断**：跨境 UDP QoS 下 TCP 承载更稳（⑩），`edns-buffer-size: 1232` 避免 MTU 1280 分片；「光猫只当管道用」（第 19 章）是同一条可控性纪律。

## 参考资料

- Unbound 配置手册（`forward-first`、`edns-buffer-size`、`harden-below-nxdomain`、`qname-minimisation-strict`、`cache-max-negative-ttl` 等选项与 NOTE）：<https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html>
- Unbound `unbound-control` 手册（`flush_negative` / `flush_zone` / `dump_cache`）：<https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html>
- RFC 8020, *NXDOMAIN: There Really Is Nothing Underneath*：<https://www.rfc-editor.org/rfc/rfc8020>
- RFC 9156, *DNS Query Name Minimisation to Improve Privacy*（更新 [RFC 7816]）：<https://www.rfc-editor.org/rfc/rfc9156>
- RFC 4862 第 5.5.3 节（地址寿命与 STALE PIO）：<https://www.rfc-editor.org/rfc/rfc4862>
- Linux 内核 `ip-sysctl` 文档（临时地址与 RA 相关 sysctl）：<https://docs.kernel.org/networking/ip-sysctl.html>
- nftables 官方 wiki（规则集、计数器与 mark）：<https://wiki.nftables.org/wiki-nftables/index.php/Main_Page>
- DNS Flag Day 2020（EDNS 缓冲区尺寸 1232 的建议）：<https://dnsflagday.net/2020/>
