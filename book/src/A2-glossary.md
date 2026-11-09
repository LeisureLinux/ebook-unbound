# 附录 A2 缩略语

> 按字母顺序列出本书出现的缩略语。同一缩写在 DNS 语境下可能有歧义（例如 `AD` 既是 DNS 标志位也是 Active Directory），表中按本书用法给出。

| 缩写 | 全称 | 中文 / 说明 |
|---|---|---|
| A / AAAA | Address / Address (IPv6) | 地址记录，IPv4 / IPv6 |
| ACL | Access Control List | 访问控制列表；Unbound 的 `access-control`、AD 集成区的 ACL |
| ACME | Automatic Certificate Management Environment | 自动证书管理环境；DNS-01 挑战用 `_acme-challenge` TXT |
| AD | Authenticated Data | DNS 报文标志位：验证器确认答案通过 DNSSEC 验证 |
| AD | Active Directory | 微软目录服务（与上一行同缩写，语境区分） |
| ADNS | Authoritative DNS | 权威 DNS（非正式缩写，本书只作行文简写） |
| ALPN | Application-Layer Protocol Negotiation | TLS 应用层协议协商；HTTPS/SVCB 记录里的 `alpn` 参数 |
| anycast | — | 任播：多个节点宣告同一服务地址 |
| AXFR | Authoritative Zone Transfer | 全量区域传送 |
| BGP | Border Gateway Protocol | 边界网关协议；anycast 与权威路由宣告的基础 |
| BIND | Berkeley Internet Name Domain | 最广泛部署的 DNS 实现 |
| CAA | Certification Authority Authorization | 限定可为域名签发证书的 CA |
| CD | Checking Disabled | DNS 报文标志位：请求验证器跳过验证 |
| CLAT | Customer-side Translator | 464XLAT 的客户端侧翻译，[RFC 8683] |
| CNAME | Canonical Name | 别名记录；悬空 CNAME 是子域接管的常见入口 |
| CoreDNS | — | Kubernetes 默认的插件化 DNS 服务器 |
| Corefile | — | CoreDNS 的配置文件 |
| CSRC | Computer Security Resource Center | NIST 下属，SP 800 系列发布方 |
| CT | Certificate Transparency | 证书透明日志；资产排查时的子域来源 |
| DANE | DNS-based Authentication of Named Entities | 用 DNSSEC 保护的 TLSA 记录绑定证书 |
| DDNS | Dynamic DNS | 动态域名；常指 [RFC 2136] UPDATE 或公网前缀更新脚本（两者不同） |
| DDR | Discovery of Designated Resolvers | [RFC 9462]，`_dns.resolver.arpa` 自动发现加密解析器 |
| delv | DNSSEC validation tool (BIND) | 从验证器视角看应答为何通过/为何 bogus |
| DHCID | DHCP Client Identifier | DHCP 代注册时防止名字被冒用的记录 |
| DHCP | Dynamic Host Configuration Protocol | 动态主机配置协议；option 252 与 WPAD 相关 |
| DHCPv6 | Dynamic Host Configuration Protocol for IPv6 | 下发 option 23/24/56 等，[RFC 3646] / [RFC 5908] 一类 |
| DKIM | DomainKeys Identified Mail | 邮件签名；公钥发布在 `selector._domainkey` TXT |
| DMARC | Domain-based Message Authentication, Reporting and Conformance | 基于 SPF/DKIM 对齐的邮件策略 |
| DNAME | Delegation Name | 整个子树的别名重定向 |
| DNS-SD | DNS-Based Service Discovery | 用 PTR/SRV/TXT 做服务发现，[RFC 6763] |
| DNS64 | DNS64 | [RFC 6147]，从 A 记录合成 AAAA 供 NAT64 使用 |
| dnstap | — | DNS 查询/应答的结构化日志协议 |
| dnstracer | — | 逐级追踪委派链；名字是 dnstracer，不是 dnstrace |
| DoH | DNS over HTTPS | [RFC 8484]，DNS 报文走 HTTPS |
| DoQ | DNS over Dedicated QUIC Connections | [RFC 9250] |
| DoT | DNS over TLS | [RFC 7858]，端口 853 |
| drill | DNS lookup tool (ldns) | NLnet Labs 的 ldns 工具，可追 DNSSEC 链 |
| DS | Delegation Signer | 父区里对子区 KSK 的哈希，DNSSEC 信任链的一环 |
| eBPF | extended Berkeley Packet Filter | 内核可编程观测；不经应用改代码即可看 UDP/53 与丢包 |
| ECH | Encrypted Client Hello | TLS ClientHello 加密；可由 HTTPS 记录发布公钥 |
| ECS | EDNS Client Subnet | [RFC 7871]，把客户端网段带给权威，隐私代价明显 |
| EDNS(0) | Extension Mechanisms for DNS | [RFC 6891]；`edns-buffer-size`、DO 位、Cookie 都在这里 |
| fake-ip | — | 代理客户端用保留段占位地址延迟「域名→IP」映射（198.18.0.0/15 为常见示例段） |
| gai.conf | getaddrinfo configuration | 影响 [RFC 6724] 下的地址排序与 getaddrinfo 返回顺序 |
| getaddrinfo | — | libc 名字解析接口；返回列表是**策略排序**，不是可用性排序 |
| GSS-TSIG | Generic Security Service TSIG | [RFC 3645]，微软安全动态更新用 Kerberos 协商 |
| HINFO | Host Information | 主机信息记录，现代基本弃用 |
| HTTPS RR | HTTPS Service Binding | [RFC 9460] 的 HTTPS 专用 SVCB 形式 |
| IANA | Internet Assigned Numbers Authority | 号码与协议参数分配机构 |
| IETF | Internet Engineering Task Force | RFC 与 Internet-Draft 的标准化组织 |
| IXFR | Incremental Zone Transfer | 增量区域传送 |
| KSK | Key Signing Key | DNSSEC 区签名密钥中的密钥签名密钥 |
| LLQ | Long-Lived Queries | DNS-SD 的长轮询更新机制 |
| mDNS | multicast DNS | [RFC 6762]，链路本地多播名字解析 |
| MTA-STS | MTA Strict Transport Security | 强制 SMTP 走校验过的 TLS |
| MX | Mail Exchanger | 邮件交换器记录 |
| NAT64 | Network Address Translation IPv6→IPv4 | [RFC 6146]，与 DNS64 配合 |
| NIST | National Institute of Standards and Technology | 美国 NIST，SP 800-81r3 发布方 |
| NSEC / NSEC3 | Next Secure (v3) | DNSSEC 的"否认存在"证明；NSEC3 用哈希名 |
| NSEC walking | — | 借 DNSSEC 否认存在记录枚举签名区名字 |
| NSS | Name Service Switch | `/etc/nsswitch.conf` 决定 getent 先读 hosts 还是 DNS |
| NXDOMAIN | Non-Existent Domain | 名字不存在的应答码 |
| ODoH | Oblivious DoH | [RFC 9230]，relay 与 target 分离的两跳加密解析 |
| OOB | Out-of-Band | 带外管理通道 |
| PAC | Proxy Auto-Config | `FindProxyForURL()` 脚本，WPAD 找到的目标 |
| PVD | Provisioning Domain | 网络侧下发配置域的概念 |
| QMIN | QNAME Minimisation | 查询名最小化，[RFC 7816] → [RFC 9156] |
| QNAME / QTYPE / QCLASS | Query Name / Type / Class | DNS 问题段的三个字段 |
| RA | Recursion Available | DNS 报文标志位：服务器支持递归 |
| RCODE | Response Code | DNS 应答码（NOERROR / NXDOMAIN / SERVFAIL 等） |
| RD | Recursion Desired | DNS 报文标志位：客户端请求递归 |
| RDNSS / DNSSL | Recursive DNS Server / DNS Search List | [RFC 8106] 的 RA 选项，与 DHCPv6 23/24 并行 |
| resolvdns | — | 一类 SSH/代理客户端的 DNS 归属开关（yes 交给对端 / no 本地解析）；OpenSSH 无同名选项 |
| resolvectl | — | systemd-resolved 的查询/状态命令（未装时读 /etc/resolv.conf） |
| RPZ | Response Policy Zone | 基于策略改写/阻断应答 |
| RPZ | Response Policy Zone | 策略改写/阻断应答，蜜罐 sinkhole 常用 |
| RRL | Response Rate Limiting | 权威侧应答限速，抑制放大与扫描 |
| RRSIG | Resource Record Signature | DNSSEC 签名记录 |
| sinkhole | — | 把恶意/诱饵域导向受控解析器以记录查询 |
| SLP | Service Location Protocol | [RFC 2608]，WPAD 草案里的发现机制之一 |
| socks5h | SOCKS5 with remote hostname | curl 中把主机名交给代理解析；`socks5://` 是本地解析 |
| SPF | Sender Policy Framework | 邮件发件方授权，TXT 记录 |
| SRP | Service Registration Protocol | [RFC 9665]，纯单播的 DNS-SD 注册 |
| SRV | Service Location | `_service._proto` 形式的服务定位记录 |
| SSHFP | SSH Host Key Fingerprint | [RFC 4255] 一类，把 SSH 公钥指纹放 DNS（需 DNSSEC） |
| SVCB | Service Binding | [RFC 9460] 通用服务绑定记录 |
| TC | TrunCation | DNS 报文标志位；mDNS 查询里语义变为 Known-Answer 续包 |
| TKEY | Transaction Key | 建立 TSIG 安全上下文的记录类型 |
| TLS-RPT | SMTP TLS Reporting | SMTP TLS 失败报告 |
| TLSA | TLS Association | DANE 的证书/公钥绑定记录 |
| TOFU | Trust On First Use | 首次使用时信任（SSH 默认行为；SSHFP+DNSSEC 才摆脱它） |
| TSIG | Transaction Signature | DNS 消息级 HMAC 认证，用于 AXFR / UPDATE |
| TTL | Time To Live | 缓存存活时间 |
| TXT | Text | 任意文本记录，承载 SPF/DKIM/DMARC/ACME 等 |
| UDP / TCP | User Datagram / Transmission Control Protocol | DNS 的两种传输 |
| ULA | Unique Local Address | [RFC 4193] 的 IPv6 私有点对点地址段 |
| W-Bad | WPAD Name Collision / Bad WPAD | 学界对 WPAD 名称冲突攻击的称呼 |
| WPAD | Web Proxy Auto-Discovery Protocol | 1999 年过期的 IETF 草案，未成为 RFC |
| ZSK | Zone Signing Key | DNSSEC 区签名密钥中签数据的那一把 |
| 464XLAT | — | CLAT + PLAT 组合，让纯 v6 接入网里的 v4-only 应用可达 |

> 本表不收录只出现一次且不构成术语的产品名（如某具体云厂商、工具名）。工具名在正文首次出现时给出全称或用途说明。
