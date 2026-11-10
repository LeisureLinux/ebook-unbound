# DNS 里的信任数据：邮件、SSH 与自动化

一个区里最不显眼的名字，往往是以下划线开头的那些。它们不指向任何一台主机，却是别人决定是否相信你的依据：这封邮件该不该收、这个 SSH 主机密钥该不该信、这张证书能不能签、邮箱客户端该连哪个端口。这类记录统称**信任数据**（trust records）——不是地址簿，是**公开的声明**。

第 29 章把 DNS 当成攻击面看：记录暴露了什么、悬空记录怎么被接管。这里换一个方向，只看**记录里放了什么、谁在读、读不到或读错了会发生什么**：权威区里那几行字的语义与消费端行为。


## 一张大表：谁在读这些记录

表里「需 DNSSEC 吗」一列问的是**这条记录要产生安全结论，是否必须依赖 DNSSEC 验证**，不是问「能不能顺手签名」。这一点是本章后面所有结论的分水岭。「轮换频率」指正常运维下改动的频次，不是 TTL。「失效后果」指记录写错、缺失或与对端状态不同步时，消费端会发生什么。

| 用途 | 记录类型 | 名字形态 | 谁消费 | 需 DNSSEC 吗 | 轮换频率 | 失效后果 | 相关 RFC |
|---|---|---|---|---|---|---|---|
| 邮件发件授权 | SPF，TXT | `example.org. TXT "v=spf1 mx -all"` | 收件方 MTA | 否 | 低，改发件源时 | 一个域只能有一条 `v=spf1`；两条即永久错误（permerror），全量拒收或降级 | [RFC 7208] |
| 邮件签名公钥 | DKIM，TXT | `sel1._domainkey.example.org. TXT "v=DKIM1; k=rsa; p=<BASE64_PUBLIC_KEY>"` | 收件方 MTA | 否 | 中，按 selector 定期轮换 | 公钥取不到则签名验证失败，进而 DMARC 不通过；`p=` 里放的是**公钥**不是私钥 | [RFC 6376] |
| 邮件认证策略 | DMARC，TXT | `_dmarc.example.org. TXT "v=DMARC1; p=none; rua=mailto:dmarc@example.org"` | 收件方 MTA、报告接收方 | 否 | 低，策略收紧时 | 取不到则按无策略处理；一上来就 `p=reject` 会把合法信源一起拒掉 | 见官方资料 |
| SMTP 强制 TLS | MTA-STS，TXT + HTTPS 策略 | `_mta-sts.example.org. TXT` 与 `https://mta-sts.example.org/.well-known/mta-sts.txt` | 发件方 MTA | 否，靠 HTTPS 与策略缓存 | 中，策略变更要改 `id` | 策略文件 404 或证书不匹配，发送方回退到机会性 TLS，可被降级 | [RFC 8461] |
| TLS 失败报告 | TLS-RPT，TXT | `_smtp._tls.example.org. TXT "v=TLSRPTv1; rua=mailto:tlsrpt@example.org"` | 发件方 MTA | 否 | 低，改报告地址时 | 收不到失败报告，MTA-STS 的故障对己方不可见 | 见官方资料 |
| 邮件证书绑定（DANE） | TLSA | `_25._tcp.mx.example.org. TLSA 3 1 1 <CERT_SHA256_HEX>` | 发件方 MTA | **必须，只在 DNSSEC 签名区生效** | 高，跟每次证书轮换同步 | 证书换了而 TLSA 没同步，对端按 DANE 硬失败**拒收**，不是降级警告 | [RFC 7672] 一类 |
| 邮件主机 | MX | `example.org. MX 10 mx.example.org.` | 发件方 MTA | 否 | 极低，换邮件主机时 | **目标必须是名字，不能是 IP 字面量，也不能是 CNAME**；写错会导致解析不一致或退信 | 见官方资料 |
| 客户端服务定位 | SRV | `_imaps._tcp.example.org. SRV 0 1 993 imap.example.org.` | 邮件客户端自动配置 | 否 | 低，改端口/主机名时 | 自动配置失败，客户端回退到手工填写 | [RFC 6186] / [RFC 8314] 一类 |
| 邮箱客户端自动配置 | A / CNAME + HTTPS XML | `autoconfig.example.org.` CNAME 或 A，HTTPS 上 `/mail/config-v1.1.xml` | Thunderbird 一类客户端 | 否，靠 HTTPS 证书 | 低，改服务器配置时 | 配置取不到则配不出邮箱；证书不覆盖该名字则报证书错误 | 见官方资料（Mozilla 格式） |
| 同族自动发现 | A / CNAME + HTTPS | `autodiscover.example.org.` | Outlook 一类客户端 | 否，靠 HTTPS 证书 | 低 | 同上，回退手工配置；名字不同、机制同族 | 见官方资料 |
| SSH 主机公钥指纹 | SSHFP | `host.example.org. SSHFP <alg> <fptype> <FINGERPRINT_HEX>` | OpenSSH 客户端 | **必须，只有 AD 位为真才摆脱 TOFU** | 中，主机重装/换密钥时 | 指纹不匹配会告警或拒绝连接；AD 为假则仍是首次连接信任（TOFU） | [RFC 4255] 一类 |
| 限定可签发 CA | CAA | `example.org. CAA 0 issue "ca.example.net"` | CA 签发前的检查方 | 否 | 低，换 CA 时 | 写错会让合法 CA 拒签；**没有 CAA 不等于禁止签发** | [RFC 6844] 一类 |
| ACME 自动续期 | DNS-01，TXT | `_acme-challenge.example.org. TXT "example-token-0000"` | CA 验证器、certbot 一类客户端 | 否 | **高，每次续期自动写/删** | 记录未生效或残留导致续期失败、证书过期；写权限泄露等于可被冒签证书 | 见官方资料 |
| 服务绑定 | SVCB / HTTPS | `example.org. HTTPS 1 . alpn="h3,h2"` | 浏览器、客户端 | 可选，DNSSEC 对它是 OPTIONAL | 中，换端点/ALPN 时 | 参数错会连到不存在的端点或回落；不是访问控制机制 | [RFC 9460] |
| 加密解析器发现 | DDR / SVCB | `_dns.resolver.arpa.` | 客户端自动发现加密解析器 | 可选，见官方资料 | 低，解析器部署变更时 | 客户端可能绕过本地解析器；第 13 章讲为何压制它 | [RFC 9462] |
| 域所有权验证 | TXT | `_<vendor>-challenge.example.org. TXT` | 第三方服务验证器 | 否 | 一次性，验证后应删除 | 临时记录长期残留，就是第 29 章说的暴露面 | 见官方资料 |

