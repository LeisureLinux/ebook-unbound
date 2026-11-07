# Unbound DNS 服务加固指南

> **Unbound DNS Hardening** — 从递归解析器到加密、私有、IPv6-ready 的 DNS 基础设施。
>
> 中文原创开源电子书 · 对齐 **NIST SP 800-81r3** 威胁模型 · 覆盖 DNSSEC / DoT / DoH / QMIN / IPv6 / Kubernetes DNS 选型。

by [LeisureLinux](https://github.com/LeisureLinux) · 正文 **CC BY-SA 4.0** · 构建脚本 MIT

[![build](https://github.com/LeisureLinux/ebook-unbound/actions/workflows/build.yml/badge.svg)](https://github.com/LeisureLinux/ebook-unbound/actions/workflows/build.yml)
[![license](https://img.shields.io/badge/license-CC%20BY--SA%204.0-blue.svg)](./LICENSE)
[![llms.txt](https://img.shields.io/badge/llms-txt-green.svg)](./llms.txt)

---

## 在线阅读 / 下载

| 格式 | 入口 |
|---|---|
| 🌐 HTML 在线版 | <https://leisurelinux.github.io/ebook-unbound/> |
| 📄 PDF | [Releases](../../releases) 或 [直接下载](https://leisurelinux.github.io/ebook-unbound/unbound-dns-hardening.pdf) |
| 📱 ePub | [Releases](../../releases) 或 [直接下载](https://leisurelinux.github.io/ebook-unbound/unbound-dns-hardening.epub) |

---

## 这本书解决什么

大多数 DNS 教程止步于「填哪个公共 DNS」。这本书回答的是另一组问题：

- 一台解析器凭什么相信拿到的答案？链路上谁看得见你在查什么？
- **DNSSEC（数据完整性）** 与 **DoT/DoH（传输加密）** 到底各自防住了什么？
- 为什么 `qname-minimisation-strict: yes` 会让一整个站点的 CSS/JS 全部解析失败？
- 为什么转发器上配了 `auto-trust-anchor-file`，会间歇性全量 `SERVFAIL`？
- 光猫里配 IPv6 DNS 为什么会把整天的排障带偏？
- Kubernetes 里 CoreDNS 和 Unbound 各管什么？

全书以**原创实战经验**为主。每一处配置后面都跟着三件事：**为什么这么设、怎么验证、坏了怎么退回来**。

### 一条贯穿全书的经验

> **排查 DNS / 网络故障，先看 nftables 规则，再怀疑别的。**
>
> 本书记载的多次「解析器坏了」的事故，根因都在策略网关的 `output-tproxy`：
> 本机解析器进程的出向 53 被自己的 `hijack-dns` 吃掉、WireGuard 自己的
> `udp sport 51820` 回包被链尾 mark 规则送进代理。这两类故障从 DNS 侧
> 看都是「上游不响应」，但从 nft 计数器看一眼就能定位。

---

## 目录

### 第一篇 认识 DNS 与 Unbound

| 章 | 标题 | 关键内容 |
|---|---|---|
| 1 | [Unbound 的历史演进](book/src/01-history.md) | 2004 架构 → NLnet Labs 重写 → 1.0 发布；DNSSEC、libunbound、DoT/DoH、serve-stale、QMIN 的里程碑 |
| 2 | [DNS 基础与主流实现横评](book/src/02-dns-landscape.md) | 命名空间、递归/迭代、缓存与负缓存；BIND / dnsmasq / CoreDNS / NSD / PowerDNS / Knot Resolver / systemd-resolved 对照 |
| 3 | [存根、缓存、转发、递归：四种角色](book/src/03-three-roles.md) | RD/RA/AD/CD 位、谁做迭代、谁缓存、谁验证；多实例串链 |

### 第二篇 按角色配置 Unbound

| 章 | 标题 | 关键内容 |
|---|---|---|
| 4 | [缓存解析器：最小可用配置](book/src/04-caching-resolver.md) | 10 行起步、EDNS 1232、缓存与 serve-stale、完整验证闭环 |
| 5 | [转发器：单上游、加密上游、fail-closed](book/src/05-forwarder.md) | 多 `forward-addr` 是竞速不是主备；转发器上不要配 trust anchor；幂等巡检 |
| 6 | [递归解析器与本地根区镜像](book/src/06-recursive-root-mirror.md) | `auth-zone`、NSD 根镜像、AXFR、按 uid 豁免出向 53 |
| 7 | [多实例、缓存与可观测性](book/src/07-multi-instance-observability.md) | 端口规划、过滤表内存、dnstap 保留期、负缓存与刷缓存 |

### 第三篇 加固

| 章 | 标题 | 关键内容 |
|---|---|---|
| 8 | [威胁模型与 NIST SP 800-81r3 基线](book/src/08-threat-model-nist.md) | 资产、信任边界、攻击者能力；控制项 → 章节映射 |
| 9 | [最小暴露面与访问控制](book/src/09-minimal-exposure.md) | 开放解析器与放大攻击、降权、AppArmor、rebinding 防护 |
| 10 | [DNSSEC 验证：信任锚、放行与拒绝](book/src/10-dnssec.md) | 三态验证、AD 标志、`harden-*` 逐项、验证器行为验收表 |
| 11 | [DoT：端口 853 的服务端与客户端](book/src/11-dot.md) | 证书、SNI、连接复用、完整可复制配置 |
| 12 | [DoH：与 Web 服务共存的加密解析](book/src/12-doh.md) | dnsdist + nginx 前端、token 路径、浏览器 canary 与 iOS 描述文件 |
| 13 | [查询隐私：QMIN、防降级与 DDR 压制](book/src/13-query-privacy.md) | QMIN 边界、违反 RFC 8020 的 CDN 区、fail-closed、denylist 不是证明 |
| 14 | [加密通道选路与 fail-closed 工程](book/src/14-encrypted-transport-routing.md) | nft 劫持与 uid 豁免、跨境 UDP QoS、WG 回包被 tproxy 吃掉 |

### 第四篇 RFC 与协议理论

> 本篇**以理论为主**，不重复贴配置块。

| 章 | 标题 | 关键内容 |
|---|---|---|
| 15 | [DNS 基础 RFC 族](book/src/15-rfc-dns-basics.md) | 1034/1035、2308、6891、8020、8499、8767、5737 |
| 16 | [DNSSEC RFC 族](book/src/16-rfc-dnssec.md) | 4033–4035、5011、6840、7129、8624、9276、根 KSK 轮换 |
| 17 | [加密与隐私 RFC 族](book/src/17-rfc-privacy.md) | 7816→9156、7858、8484、9250、9230、9462、7871、9076 |
| 18 | [IPv6 / RA / RDNSS RFC 族与实现差异](book/src/18-rfc-ipv6-implementations.md) | 4861/4862/8106/4191/4941/6724/4193 与不同实现的处理差异 |

### 第五篇 工程实践

| 章 | 标题 | 关键内容 |
|---|---|---|
| 19 | [IPv6 部署：RA、RDNSS、ULA 与前缀轮换](book/src/19-ipv6-deployment.md) | 「不用碰光猫 DNS」、/64 检测 vs /48 RIO、临时地址寿命、sysctl 三处 |
| 20 | [Kubernetes 中的 DNS 选型](book/src/20-kubernetes-dns.md) | CoreDNS 与 Unbound 的分工、`ndots`、NodeLocal DNSCache、三种拓扑 |
| 21 | [综合实验：一台网关 + 一台云主机](book/src/21-lab-topology.md) | 完整教学拓扑、失败注入、验收命令清单 |
| 22 | [故障案例集](book/src/22-failure-cases.md) | 十个真实坑，统一「现象 → 误判 → 定位 → 根因 → 修复 → 验证」 |
| 23 | [WPAD 与 PAC：自动代理发现的实践](book/src/23-wpad.md) | 1999 过期草案、DHCP 252、DNS devolution、PAC 与 WPAD 分层、安全边界 |
| 24 | [Adhole：DNS 层过滤表的工程实践](book/src/24-adhole.md) | 四类上游格式与转换、`local-zone` 与 `forward-zone` 优先级、原子生成与误伤排查 |
| 25 | [内网 DDNS 与微软 DNS 对照](book/src/25-ddns-msdns.md) | Unbound 不支持 RFC 2136；AD 安全动态更新；三条绕行路径 |
| 26 | [企业级大规模部署：anycast 与集群](book/src/26-anycast-cluster.md) | BGP 任播、Cookie 密钥滚动、缓存不共享、NIST 部署基线 |
| 27 | [单播 DNS 与多播 DNS](book/src/27-mdns-dnssd.md) | mDNS / DNS-SD / Discovery Proxy / SRP，`.local` 与 `home.arpa.` 边界 |
| 28 | [DNS 故障域隔离](book/src/28-fault-domain-isolation.md) | 2021 全球中断复盘：官方确认/外部观测/本书推论分列，六层隔离 |

---

## 核心配置速览

本书第三篇给出的加固基线（节选，**完整版与逐条解释见第 9–13 章**）：

```yaml
server:
    # 只绑必要地址，默认拒绝
    interface: 192.0.2.1@53
    access-control: 192.0.2.0/24 allow
    hide-identity: yes
    hide-version: yes

    # DNS Flag Day 2020：1472 会在 MTU 1280 / PPPoE 1492 上分片
    edns-buffer-size: 1232
    max-udp-size: 1232

    # 完整性：真验证，不做 permissive 放行
    auto-trust-anchor-file: "/var/lib/unbound/root.key"
    val-permissive-mode: no
    harden-dnssec-stripped: yes
    harden-below-nxdomain: yes
    harden-glue: yes

    # 隐私：少说话，但别用 strict（会撞违反 RFC 8020 的 CDN 区）
    qname-minimisation: yes
    qname-minimisation-strict: no

    # 韧性：上游抖动时回过期缓存，而不是 SERVFAIL
    serve-expired: yes
    serve-expired-ttl: 3600
    serve-expired-client-timeout: 1800   # 毫秒，不是秒

    # 压制自动发现，防止客户端绕过本地解析器
    local-zone: "_dns.resolver.arpa." always_nodata
```

> ⚠️ 这份速览是**教学片段**，不要整段照抄到生产环境：转发器与验证器不能同时配
> （第 5 章）、`auto-trust-anchor-file` 需要初始化（第 10 章）、接口与 ACL 要改成你自己的网段。

---

## 合规框架映射

| 框架 | 本书对应 |
|---|---|
| **NIST SP 800-81r3** *Secure DNS Deployment Guide* | 第 8 章威胁模型与全书控制项映射 |
| **NIST SP 800-53** SC-8 / SC-20 / SC-21 / AU-2 / SI-4 | 第 11–14 章加密传输、第 7 章可观测性 |
| **RFC 9156**（QMIN）、**RFC 8767**（serve-stale） | 第 13、15、17 章 |
| **RFC 4033–4035**（DNSSECbis）、**RFC 5011**（锚滚动） | 第 10、16 章 |

本书不复述条文编号，落点始终是可运行配置与可观测验证。

---

## 配套工具

```bash
# 检查一份 unbound.conf 是否违反本书的加固基线
scripts/unbound-audit.sh /etc/unbound/unbound.conf
```

覆盖：单上游断言、`auto-trust-anchor` 与 `forward-zone` 互斥、EDNS 与 MTU、
`val-permissive-mode`、QMIN strict、`harden-glue`、serve-expired 单位、ACL 不是 `0/0 allow`、
dnstap 保留期提示。

---

## 本地构建

依赖：`pandoc`、`texlive-xetex` + CJK 字体、`librsvg2-bin`、`python3`。

```bash
git clone https://github.com/LeisureLinux/ebook-unbound
cd ebook-unbound
make        # 或 ./scripts/build-local.sh
```

构建出 `dist/unbound-dns-hardening.{pdf,epub,html}`。GitHub Actions 在 `main` 推送
与 `v*` tag 时自动构建、发布 Pages，并在 tag 上把 PDF/ePub 附到 Release。

---

## 参考资料（写作时的核对依据，不随书发布）

- [Unbound 官方文档](https://unbound.docs.nlnetlabs.nl/en/latest/)（NLnet Labs, CC BY 3.0；提供 PDF / EPUB 下载）
- [NIST SP 800-81r3](https://doi.org/10.6028/NIST.SP.800-81r3) *Secure Domain Name System (DNS) Deployment Guide*
- [RFC Editor](https://www.rfc-editor.org/)
- Jan-Piet Mens, *Alternative DNS Servers: Choice and Deployment, and Optional SQL/LDAP Back-Ends*, UIT Cambridge, 2008/2009（作者 2010 年放出无 DRM PDF）—— 史料与横评参考，本书不节录其正文

---

## 许可

正文 **CC BY-SA 4.0**，构建脚本 MIT。详见 [LICENSE](./LICENSE)。
所有示例地址取自 RFC 5737 / RFC 3849 文档段，域名用 `example.net` / `example.org`，
不指向任何真实服务。
