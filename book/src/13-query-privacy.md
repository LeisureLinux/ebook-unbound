# 查询隐私：QMIN、防降级与 DDR 压制

一台递归解析器向权威侧发出的迭代查询，默认会把完整查询名（QNAME）逐级送出去。查询名最小化（QNAME minimisation，QMIN）要减少的正是这份元数据；防降级要保证加密链路不会被静默剥掉；DDR 压制要保证客户端不会绕过本地解析器直连外部端点。这三条是**同一件事的三个方向**：谁看到了查询、链路有没有被剥掉、查询有没有走别的路。

本章只处理第三篇的「怎么做」：参数、命令、验证与失败模式。QMIN 的协议演进（[RFC 7816] → [RFC 9156]）、DoT/DoH/DoQ 的 RFC 对比放在第四篇第 17 章。以下地址均为教学用示例地址，域名用 `dns.example.org`、`resolver.example.net`，凭据写作 `<TOKEN>`。

## 13.1 QMIN 的动机与边界

### 13.1.1 它抗的是「元数据聚合」，不是「全路径嗅探」

未开 QMIN 时，解析器查 `assets.cdn.example.org A`，向根、TLD、以及该域的权威服务器都会带上完整 QNAME。这些服务器各自只负责命名空间的一部分，本不需要知道完整名字，但完整名字让它们能长期积累「谁在查什么」的元数据。QMIN 的做法是：向每一级只发**推进解析所必需的最少标签数**，并在可能时把 QTYPE 改成 `A`，只在需要最终答案的那一跳才发完整 QNAME 与原始 QTYPE。

```yaml
# /etc/unbound/unbound.conf.d/10-qmin.conf
server:
    qname-minimisation: yes
    qname-minimisation-strict: no
```

**为什么这么设**：`qname-minimisation: yes` 是官方手册的默认值，语义是**尽力而为（best effort）**——上游若返回 NOERROR 以外的 RCODE，Unbound 会回退成发送完整 QNAME 与原始 QTYPE；`qname-minimisation-strict: no` 保留这个回退（默认值也是 `no`）。这一对组合是「隐私收益」与「兼容性」的平衡点。

**怎么验证**：

```bash
unbound-control get_option qname-minimisation
unbound-control get_option qname-minimisation-strict
tcpdump -ni any 'udp port 53 and dst net 192.0.2.0/24' -c 30 -A | grep -i 'example\|cdn' || true
dig @127.0.0.1 -p 5353 assets.cdn.example.org A +short
```

**失败模式 / 回滚点**：`get_option` 在某些构建里可能不可用，此时以 `unbound-control stats_noreset` 与配置文件为准，不要凭「配了就生效」下结论。抓包里若仍看到完整 QNAME 出现在 TLD 那一跳，先确认查询是不是命中了缓存（命中缓存不出向）。回滚：把 `strict` 明确写成 `no`，别只删掉一行——默认值随版本可能变化。

### 13.1.2 三条必须写明的边界

1. **QMIN 不是加密**。它只减少发给上级的 QNAME 标签数，报文仍在链路上以明文（或解密后的形式）存在。能观察完整路径的人依旧能看到查询；QMIN 削减的是「每一级权威服务器各自能聚合到多少元数据」。
2. **`harden-below-nxdomain` 只对 DNSSEC-secure 的 NXDOMAIN 生效**。官方手册的 NOTE 明确写了这一点：只有 DNSSEC 验证过的 NXDOMAIN 才会用于剪枝（且 NSEC3 opt-out 不够）。无信任锚、无签名区里的 NXDOMAIN 不触发，所以它在「没有验证器」的部署里是空转，别误判成「已生效」。
3. **本地根区镜像时，根这一跳本来就不出口**。第 6 章的本地根镜像让根区的 SOA / NS / glue 全在本机，因此 QMIN 在根层没有可出口的元数据可减；收益主要落在 **TLD 层**（以及其后的委派层）。这一点决定了「要不要为 QMIN 单独改架构」——通常不需要，它是默认开启的渐进收益。

```bash
# 边界②的可观测证据：无信任锚时，NXDOMAIN 剪枝不会触发
unbound-control get_option module-config
dig @127.0.0.1 -p 5353 nonexistent.example.org A | grep 'status:'
unbound-control stats_noreset | grep -E 'num.query.aggressive|num.val' || true
```