DKIM 那一行常被口语叫作 **Domain Key**，因此 `sel1._domainkey` 里的 `selector` 就是这套机制的关键：它让「换公钥」变成「换一个左标签」，而不是「改一条所有人都在缓存的记录」。名字里的 `_domainkey` 是固定标签，`sel1` / `sel2` 才是可轮换的部分。

表格里最容易被误读的两行是 TLSA 与 SSHFP：它们把「安全结论」直接挂在 DNS 应答上，所以 DNS 应答本身必须可认证。其余各行要么靠公钥密码学（DKIM、ACME），要么靠独立的 HTTPS 通道（MTA-STS、autoconfig、autodiscover），要么只是配置提示（SPF、DMARC、SRV、CAA、SVCB），DNSSEC 对它们是**增强**而非**前提**。把这两类混成一类，就会出现「我签了名所以 SSH 一定安全」或者「没签名所以 DKIM 无效」这类反向错误。

### 逐条 dig 验证命令

表里每一行都有对应的可复制命令。统一用文档地址段里的假解析器 `192.0.2.53`，域名只用 `example.org` / `example.net`。`+dnssec +multi` 用于要看 AD 位与完整字段的场合，`+short` 用于脚本里做断言。

```bash
# 递归解析器 192.0.2.53，属 RFC 5737 文档段
R="@192.0.2.53"

# SPF：断言「有且只有一条 v=spf1」
dig $R example.org TXT +short | grep -c 'v=spf1'
# DKIM：selector 在名字里，轮换就是换 sel1/sel2
dig $R sel1._domainkey.example.org TXT +short
dig $R sel2._domainkey.example.org TXT +short
# DMARC
dig $R _dmarc.example.org TXT +short
# MTA-STS：TXT 只给「存在性 + id」，策略正文走 HTTPS
dig $R _mta-sts.example.org TXT +short
curl -fsS https://mta-sts.example.org/.well-known/mta-sts.txt
# TLS-RPT
dig $R _smtp._tls.example.org TXT +short
# TLSA：一定要带 +dnssec 看 AD 位，并核对证书哈希
dig $R _25._tcp.mx.example.org TLSA +dnssec +multi
# MX：目标必须是名字；再单独查这个目标的 A，确认不是 CNAME
dig $R example.org MX +multi
dig $R mx.example.org A +short
# SRV
dig $R _imaps._tcp.example.org SRV +short
# Thunderbird autoconfig：DNS 只负责指路，正文在 HTTPS
dig $R autoconfig.example.org A +short
dig $R autoconfig.example.org CNAME +short
curl -fsS https://autoconfig.example.org/mail/config-v1.1.xml
# 同族 autodiscover
dig $R autodiscover.example.org A +short
# SSHFP：AD 位才是信任开关，字段含义见官方资料
dig $R host.example.org SSHFP +dnssec +multi
# CAA
dig $R example.org CAA +short
# ACME DNS-01：续期窗口里应能看到一次性的验证值
dig $R _acme-challenge.example.org TXT +short
# SVCB / HTTPS
dig $R example.org HTTPS +short
dig $R example.org SVCB +short
# DDR：特殊用途名字，通常由本地递归解析器应答
dig $R _dns.resolver.arpa. SVCB +dnssec +multi
# 域所有权验证 TXT：拿全量 TXT 做残留巡检
dig $R example.org TXT +short
dig $R _verification-example.example.org TXT +short
```