**为什么这么设**：把「参数存在」与「机制生效」分开验证。`module-config` 里没有 `validator` 时，DNSSEC-secure 的 NXDOMAIN 无从谈起，剪枝自然不触发。

**怎么验证**：`status: NXDOMAIN` 只说明名字不存在，不说明剪枝生效；要看 `module-config` 是否含 `validator`、`auto-trust-anchor-file` 是否就位（第 10 章）。

**失败模式 / 回滚点**：把 `harden-below-nxdomain: yes` 当成「QMIN 的强力版」是常见误读——它管的是「已知 NXDOMAIN 之下不必再问」，与「向每一级发多少标签」是两件事。回滚：二者独立配置，互不为前置。

## 13.2 `yes` 与 `yes+strict` 的区别

官方手册对 `qname-minimisation-strict` 的定义是：严格模式下**不回退**到向可能有问题的权威服务器发送完整 QNAME，并附了一句很直白的警告——「启用后很多域将无法解析，只在你知道自己在做什么时使用」。

| 行为 | `qname-minimisation: yes`（strict: no） | `qname-minimisation: yes` + `strict: yes` |
|---|---|---|
| 中间层返回 NXDOMAIN | 回退：重新发完整 QNAME 与原始 QTYPE | 直接接受 NXDOMAIN，不再回退 |
| 中间层返回 REFUSED / SERVFAIL 等非 NOERROR | 回退 | 不回退 |
| 兼容性 | 好，能绕过违反 [RFC 8020] 的区 | 差，某些域整站解析失败 |
| 隐私强度 | 略有回退，但仍减少大部分标签 | 最强，无完整 QNAME 外泄 |
| 适用 | 通用递归 / 转发器 | 受控实验、明确知道上游全部合规 |

判据可以简化成一句话：**strict 问中间层拿到 NXDOMAIN 就不回退完整域名**，于是「中间层说没有」被当成了「最终答案」。这在严格符合 [RFC 8020]（*NXDOMAIN: There Really Is Nothing Underneath*）的命名空间里是对的：父名 NXDOMAIN 意味着其下所有名字都不存在。问题在于现实里存在不合规的区。

```yaml
# 实验对照：仅用于受控环境，不要直接上生产
server:
    qname-minimisation: yes
    qname-minimisation-strict: yes
```

**为什么这么设**：只在能承受「部分域解析失败」的实验里开，用来复现下一节的故障；生产环境保持 `strict: no`。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf.d/10-qmin.conf
unbound-control get_option qname-minimisation-strict
dig @127.0.0.1 -p 5353 www.example.org A +short
```

**失败模式 / 回滚点**：`strict: yes` 的失败不是「某个域查不到」，而是「整站资源解析失败」（见 13.3），且伴随负缓存放大。改配置必须 **restart** 并逐层刷缓存，回滚点就是 `strict: no` 的完整配置备份。

## 13.3 真实兼容坑：违反 RFC 8020 的 CNAME 区

这是本章最值得记住的一条实测结论。某大型 CDN 的 CNAME 区违反了 [RFC 8020]：对中间层名字返回 **NXDOMAIN**，但其子名（例如带主机名前缀的 CNAME）**确实存在**。按规范这是矛盾的——父名 NXDOMAIN 意味着下面什么都没有。

QMIN 严格模式下的解析过程会这样走：

```text
① 客户端查 assets.cdn.example.org A
② 解析器按 QMIN 只向中间层问 cdn.example.org（QTYPE=A）
③ 该 CDN 的权威服务器对 cdn.example.org 回 NXDOMAIN
④ strict: yes → 不回退完整 QNAME，直接把 NXDOMAIN 当作最终答案
⑤ 客户端收到 NXDOMAIN；页面 HTML 能开（主域已缓存），但 CSS/JS/图片域名全部解析失败
```

现象很有迷惑性：「网站打开了但样式全丢」「只有静态资源挂」「换个网络就好了」。改回 `qname-minimisation-strict: no` 之后，第 ④ 步会回退成发完整 QNAME，解析恢复。

```yaml
# /etc/unbound/unbound.conf.d/10-qmin.conf —— 修复后的配置
server:
    qname-minimisation: yes
    qname-minimisation-strict: no