读法上有一个坑：`dig` 输出里的 `ad` 标志是**递归验证器**给出的结论，不是权威服务器「说自己是安全的」。用不带验证的解析器（例如纯转发器）去查 SSHFP，即使记录本身正确，也不会有 AD 位。另一个坑是 `+short` 会吞掉 TTL 与 flags，做断言方便，做排障不够；排障一律换回不带 `+short` 的完整输出。至于「刚加完却查不到」，那是下一节的负缓存问题，不是命令写错了。

## 完整配置片段

下面七组片段都是教学用的权威区片段或客户端配置，**不是 Unbound 配置**。每组后面紧跟三件事：为什么这么设、一条可复制的验证命令、失败模式与回滚点。

### 邮件发件授权与策略

```text
; example.org. 区片段；地址均为 RFC 5737 文档段
$TTL 3600
@                  IN  MX    10 mx.example.org.
mx                 IN  A     198.51.100.25
@                  IN  TXT   "v=spf1 mx -all"
sel1._domainkey    IN  TXT   "v=DKIM1; k=rsa; p=<BASE64_PUBLIC_KEY>"
sel2._domainkey    IN  TXT   "v=DKIM1; k=rsa; p=<BASE64_PUBLIC_KEY>"
_dmarc             IN  TXT   "v=DMARC1; p=none; rua=mailto:dmarc@example.org"
_imaps._tcp        IN  SRV   0 1 993 imap.example.org.
_submission._tcp   IN  SRV   0 1 587 smtp.example.org.
```

- **为什么这么设**：MX 目标写名字不写 IP、也不指 CNAME；SPF 全文件只保留一条 `v=spf1`；DKIM 用两个 selector 并存，就是为了能在不中断收信的前提下换公钥；DMARC 先 `p=none` 收报告观察，再逐步收紧；SRV 让客户端免手工填端口。`p=` 里是**公钥**，公开是设计使然。
- **怎么验证**：`dig @192.0.2.53 example.org TXT +short`（数一下 `v=spf1` 是否恰好一条）。
- **失败模式 / 回滚点**：两条 `v=spf1` 会直接 permerror；DKIM 轮换务必按「先加新 selector → 切签名 → 看 DMARC 报告 → 再删旧 selector」的顺序，**旧 selector 的 TXT 就是回滚点**；删早了在途邮件会验签失败。

### MTA-STS 与 TLS-RPT

```text
; example.org. 区片段
_mta-sts     IN  TXT  "v=STSv1; id=chg-0007"
_smtp._tls   IN  TXT  "v=TLSRPTv1; rua=mailto:tlsrpt@example.org"
mta-sts      IN  A    203.0.113.10
```

HTTPS 上 `https://mta-sts.example.org/.well-known/mta-sts.txt` 的策略正文：

```text
version: STSv1
mode: enforce
mx: mx.example.org
max_age: 86400
```

- **为什么这么设**：TXT 只是「策略存在 + 版本号 `id`」的信号，真正的策略正文经 HTTPS 取；因此 `mta-sts.example.org` 的证书必须覆盖这个名字。TLS-RPT 是配套的眼睛，没有它，对端 TLS 握手失败对己方就是静默的。`mode` 先观察再 `enforce`，字段取值见官方资料。
- **怎么验证**：`dig @192.0.2.53 _mta-sts.example.org TXT +short` 与 `curl -fsS https://mta-sts.example.org/.well-known/mta-sts.txt`。
- **失败模式 / 回滚点**：策略文件 404、证书不匹配或 `mx:` 与实际 MX 不符，发送方会回退到机会性 TLS，安全性静默降级；**改策略必须同时改 `id`**，否则发送方按 `max_age` 继续用缓存里的旧策略，改了等于没改。回滚点是保留上一版 `id` 的 TXT 与旧策略文件，直到旧缓存过期。

### TLSA 与 DANE

```text
; 仅当 example.org. 已 DNSSEC 签名、且父区有匹配的 DS 时才有意义
_25._tcp.mx   IN  TLSA  3 1 1 <CERT_SHA256_HEX>
mx            IN  A     198.51.100.25
```

- **为什么这么设**：DANE（DNS-Based Authentication of Named Entities）把证书或公钥的哈希放进 DNS，让发件方不必只依赖公共 CA 体系来判断「这张证书对不对」。三个数字的含义见 [RFC 7672] 一类官方资料。它的可信度**完全来自 DNSSEC**：在**未签名的区里，这条记录没有安全意义**，攻击者改应答即可改哈希。
- **怎么验证**：`dig @192.0.2.53 _25._tcp.mx.example.org TLSA +dnssec +multi`（看 `flags` 里有没有 `ad`）。
- **失败模式 / 回滚点**：证书轮换不同步是这条记录最典型的故障——**不是警告，是拒收**。回滚点是一个「双记录窗口」：先加新证书的 TLSA，再换证书，观察投递成功，最后删旧 TLSA。把 certbot 的自动续期接进来时，这个窗口必须由同一次自动化事务完成，不能靠人记。

### Thunderbird autoconfig 与同族 autodiscover

```text
; example.org. 区片段；CNAME 与 A 二选一，同一名字不能并存
autoconfig      IN  A      203.0.113.10
; autoconfig   IN  CNAME  config-host.example.net.
autodiscover    IN  A      203.0.113.10
```

HTTPS 上 `https://autoconfig.example.org/mail/config-v1.1.xml` 的正文（Mozilla 的 `config-v1.1` 格式，字段以官方资料为准）：