```

**为什么这么设**：保留 QMIN 的元数据收益，只放弃「不回退」这一条最激进的约束。这是「隐私 vs 可用性」里最划算的一刀：绝大部分查询仍以最少标签发出，只有遇到非 NOERROR 的那一小部分才回退。

**怎么验证**：

```bash
systemctl restart unbound
dig @127.0.0.1 -p 5353 assets.cdn.example.org A +short
dig @127.0.0.1 -p 5353 cdn.example.org A | grep 'status:'
unbound-control stats_noreset | grep -E 'num.query|num.cache' | head -10
```

**失败模式 / 回滚点**：**改完配置不等于恢复**。原因有两条：

1. **负缓存（negative caching）会放大故障**。第 ③ 步的 NXDOMAIN 会按该区 SOA 的 minimum TTL 被缓存（常见 3600 秒）。即使上游修好了、或我们改了 strict，缓存里那条 NXDOMAIN 照样被返回。
2. **链路上每一层缓存都要刷**。第 7 章的多实例链、第 12 章的 dnsdist / nginx 后端、系统存根解析器缓存、浏览器内部缓存，任何一层留着旧的 NXDOMAIN，客户端就仍然失败。只重启最靠近客户端的那一层，是最常见的「改了没用」。

```bash
# 逐层刷缓存：顺序按「离客户端由近到远」逐台执行，每台都要做
unbound-control -c /etc/unbound/filter.conf flush_zone cdn.example.org
unbound-control -c /etc/unbound/recursive.conf flush_zone cdn.example.org
unbound-control -c /etc/unbound/exit.conf flush_zone cdn.example.org
# unbound-control 关着时只能重启实例；并同步清掉浏览器/系统存根缓存
resolvectl flush-caches 2>/dev/null || true
unbound-control stats_noreset | grep -E 'num.cache|num.query' | head -10
```

**为什么这么设**：`flush_zone` 精确刷掉该区及其下的缓存条目，比 `flush_all` / 整机重启代价小；但它是**每个实例各自的操作**，所以命令按实例配置文件逐个列出。第 7 章已说明链路 A/B/C 各有独立缓存。

**怎么验证**：`dig` 恢复 ANSWER SECTION；抓包确认向中间层重新发出了查询（而不是直接命中缓存返回 NXDOMAIN）。

**失败模式 / 回滚点**：若刷完仍失败，先检查是不是 `strict: yes` 还留在某个实例的 include 片段里（`grep -rn qname-minimisation-strict /etc/unbound/`），再检查上游权威服务器是否真的修好了。回滚：整条链回滚到 `strict: no` 的配置快照，并保留一次逐层刷缓存的记录，便于复盘。

## 13.4 fail-closed：禁止静默明文降级

隐私机制再完整，只要有一条「失败时偷偷走明文」的路径，前面全部作废。这一节把第 5 章的结论收拢成可断言的工程要求。

### 13.4.1 两波都失败就 SERVFAIL

「两波」指加密上游的两轮尝试（含重试）都失败：TLS 握手失败、超时、或对端 SERVFAIL。此时正确行为是给客户端 **SERVFAIL**，让监控与人看见故障；错误行为是回退到明文 53 自行递归，查询流全程裸奔，而且客户端完全无感。

```yaml
# /etc/unbound/unbound.conf.d/20-fail-closed.conf
server:
    # 禁止 SERVFAIL 时逃出本区自行递归 / 走明文
    forward-first: no
    tcp-upstream: yes
    edns-buffer-size: 1232
    max-udp-size: 1232

forward-zone:
    name: "."
    forward-addr: 192.0.2.53@853#dns.example.org
    forward-tls-upstream: yes
    forward-first: no
```

**为什么这么设**：`forward-first: no` 在 `forward-zone` 里再写一次，是为了让这个约束贴着上游定义，避免日后在 `server:` 段被别的文件覆盖；`forward-tls-upstream: yes` 与 `#认证名` 保证加密且认证（第 11 章）；`tcp-upstream: yes` 让出向只用 TCP 承载，配合第 14 章跨境链路的实测结论。

**怎么验证**：

```bash
# 断链演练：临时阻断到上游 853，观察客户端应答与抓包
nft add rule inet filter output tcp dport 853 drop
dig @127.0.0.1 -p 5353 example.org A | grep -E 'status:|SERVFAIL'
tcpdump -ni any 'tcp port 853 or udp port 53 or tcp port 53' -c 20
nft flush chain inet filter output   # 演练结束立刻恢复
```