```text
<?xml version="1.0"?>
<clientConfig version="1.1">
  <emailProvider id="example.org">
    <domain>example.org</domain>
    <incomingServer type="imap">
      <hostname>imap.example.org</hostname>
      <port>993</port>
      <socketType>SSL</socketType>
      <username>%EMAILADDRESS%</username>
    </incomingServer>
    <outgoingServer type="smtp">
      <hostname>smtp.example.org</hostname>
      <port>587</port>
      <socketType>STARTTLS</socketType>
      <username>%EMAILADDRESS%</username>
    </outgoingServer>
  </emailProvider>
</clientConfig>
```

- **为什么这么设**：DNS 只负责把 `autoconfig` / `autodiscover` 指到一台能答 HTTPS 的主机，配置内容本身走 HTTPS，证书必须覆盖该名字。这就是常被口语叫作「Thunderbird 自动配置」的那组记录；`autodiscover` 是 Outlook 一类客户端的同族机制，名字不同、机制同族，别当成同一份 XML。
- **怎么验证**：`dig @192.0.2.53 autoconfig.example.org A +short` 与 `curl -fsS https://autoconfig.example.org/mail/config-v1.1.xml`。
- **失败模式 / 回滚点**：若用 CNAME 指向托管配置主机，服务下线而记录没删，就是第 29 章的**悬空 CNAME**；证书不覆盖该名字则客户端直接报证书错误，不会「自动降级到明文」。回滚点是保留旧 A 记录与旧 XML，直到客户端自动配置验证通过；下线服务时**先删 DNS 记录**。

### SSHFP 与 VerifyHostKeyDNS

```text
; example.org. 区片段；用 ssh-keygen -r 生成，不要手抄指纹
host     IN  A      198.51.100.25
host     IN  SSHFP  <alg> <fptype> <FINGERPRINT_HEX>
```

客户端侧：

```conf
# ~/.ssh/config 片段
Host host.example.org
    VerifyHostKeyDNS yes
    StrictHostKeyChecking yes
```

- **为什么这么设**：把「首次连接时人工核对指纹」（TOFU, Trust On First Use）换成「从签名区取已验证的指纹」。`<alg>` 与 `<fptype>` 的取值来自 IANA 注册表，含义见官方资料；用 `ssh-keygen -r host.example.org` 生成，避免手抄出错。**只有递归验证器把 AD 位置 1（DNSSEC 验证通过）时，SSHFP 才会被用来自动接受主机密钥**；AD 为假，客户端仍走 TOFU，`VerifyHostKeyDNS` 并没有替你建立信任。
- **怎么验证**：`dig @192.0.2.53 host.example.org SSHFP +dnssec +multi`（看 `ad`），再用 `ssh -v host.example.org` 看是否报告在 DNS 中找到匹配的主机密钥。
- **失败模式 / 回滚点**：主机重装换密钥而 SSHFP 未更新，客户端会告警甚至拒绝连接；回滚点是换密钥前先并挂新旧两条 SSHFP，确认新密钥生效后再删旧。若本区并未签名，应把 `VerifyHostKeyDNS` 退回 `ask` 并明确记录「这里仍是 TOFU」——转发器上配不了信任锚，「配了 SSHFP」和「客户端真的信了」是两件事。

### CAA 与 ACME DNS-01

```text
; example.org. 区片段；issue 值用 example 域名占位，实际按 CA 官方资料填
@                   IN  CAA  0 issue "ca.example.net"
@                   IN  CAA  0 iodef "mailto:ca-alerts@example.org"
_acme-challenge     IN  TXT  "example-token-0000"
```

续期侧凭据文件（字段名随 DNS 插件而定，见官方资料）：

```text
# /etc/letsencrypt/dns-provider.ini，权限 0600，属主为续期专用账号
dns_provider_api_token = <TOKEN>
```