期望 `status: SERVFAIL`，且抓包里**没有**向任何地址的 53 端口明文查询。

**失败模式 / 回滚点**：演练时 `nft` 规则一定要记得恢复（用 `nft list ruleset > /etc/nftables.conf.bak` 先备份）。若出现「慢一点但还是返回答案」，几乎一定是 `forward-first: yes` 或存在第二条 `forward-addr`。

### 13.4.2 单上游断言

转发器里给 `forward-zone` 配第二个 `forward-addr`，Unbound 是**并发竞速取最快**，不是主备。这会把查询送到既定上游之外，令过滤表、加密策略、日志归属全部失效。单上游不是「冗余不足」，而是「策略唯一」的断言：上游切换交给外部巡检脚本（第 5 章）。

```bash
unbound-control list_forwards
grep -rn 'forward-addr' /etc/unbound/unbound.conf.d/ | grep -v '^.*#'
```

**为什么这么设**：用可执行的断言代替「配置里看一眼」。`list_forwards` 显示运行态实际生效的转发目标，`grep` 检查所有 include 片段里有没有藏着的第二条。

**怎么验证**：`list_forwards` 中每个 zone 只有一条地址；`grep` 输出里 `forward-addr` 行数与 zone 数一致。

**失败模式 / 回滚点**：配置文件被某个包管理器的默认片段追加了一条上游，是最隐蔽的来源。回滚：把上游定义集中到单一文件，并在巡检脚本里加一条「条数不等于 1 就告警」。

### 13.4.3 fallback 定义了但无人引用，也算安全

一个常被忽略的细节：配置里可以存在一段 `forward-zone` 或某个备用池，只要**没有任何规则把查询引到它**，它就不会被使用。第 12 章 dnsdist 的 `newServer({pool="..."})` 定义了池但不写 `PoolAction` 引用，同样不会生效。这类「死配置」在审计时是安全的，但有两个前提：

```bash
# 确认没有规则引用某个备用池 / 备用转发区
dnsdist -c 127.0.0.1:5199 -k '<TOKEN>' -e 'showRules()' 2>/dev/null || true
unbound-control list_forwards
unbound-control list_local_zones | grep -E 'fallback|backup' || true
```

**为什么这么设**：把「定义了」与「被引用」分开验证。审计结论要写成「该 fallback 无引用路径」，而不是「没有 fallback」——后者会被下次配置变更推翻。

**怎么验证**：`showRules()` / `list_forwards` 里看不到指向备用目标的规则；同时用断链演练（13.4.1）确认故障时确实没有流量改道。

**失败模式 / 回滚点**：某天有人加了一条 `PoolAction` 或 `forward-first: yes`，死配置瞬间变成**静默降级通道**。回滚：把这类 fallback 定义放进单独的注释文件并定期复查引用关系；发现被引用就按 13.4.1 重做断链演练。

## 13.5 DDR 压制与浏览器 canary

客户端绕过本地解析器的路径有两条：DDR（Discovery of Designated Resolvers，[RFC 9462]）自动发现加密解析器，以及浏览器自带的 DoH。两条都要在本地解析器上压制，而不是靠封 IP。

```yaml
# /etc/unbound/unbound.conf.d/30-ddr-canary.conf
server:
    local-zone: "_dns.resolver.arpa." always_nodata
    local-zone: "use-application-dns.net." always_nxdomain
```

**为什么这么设**：`_dns.resolver.arpa.` 是 DDR 的特区，客户端通过查询它的 SVCB / HTTPS 记录来发现 DoT / DoH / DoQ 端点。`always_nodata` 返回 NODATA（名字存在但没有所问类型的记录），且**忽略该区下的 local-data**，等于告诉客户端「这里没有可发现的加密解析器」，客户端继续用本地解析器。`always_nxdomain` 对 Firefox 的 canary 域生效：回 NXDOMAIN 让 Firefox 关闭内置 DoH（第 12 章）。两者语义不同，不能互换——`always_nxdomain` 会让「名字不存在」，`always_nodata` 让「名字存在但无此类型」。

**怎么验证**：