- **为什么这么设**：CAA 让 CA 在签发前检查「是否允许我签」；`_acme-challenge` 的 TXT 由 certbot 一类客户端在每次续期时**自动写入、验证后删除**。这里的关键判断是：**能写这条记录，约等于能为该域签发证书**。所以 `example-token-0000` 这个一次性验证值公开可读、短期有效，并不机密；真正的高风险凭据是 `dns_provider_api_token = <TOKEN>` 这一行，以及它背后的服务商账号。
- **怎么验证**：`dig @192.0.2.53 example.org CAA +short` 与 `dig @192.0.2.53 _acme-challenge.example.org TXT +short`（续期窗口内）。
- **失败模式 / 回滚点**：记录还没生效或负缓存没过期 → CA 验证失败 → 续期失败 → 证书过期全站不可达；CAA 写错 → 合法 CA 拒签；凭据泄露 → 第三方可能为自己签发该域证书。回滚点有三条：续期前保留上一张证书与旧私钥、保留「禁用该 API 凭据」的开关、以及一份凭据轮换记录。CAA 是**限定**而非认证，不要把它当成防冒签的万能锁。

### SVCB、HTTPS 与域所有权验证 TXT

```text
; example.org. 区片段；参数含义见 RFC 9460
@                        IN  HTTPS  1 . alpn="h3,h2"
@                        IN  SVCB   1 svc.example.org. alpn="h2"
_verification-example    IN  TXT    "example-token-0000"
```

- **为什么这么设**：SVCB / HTTPS（[RFC 9460]）把服务绑定信息——ALPN、端口、替代端点、apex 别名、ECH 一类参数——放进 DNS，客户端据此选端点；DNSSEC 对它是 OPTIONAL。域所有权验证 TXT 是**一次性**的：验证通过就该删，长期留着等于一直挂着一块「这个子域可能被接管」的牌子（第 29 章）。DDR（[RFC 9462]）用的是特殊用途名字 `_dns.resolver.arpa.`，通常由本地递归解析器应答，第 13 章讲为何压制它，这里不重复那段配置。
- **怎么验证**：`dig @192.0.2.53 example.org HTTPS +short` 与 `dig @192.0.2.53 _verification-example.example.org TXT +short`（应当查不到，说明已清理）。
- **失败模式 / 回滚点**：SVCB 参数写错会让客户端连到不存在的端点或回落；验证 TXT 残留是慢性风险而非急性故障。回滚点是变更前把整区记录导出一份快照，逐条核对后删除临时名字。

## 共同结论：四条物理性质

上面十六行记录形态各异，但共享同一组物理约束。把这四条记住，比记住任何单条记录的语法都重要。

### 公开、可读、被缓存，TTL 决定变更速度

权威区里的每一个字节都是**公开可读**的：任何人一条 `dig` 就能拿到，还会被递归解析器按 TTL 缓存、被被动 DNS 与证书透明日志记录、被第三方镜像。因此区文件里绝不能出现私钥、API token 明文、口令、内部主机名拓扑细节。反过来说，DKIM 的 `p=` 放公钥、`_acme-challenge` 放一次性验证值，公开并不违背设计——设计就是让别人来读的。

第二个推论同样重要：**TTL 决定变更生效速度**。一个 3600 秒 TTL 的记录，改完之后在最长一小时内，不同解析器看到的新旧值可能不同。所有「先切再删」「双记录窗口」的操作节奏，都要按 TTL 而不是按秒表来排。变更前把 TTL 调低、等旧 TTL 自然过期、变更、验证、再把 TTL 调回，是这类记录的标准动作。

### 一次性验证值不是秘密，写权限才是

最容易搞反的一处，是把 `_acme-challenge` 的 TXT 值当机密锁起来，却把能写区文件的 API 凭据随手写进 CI 日志或共享脚本里。前者是一次性验证值，公开、短期、用完即删；后者是**区级写权限**，持有者可以改 SPF 的 `include`、改 MX、加一条自己的 DKIM 公钥，或者给自己签一张证书。

所以最小权限的粒度不是「整个区」，而是「**这一个名字**」：`_acme-challenge.example.org.` 的 TXT，或某个自动化只负责的那一个子域。CAA 可以在一定程度上缩小可签发 CA 的范围，但它管的是「谁被允许签」，不管「谁能改 DNS」，两者别互相替代。

### 负缓存让「刚加的记录查不到」成为常态

[RFC 2308] 定义的负缓存（negative caching）会把 NXDOMAIN / NODATA 按 SOA 的 minimum 字段缓存下来，常见取值是 3600 秒。于是自动化里最经典的一幕是：脚本刚写入 `_acme-challenge`，立刻 `dig` 一次，拿回「不存在」，判定续期失败。这不是写失败，是**验证链路里某一层还缓存着之前的『不存在』**。

变更前把相关名字的 TTL 与 SOA minimum 调低，是最省事的预防：

```text
; 变更前：调低 TTL 与 SOA minimum（教学用示例值）
$TTL 300
@   IN  SOA  ns.example.org. hostmaster.example.org. (
        1 7200 3600 1209600 300 )
```

```bash
# 变更前先等旧 TTL 过期，再改；改完复查完整应答而不是只看 +short
dig @192.0.2.53 _acme-challenge.example.org TXT
# 变更后若仍查不到，链路上每一层缓存都要刷，否则前一层照样返回缓存的 NXDOMAIN
```

- **为什么这么设**：把「不存在的记忆」缩短，让新增记录尽快可见；SOA 各字段语义见官方资料。
- **怎么验证**：`dig @192.0.2.53 _acme-challenge.example.org TXT` 看 TTL 与 `flags`。
- **失败模式 / 回滚点**：验证脚本必须重试并带超时，不能「查一次就判失败」；书里第 4 项实测结论里那条「链路上每个实例都要刷」在这里同样成立。回滚点是等负缓存自然过期，或在受控实例上刷缓存（第 7 章讲采集与运行时操作，此处不重复配置）。

### 依赖 DNSSEC 的记录在未签名区没有安全意义

TLSA / DANE、SSHFP 这类记录把安全结论直接挂在 DNS 应答上。应答若只靠 UDP/53 的源端口与事务 ID 保护，改应答就能改指纹或证书哈希，此时那条记录只是「更方便的 TOFU」，不是认证。判断标准只有一个：**递归验证器是否真的验过并把 AD 位置 1**。

「本区已签名」也不等于一劳永逸：签名区里还得管 KSK / ZSK / DS 的轮换，DS 与子区不匹配会让整区变成 bogus，连带把这些信任记录一起打成 SERVFAIL。边界只有一句：**转发器与验证器二选一**，在纯转发器上讨论 SSHFP 的 AD 位没有意义。

## 自动化写记录的凭据与故障域

certbot、DNS 服务商 API、动态 DNS 客户端，本质是同一类东西：**一个能改区内容的程序化凭据**。对它按下面五条收紧，逐条都能对应到前面某个失败模式。

- **最小权限**：只授 `example-token-` 那个一次性名字所属的 `_acme-challenge.example.org.`，或自动化实际负责的那一个子域；不授整区、不授 NS、不授 MX、不授 SOA。服务商若不支持按名字授权，就把这个域单独放一个账号。
- **独立凭据**：续期脚本、DDNS 客户端、人工运维各用各的凭据，不复用、不写进同一个 CI 变量；凭据文件权限 0600、属主为续期专用账号，值写 `<TOKEN>` 而非明文。
- **审计**：每次写/删记录都留「谁、何时、改了哪个名字、旧值是什么」；这些事件应该进第 31 章的栈里做告警，尤其是非续期窗口里出现的 `_acme-challenge` 写入。
- **轮换与吊销**：凭据定期轮换，并预先验证「禁用这个凭据」的路径真的可用；轮换本身也要走一次完整续期演练，别等到证书过期那天才发现新凭据没生效。
- **回滚点**：变更前导出区快照、保留上一张证书与旧私钥、保留旧 selector / 旧 TLSA / 旧 SSHFP 直到新值验证通过。任何「先删后加」的顺序在这类记录上都是错的。