```bash
dig @127.0.0.1 -p 5353 _dns.resolver.arpa. SVCB +short
dig @127.0.0.1 -p 5353 _dns.resolver.arpa. SVCB | grep 'status:'
dig @127.0.0.1 -p 5353 use-application-dns.net A | grep 'status:'
unbound-control list_local_zones | grep -E '_dns.resolver|use-application'
```

期望 SVCB 查询 `status: NOERROR`、ANSWER SECTION 为空（NODATA）；canary 域 `status: NXDOMAIN`。

**失败模式 / 回滚点**：如果链路上还有另一层解析器（nft 劫持目标、dnsdist 后端、系统存根），那一层没配这两条 → 客户端照样发现端点或启用内置 DoH。**要逐层验证**。另一个坑是 `always_nodata` 与 `static` 的区别：前者忽略 `local-data`，需要给个别名字留白名单时应改用 `transparent` 或配合具体 `local-data`。回滚：注释掉两条 `local-zone` 并 restart，客户端立即恢复默认发现行为。

### 13.5.1 Chrome 与「按 IP 封 443 是 denylist 不是证明」

Chrome 的内置 DoH 升级依赖**系统解析器是否匹配已知 DoH 提供商列表**。指向私有解析器（如 `192.168.68.1`、`fd00::1`、`192.0.2.1`）时不在列表里，Chrome 不会自行升级，会继续用系统解析器——因此不需要 canary 域，也不需要封域名。

按 IP 封 443 的问题是三重的：会误伤同一 IP 上的正常 Web 服务；端点换地址就失效；最重要的是它**不能证明客户端没有绕过**，只能证明「这几个地址不通」。真正的验收是「能不能测出绕过」：把一台客户端的系统 DNS 临时改成公共解析器，看浏览器是否升级到内置 DoH、本地 canary 是否仍把它压回来，并用抓包确认有没有直连外部 443 的 DoH 流量。

```bash
# 验收要测「绕过」，不是测「封没封住」
cat /etc/resolv.conf
dig @127.0.0.1 -p 5353 whoami.example.org A +short
tcpdump -ni any 'tcp port 443 and not dst net 192.0.2.0/24' -c 20
```

**为什么这么设**：把判据从「denylist 有没有命中」换成「系统解析器是谁、有没有外部 DoH 连接」。前者是配置，后者是行为。

**怎么验证**：`resolv.conf` 指向自建解析器；抓包里没有客户端直连外部 DoH 端点的连接；改系统 DNS 做一次对照演练后恢复。

**失败模式 / 回滚点**：演练后忘记把系统 DNS 改回来，会让这台客户端长期绕过所有本地策略。回滚：演练前后各存一份 `/etc/resolv.conf` 快照，并在脚本里用 `trap` 恢复。

## 13.6 ECS 默认不发

EDNS Client Subnet（ECS，[RFC 7871]）会把客户端地址的一部分随查询送给权威服务器，让 CDN 按「用户所在网段」返回就近节点。代价是：**每发一次 ECS，就等于主动向第三方披露一条客户端网段信息**，与本章的抗元数据目标直接冲突。

Unbound 只有在 `module-config` 里显式加入 `subnetcache` 模块，并且配置了 `send-client-subnet` / `client-subnet-zone` 时才会向权威服务器添加 ECS 选项。默认模块链是 `"validator iterator"`，**默认不发 ECS**。官方手册也建议只在「解析器与客户端属于不同网络」的开放解析器场景启用，因为它会按网段切分缓存、拖累命中率。

```yaml
# /etc/unbound/unbound.conf.d/40-ecs.conf —— 默认（不启用 ECS）
server:
    module-config: "validator iterator"
    # 不写 subnetcache，不写 send-client-subnet / client-subnet-zone
```

**为什么这么设**：把「不发」写成显式的一行 `module-config`，是为了让审计能直接读到结论，而不是靠「默认应该没开吧」。若确实需要 ECS（例如解析器与客户端不同网、且 CDN 调度必须靠它），应把 `max-client-subnet-ipv4` / `max-client-subnet-ipv6` 设成能接受的最粗前缀，并且只把 `send-client-subnet` 列给确需的目标，而不是全局打开 `client-subnet-always-forward: yes`。

**怎么验证**：

```bash
unbound-control get_option module-config
tcpdump -ni any 'udp port 53' -c 30 -vvv 2>/dev/null | grep -i 'CLIENT-SUBNET' || true
dig @127.0.0.1 -p 5353 +subnet=192.0.2.0/24 example.org A | grep -E 'CLIENT-SUBNET|status:'
```

第一条应显示模块链里没有 `subnetcache`；抓包里不应出现 CLIENT-SUBNET 选项。注意即便客户端自己带了 ECS 选项（`dig +subnet=`），未启用 `subnetcache` 时 Unbound 也不会把它转发给未列入 `send-client-subnet` 的权威服务器。

**失败模式 / 回滚点**：把 `module-config` 写成 `"subnetcache validator iterator"` 却忘了配任何 `send-client-subnet`，表面上「开了 ECS 模块」但什么都没发，容易误判成已生效或已泄露。回滚：恢复 `"validator iterator"`，并复查所有 include 片段里有没有 `client-subnet-zone`。

## 13.7 dnstap 是自己的查询流副本

dnstap 是结构化的 DNS 事件日志格式，能记录客户端查询、客户端应答、解析器与转发器的出向/入向报文。它的价值是排障与审计；它的风险是**你自己在自己门口生成了一份完整的查询流副本**。前面所有 QMIN、DDR 压制、ECS 默认不发，都是为了减少元数据外泄；如果 dnstap 全量日志无限期留存，等于把削减掉的元数据又完整存了一份在本地磁盘上，还会进入备份、日志采集系统、SIEM。

```yaml
# /etc/unbound/unbound.conf.d/50-dnstap.conf
# 教学示例：开启时写明保留期（例如 30 天），由外部采集器落盘
dnstap:
    dnstap-enable: yes
    dnstap-socket-path: "/run/dnstap/dnstap.sock"
    dnstap-log-client-query-messages: yes
    dnstap-log-client-response-messages: yes
    dnstap-log-resolver-response-messages: yes
    dnstap-log-forwarder-response-messages: yes
    dnstap-bidirectional: no
```