与第 25 章 DDNS 对照：那边更新的是 A / AAAA，这边更新的是 TXT / TLSA / SSHFP，**写权限模型完全一样**——一个能改区内容的凭据就是区级权限，第 25 章讲的更新凭据隔离与事务边界，在这里具体化成「只授一个 `_acme-challenge` 名字」。

与第 28 章故障域隔离对照：证书续期、邮件投递、SSH 首次信任，都绑在**同一份区数据的可写与可解析**上。若权威区、自动化 API 账号、续期主机、以及排障要用的解析器共用一条链路和一个凭据，一次服务商 API 故障或一次区推送失败会同时打掉这几件事，而且修复动作本身还要依赖这套 DNS。把「谁能改记录 / 改了谁会读 / 读不到时谁降级」列成一张依赖表，是第 28 章隔离框架在信任数据上的具体化：**别把回滚用的那条路，和坏掉的那条路放在同一个故障域里**。

## 本章要点

- 信任数据是**公开声明**：区文件里绝不放私钥、API 凭据或内部拓扑；DKIM 公钥与 ACME 一次性验证值公开是设计使然，机密的是**写权限**。
- 「需 DNSSEC 吗」是全章分水岭：SSHFP 与 TLSA/DANE 必须靠 AD 位为真的验证应答，在未签名区里没有安全意义；其余记录靠公钥、HTTPS 或只是配置提示，DNSSEC 只是增强。
- 失效后果要按消费端写：两条 `v=spf1` 是 permerror，MX 指 IP/CNAME 会解析不一致，TLSA 与证书轮换不同步是**拒收**而非警告，MTA-STS 策略取不到是静默降级到机会性 TLS。
- 负缓存（[RFC 2308]）让「刚加的记录查不到」成为常态：变更前调低 TTL 与 SOA minimum、验证脚本要重试、链路上每层缓存都要刷；所有轮换都用「先加新、再切、后删旧」的双记录窗口。
- 自动化写记录的凭据按「最小权限到单个名字、独立凭据、审计、可吊销轮换、有回滚点」五条收紧；`_acme-challenge` 的写权限约等于该域的证书签发权。
- 与第 25 章 DDNS、第 28 章故障域隔离呼应：这些记录把内部系统绑到外部 DNS 上，回滚路径不能和故障路径共用同一个凭据、同一条链路。

## 参考资料

- RFC 7208（SPF）：https://www.rfc-editor.org/rfc/rfc7208
- RFC 6376（DKIM）：https://www.rfc-editor.org/rfc/rfc6376
- RFC 8461（MTA-STS）：https://www.rfc-editor.org/rfc/rfc8461
- RFC 7672（DANE / SMTP TLSA）：https://www.rfc-editor.org/rfc/rfc7672
- RFC 4255（SSHFP）：https://www.rfc-editor.org/rfc/rfc4255
- RFC 9460（SVCB / HTTPS）：https://www.rfc-editor.org/rfc/rfc9460
- RFC 6186（邮件客户端自动配置的 SRV 用法）：https://www.rfc-editor.org/rfc/rfc6186
- certbot 官方站（DNS-01 插件与凭据文件权限见官方资料）：https://certbot.eff.org/

其余 RFC 编号（如 [RFC 2308]、[RFC 9462]、[RFC 6844]、[RFC 8314]、[RFC 5737]）按第四篇统一索引查 RFC Editor；Thunderbird `config-v1.1.xml`、autodiscover、DMARC、TLS-RPT、ACME 与域所有权验证的规范编号、年份与字段取值，本章一律写「见官方资料」，不做断言。