**为什么这么设**：明确列出记录了哪几类消息，便于审计「到底留了什么」；`dnstap-socket-path` 由外部采集器接收，Unbound 自己不直接写盘，因此**保留期由采集器与 logrotate 落实**，不能只写在注释里。`dnstap-bidirectional: no` 避免重复记录同一条流的两个方向。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf.d/50-dnstap.conf
ls -l /run/dnstap/dnstap.sock
# 保留期是否真的执行（示例：30 天）
find /var/log/dnstap -type f -mtime +30 -print
systemctl cat dnstap-collector 2>/dev/null | grep -i -E 'retention|rotate' || true
```

`find` 在稳定运行 30 天以上的系统上应该为空；若列出文件，说明保留期只是写在文档里。

**失败模式 / 回滚点**：三个高频问题：① 编译 Unbound 时没带 `--enable-dnstap`，配置能过但功能不存在；② 采集器挂了导致 dnstap 线程阻塞或事件丢失，表现为「日志有缺口」而不是「DNS 不通」；③ 只给 nginx access log 设了保留期，dnstap 归档无限增长。回滚：`dnstap-enable: no` 并 restart，先恢复可观测性的最小集，再修采集器。

### 13.7.1 把保留期写成可执行断言

```bash
# 教学示例：采集器侧的清理任务，保留 30 天
find /var/log/dnstap -type f -name '*.dnstap' -mtime +30 -delete
# 审计：确认配置文件里写明了保留期，且任务确实被调度
grep -rn '30' /etc/logrotate.d/dnstap /etc/systemd/system/dnstap-*.service 2>/dev/null || true
systemctl list-timers | grep -i dnstap || true
```

**为什么这么设**：把「保留期」从一句承诺变成一条可 grep、可看定时器、可看文件年龄的事实。第 12 章的 nginx access log 与这里的 dnstap 要用同一套口径，否则会出现「前端日志 30 天、查询流副本无限期」的落差。

**怎么验证**：`systemctl list-timers` 里能看到清理任务；`find -mtime +30` 无输出。

**失败模式 / 回滚点**：`-delete` 类命令写错路径会误删别的日志。回滚：先不带 `-delete` 跑一遍 `-print` 看清单，确认路径后再启用删除；并保留采集器配置的备份。

## 13.8 一张验收清单

| 检查项 | 期望 | 命令 / 依据 |
|---|---|---|
| QMIN | `yes` 且 `strict: no` | `unbound-control get_option ...` |
| 兼容性 | 静态资源域名有 ANSWER | `dig assets.cdn.example.org A` |
| 负缓存 | strict 改动后逐层刷过 | 每台实例 `unbound-control flush_zone` |
| 单上游 | 每 zone 一条 `forward-addr` | `unbound-control list_forwards` |
| fail-closed | 断 853 后 SERVFAIL、抓包无 53 | 断链演练 + `tcpdump` |
| fallback | 定义了但无引用路径 | `showRules()` / `list_forwards` |
| DDR | `_dns.resolver.arpa.` NODATA | `dig ... SVCB` |
| canary | `use-application-dns.net` NXDOMAIN | `dig ... A` |
| 绕过可测 | 改系统 DNS 能观察到行为变化 | 对照演练 |
| ECS | 模块链无 `subnetcache`，抓包无 CLIENT-SUBNET | `get_option` + `tcpdump` |
| dnstap | 记录类型明确、保留期执行 | `find -mtime +30` + 定时器 |

## 本章要点

- 查询名最小化（QMIN）**抗的是元数据聚合，不抗全路径嗅探**，也不是加密；本地根区镜像时根这一跳本来不出口，收益主要落在 TLD 层。`harden-below-nxdomain` 只对 DNSSEC-secure 的 NXDOMAIN 生效，无验证器时是空转。
- `qname-minimisation: yes` 是尽力而为：中间层返回非 NOERROR 就回退完整 QNAME；`strict: yes` **不回退**，官方手册明确警告「很多域将无法解析」。
- 某大型 CDN 的 CNAME 区违反 [RFC 8020]（父名 NXDOMAIN、子名却存在）→ strict 模式问中间层拿到 NXDOMAIN 就收工，整站 CSS/JS 解析失败；改 `strict: no` 后恢复，**且链路上每层缓存都要刷**，否则前一层照样返回缓存的 NXDOMAIN（负缓存按 SOA minimum 缓存，常见 3600 秒）。
- fail-closed 三件套：`forward-first: no` 禁止静默明文降级、两波都失败就 SERVFAIL、`forward-addr` 只留一条（多条是并发竞速取最快而非主备）。**fallback 定义了但无人引用也算安全**，但要用 `showRules()` / `list_forwards` 与断链演练证明无引用路径。
- `local-zone: "_dns.resolver.arpa." always_nodata` 压制 DDR，`"use-application-dns.net." always_nxdomain` 关闭 Firefox 内置 DoH；Chrome 靠系统解析器匹配已知提供商列表，指向私有解析器时不升级。**按 IP 封 443 是 denylist 不是证明**，验收要能测出绕过。
- ECS 默认不发（`module-config` 不含 `subnetcache`）；dnstap 是**自己的查询流副本**，全量日志必须写明记录类型与保留期（例如 30 天）并验证真的执行，否则抗元数据聚合在自己门口破功。

## 参考资料

- RFC 9156，*DNS Query Name Minimisation to Improve Privacy*：https://www.rfc-editor.org/rfc/rfc9156
- RFC 7816，*DNS Query Name Minimisation to Improve Privacy*（前一版）：https://www.rfc-editor.org/rfc/rfc7816
- RFC 8020，*NXDOMAIN: There Really Is Nothing Underneath*：https://www.rfc-editor.org/rfc/rfc8020
- RFC 9462，*Discovery of Designated Resolvers (DDR)*：https://www.rfc-editor.org/rfc/rfc9462
- RFC 7871，*Client Subnet in DNS Queries*：https://www.rfc-editor.org/rfc/rfc7871
- Unbound 官方手册，`unbound.conf(5)` 的 `qname-minimisation`、`qname-minimisation-strict`、`harden-below-nxdomain`、`local-zone` 类型、EDNS Client Subnet 模块与 dnstap 段：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound-control(8)` 的 `list_forwards` / `flush_zone` / `get_option`：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- Mozilla Support，Canary domain `use-application-dns.net`：https://support.mozilla.org/en-US/kb/canary-domain-use-application-dnsnet
- dnstap 官方站点（结构化 DNS 事件日志格式）：https://dnstap.info/
- dnsdist 官方手册，Actions / Selectors / Constants 参考（`PoolAction`、`RCodeAction`、`DNSRCode.NXDOMAIN`）：https://www.dnsdist.org/reference/actions.html
