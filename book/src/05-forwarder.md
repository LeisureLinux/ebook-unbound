# 转发器：单上游、加密上游、fail-closed

缓存解析器（第 4 章）自己走完整递归；转发器（forwarder）不递归，它把查询整个交给另一台递归解析器，自己只做缓存、过滤与访问控制。这个角色在家宽网关、Kubernetes 节点级缓存、以及"出口只有一条加密通道"的场景里非常常见，也是最容易配错的一种角色。

## 最小转发器：唯一上游

```yaml
# /etc/unbound/unbound.conf.d/10-forward.conf
server:
  interface: 127.0.0.1@5357
  interface: 192.0.2.1@5357
  access-control: 127.0.0.0/8 allow
  access-control: 192.0.2.0/24 allow
  do-ip4: yes
  do-ip6: no
  do-udp: yes
  do-tcp: yes
  username: unbound
  chroot: ""
  hide-identity: yes
  hide-version: yes
  edns-buffer-size: 1232
  max-udp-size: 1232

forward-zone:
  name: "."
  forward-addr: 192.0.2.53
  forward-first: no
```

**为什么这么设**：`forward-zone` 的 `name: "."` 表示"所有查询都交给这个区"，官方手册明确写：`name: "."` 加一条 `forward-addr` 会把所有查询（除可从缓存直接回答的）转发给那台服务器。文档同时强调，列表里的 `forward-host` / `forward-addr` 指向的必须是**能继续递归的递归解析器**，不是权威服务器；Unbound 在这个区里不做递归，只逐个追 CNAME 链。

`forward-first: no`（默认 `no`）是 fail-closed 的第一道闸：它表示转发查询遇到 SERVFAIL 时**不要**回退到"较不具体的区"或普通递归解析。官方手册对 `yes` 的警告是"任何解析失败都会允许逃出这个区"——一旦逃出去，查询就会自己向互联网递归，绕开既定上游，也绕开上游那侧的过滤与审计策略。

`do-ip6: no` 是因为这台实验网关没有可用的 IPv6 出向；按第 4 章的说明，它只关 IPv6 传输，不影响应答里照常返回 AAAA 记录内容。端口 `@5357` 是给第 7 章多实例规划预留的语义，不与真实主机名或公网 IP 同段出现。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf
dig @127.0.0.1 -p 5357 example.org A +short
unbound-control -c /etc/unbound/unbound.conf list_forwards
```

`list_forwards` 应只列出一条 `forward-addr`，这是"单上游"最可靠的核对方式——比翻配置文件可靠，因为 include 拼接后的结果才是生效值。

**失败模式 / 回滚点**：若 `dig` 返回 SERVFAIL 而不是超时，先查上游是否真的会递归（拿 `dig @192.0.2.53 example.org A` 直连测一次）。回滚：删掉 `forward-zone` 段并 restart，实例退回第 4 章的普通缓存解析器，缓存会清空但无持久状态损坏。

## 多个 forward-addr 是并发竞速，不是主备

这是本章最要紧的一条实测结论，也是"唯一上游"要求的由来。

```yaml
# 反面示例：不要这样写
forward-zone:
  name: "."
  forward-addr: 192.0.2.53
  forward-addr: 198.51.100.53
  forward-first: no
```

**为什么不能这么设**：很多人把第二条 `forward-addr` 理解成"备胎"，以为第一条挂了才用第二条。Unbound 不是这个语义。多条 `forward-addr` 构成的是一组候选上级，解析器会在它们之间做服务器选择 / 并发探测，谁先答、谁答得快就用谁——也就是**并发竞速取最快**，不是主备切换。官方手册对 `auth-zone` 的 `primary` 用的是"都试一遍"（tried if one fails），对 `forward-addr` 则没有"主 / 备"或优先级的表述；实际行为是流量会分摊到所有候选。

后果不是"多一份冗余"，而是**过滤表失效**：假设 `192.0.2.53` 是你精心配置、带过滤与审计的上游，`198.51.100.53` 只是顺手加的公共解析器，那么一部分查询会稳定地走后者。客户端看到的过滤结果变成随机——同一个域名，这次被挡、下次放行。更糟的是这种故障很难归因：日志里两条上游都没报错，`forward-first: no` 也确实拦住了递归回退，问题只是"查询根本没送到该去的地方"。

静默明文降级是同一类问题的另一面：若允许上游超时后退回明文 53 或退回自身递归，整条查询流就在无人察觉的情况下裸奔。所以这里的策略是：**转发器只留一条 `forward-addr`，上游的可用性与切换交给外部巡检脚本**（5.6 节），fail-closed 由 `forward-first: no` 与 `forward-tls-upstream` 共同保证，两波都失败就明确回 SERVFAIL，让故障可见。

**怎么验证**：

```bash
unbound-control list_forwards | grep forward-addr | wc -l
tcpdump -ni any 'udp port 53 and host 198.51.100.53' -c 5
```

第一条应为 1；第二条在持续查询时不应抓到任何发往"备用"地址的报文。若抓到了，说明某处 include 又追加了一条上游。

**失败模式 / 回滚点**：已经上了双上游的生产实例，修复就是删掉多余行并 restart（缓存清空是预期代价）。不要试图用"给第二条设个更差的超时"来模拟主备——语义不支持。`forward-first: yes` 不是补救，它会打开另一条逃生通道。

## TCP 承载的上游：forward-tcp-upstream

```yaml
# /etc/unbound/unbound.conf.d/20-forward-tcp.conf
forward-zone:
  name: "."
  forward-addr: 192.0.2.53
  forward-tcp-upstream: yes
  forward-first: no
```

**为什么这么设**：`forward-tcp-upstream: yes`（默认 `no`）让这个区的上游查询**只用 TCP**，不管全局 `tcp-upstream` 是什么。它的直接收益是避开 UDP 分片与放大限制：第 4 章讲过 1232 是出向 EDNS 通告值，但某些链路对 UDP 大包或跨境 UDP 有 QoS，TCP 承载更稳。实测形态是同一条拥挤链路上 UDP/QUIC 下载 1MB 只到约 300 B/s，而 TCP 承载的通道能到约 660 KB/s；DNS 查询虽然小，超时重试的行为完全同源。

代价是每条上游查询都要建连（Unbound 会复用连接，但仍有握手与状态开销），延迟通常高于 UDP。所以它是"链路质量差时的选择"，不是默认值。注意它只管**上游方向**；下游客户端仍可用 UDP 问本机，由 `do-udp` 决定。

**怎么验证**：

```bash
dig @127.0.0.1 -p 5357 example.org A +short
tcpdump -ni any 'tcp port 53 and host 192.0.2.53' -c 5
```

应只看到 TCP/53 出向报文，没有 UDP/53 到该上游。

**失败模式 / 回滚点**：上游若只监听 UDP（少见但存在），会全量 SERVFAIL。回滚：改为 `no` 或删掉该行并 reload（`forward-zone` 选项可 reload 生效，不必改 `interface`）。若 `unbound-control` 未开启，改完记得 `systemctl restart` 才保险。

## 加密上游：forward-tls-upstream 与 tls-cert-bundle

`forward-tcp-upstream` 解决"用 TCP 承载"，但不加密。**加密传输（DoT / DoH / DoQ）与 DNSSEC 是两件事**：前者提供传输加密 / 隐私，后者提供数据完整性 / 来源认证，任何配置都不得用一个替代另一个。本节的 DoT 只是上游方向的客户端用法；服务端侧 DoT（853 端口监听）在第三篇第 11 章，DoH 在第 12 章，协议族对比在第四篇第 17 章。

```yaml
# /etc/unbound/unbound.conf.d/30-forward-dot.conf
server:
  tls-cert-bundle: /etc/ssl/certs/ca-certificates.crt
  edns-buffer-size: 1232
  max-udp-size: 1232

forward-zone:
  name: "."
  forward-addr: 192.0.2.53@853#dns.example.org
  forward-tls-upstream: yes
  forward-first: no
```

**为什么这么设**：`forward-tls-upstream: yes`（默认 `no`，旧别名 `forward-ssl-upstream`）让上游查询走 TLS。官方手册的硬性要求：**启用它就必须配 `tls-cert-bundle`（或 `tls-win-cert`），否则连接无法被认证**。`tls-cert-bundle` 指向 CA 证书包，官方手册说明它在启动阶段、降权与 chroot 之前读取——所以第 4 章的 `chroot` 与 `username` 组合不会让它读不到，但若改成自己写的 chroot 路径，仍要确认这个文件在启动时的可见性。

`forward-addr` 里的写法有三段，顺序固定：`IP@端口#认证名`。`@853` 指定非默认端口，`#dns.example.org` 指定 TLS 证书要匹配的名字。官方手册明确：只有 `@` 时用默认 53 端口，只有 `#` 时用 `tls-port` 配置的端口；`@` 必须在 `#` 前面。**如果省略 `#` 和认证名，任何名字都被接受**——这等于把证书校验退化成"有证书就行"，中间人拿一张任意可信 CA 签的证书即可顶替上游。所以认证名不是可选项。

`forward-first: no` 在这里是加密链路的 fail-closed 关键：TLS 握手失败或上游 SERVFAIL 时，绝不退回明文 53 或自行递归，直接 SERVFAIL。少了这一行，上游一抖动，查询流就静默明文降级，而客户端完全无感。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf
dig @127.0.0.1 -p 5357 example.org A +short
tcpdump -ni any 'tcp port 853 and host 192.0.2.53' -c 5
openssl s_client -connect 192.0.2.53:853 -servername dns.example.org -CAfile /etc/ssl/certs/ca-certificates.crt </dev/null 2>&1 | grep -E 'Verify return code|subject='
```

`openssl s_client` 的 `Verify return code: 0 (ok)` 证明 CA 包与认证名配置一致；`tcpdump` 应看到 853/TCP 而不是 53/UDP。高 verbosity 下 Unbound 会把对端 TLS 证书打进日志，可用于核对指纹。

**失败模式 / 回滚点**：`tls-cert-bundle` 路径写错或 `chroot` 后不可见 → 启动失败或全量 SERVFAIL；证书名不匹配 → 握手失败，日志出现 TLS 认证错误。回滚：保留 `forward-tls-upstream: yes` 但先不要把实例切到生产，用 `forward-addr: 192.0.2.53@53` 的明文配置做对照实验，验证完再切回。任何情况下都不要靠删掉 `forward-first: no` 来"让它通"。

## do-not-query-localhost: no 的适用条件

```yaml
# /etc/unbound/unbound.conf.d/40-local-upstream.conf
server:
  do-not-query-localhost: no

forward-zone:
  name: "."
  forward-addr: 127.0.0.1@5358
  forward-first: no
```

**为什么这么设**：`do-not-query-localhost` 默认 `yes`，作用是把 `::1` 与 `127.0.0.1/8` 加进 `do-not-query-address` 列表，即"绝不向本机回环地址发查询"。这是防环的默认保护：递归解析器若把根 / 上级指向自己，会形成自问自答的死循环。

只有在**确实要把本机另一个实例当上游**时才改成 `no`。典型场景就是第 7 章的多实例链：一个实例专做过滤 / 缓存（`@5357`），另一个实例专做加密出口或本地根区镜像（`@5358`），前者把 `127.0.0.1@5358` 作为唯一 `forward-addr`。此时回环就是设计中的下一跳，不是环路。

**严格适用条件**，三条都要满足：

1. 上游是**另一台实例 / 另一个端口**，且它自己有独立出口（递归、加密上游或本地根区），否则整条链没有终点。
2. 两个实例的端口、pidfile、控制接口、缓存目录互不重叠（第 7 章）。
3. 上游实例不会再把查询转回下游实例；`forward-first: no` 能保证失败时不逃逸，但不能替你检查拓扑成环。

`do-not-query-localhost: no` 放开的是回环，不是安全边界。若 `forward-addr` 写的是 `192.0.2.1` 这类本机非回环地址，改这个选项毫无作用，要用 `do-not-query-address` 相关配置去排查。

**怎么验证**：

```bash
dig @127.0.0.1 -p 5357 example.org A +short
ss -lunp | awk '$5 ~ /:5358$/ {print}'
```

两条都要成立：下游能答、且 5358 确实是另一个进程在听。

**失败模式 / 回滚点**：若只有下游实例在跑，全量 SERVFAIL 且日志有 "no query to localhost" 一类提示。回滚：恢复默认 `yes`，把 `forward-addr` 指向真正可达的非回环上游。这是本章最容易在"改了配置文件但没重启第二个实例"时踩到的坑。

## 转发器上不要配 auto-trust-anchor-file

```yaml
# 反面示例：转发器上不要这样写
server:
  auto-trust-anchor-file: /var/lib/unbound/root.key
forward-zone:
  name: "."
  forward-addr: 192.0.2.53
  forward-first: no
```

**为什么不能这么设**：信任锚（trust anchor）不是一段静态配置，Unbound 需要**自己向根发起 priming、取回根区 DNSKEY** 并按 [RFC 5011] 做滚动维护。而 `forward-zone: name "."` 把包括根 DNSKEY 在内的所有查询都交给了上游转发器：本机解析器根本没有机会与根权威服务器直接对话，priming 永远无法完成。

实测形态是日志里持续出现 `failed to prime trust anchor`，并伴随**间歇性全量 SERVFAIL**。之所以"间歇性"而不是"全挂"，是因为部分答案能从缓存或上游直接给出，一旦某个查询路径需要验证器介入，就会撞上没建立好的信任链。这个故障特别容易被误判成上游不稳定，于是运维去调上游超时、加第二条 `forward-addr`——正好又踩中 5.2 节的竞速坑，问题越修越散。

**结论是角色二选一**：转发器与验证器（validator）不要在同一实例上混。要么做纯转发器（本章），信任锚与 DNSSEC 验证交给上游那台递归解析器；要么撤掉 `forward-zone`、让本机自己递归并配好信任锚（第三篇第 10 章）。第 6 章的本地根区镜像是第三种形态，但它仍需要本机有真实出向。

顺带区分三个概念：无信任锚 = 透传不验证（坏签名域也返回答案）；`val-permissive-mode: yes` = 记录日志但放行；`val-permissive-mode: no` = 真验证（坏签名回 SERVFAIL，好签名带 AD 标志）。这些是验证器的状态，不是转发器的选项。

**怎么验证**：

```bash
journalctl -u unbound --since '10 min ago' | grep -c 'failed to prime trust anchor'
unbound-checkconf /etc/unbound/unbound.conf && grep -rn 'auto-trust-anchor-file' /etc/unbound/unbound.conf.d/
```

第一条应为 0 且不再增长；第二条应无输出（或确认该文件属于另一个实例的配置）。

**失败模式 / 回滚点**：删掉 `auto-trust-anchor-file` 行并 restart，日志里的 priming 报错应立即停止。若业务要求本机出 AD 标志，正确做法不是把它加回来，而是改成递归解析器角色或换用本地根区镜像。

## 巡检脚本生成配置的幂等写法

上游切换交给外部脚本，就必须保证脚本**幂等**。下面这个形态是本章最值得抄走的一段：

```bash
#!/bin/sh
# /usr/local/sbin/unbound-upstream-sync.sh
# 由 cron / systemd timer 每分钟调用；配置没变就不 install、不 restart
set -eu

STAGING=$(mktemp /run/unbound-upstream.XXXXXX)
DEST=/etc/unbound/unbound.conf.d/10-forward.conf
trap 'rm -f "$STAGING"' EXIT

# 1) 探测唯一上游，失败就保持现状（fail-closed，不自动降级）
UPSTREAM=192.0.2.53
if ! dig @"$UPSTREAM" example.org A +time=1 +tries=1 +short >/dev/null 2>&1; then
    logger -t unbound-sync 'upstream probe failed, keep current config'
    exit 0
fi

# 2) 先写临时文件，再比较
cat >"$STAGING" <<EOF
# generated by unbound-upstream-sync.sh; teaching addresses only
forward-zone:
  name: "."
  forward-addr: ${UPSTREAM}
  forward-tls-upstream: no
  forward-first: no
EOF

if cmp -s "$STAGING" "$DEST"; then
    exit 0                      # 内容一致：不 install、不 restart
fi

unbound-checkconf "$STAGING" >/dev/null 2>&1 || {
    logger -t unbound-sync 'generated config invalid, keep current config'
    exit 1
}
install -m 0644 "$STAGING" "$DEST"
systemctl restart unbound
logger -t unbound-sync "upstream config installed: ${UPSTREAM}"
```

**为什么这么设**：`cmp -s` 是整段的轴心。巡检任务常被设成每分钟跑一次，如果每次都无条件 `install` + `systemctl restart`，那台转发器的缓存就会被**每分钟清空一次**：命中率归零、所有热门域名重新回源、延迟曲线周期性锯齿，而配置内容其实一个字都没变。`cmp -s` 在内容相同时直接退出，把"探测"与"变更"解耦。

其余几处也是必要的：先写 `mktemp` 临时文件再 `install`，避免脚本中途失败留下半截配置；`unbound-checkconf` 在 `install` 之前跑，保证语法错误时磁盘上的生效文件没被动过；探测失败时 `exit 0` 且只记日志，是刻意的 fail-closed——不因为一次抖动就把上游换成别处，也不退回明文；`trap` 清理临时文件。

注意这段脚本只演示单上游明文形态。切到 5.4 节的 DoT 上游时，把 `forward-addr` 换成 `192.0.2.53@853#dns.example.org`、`forward-tls-upstream: yes` 即可；**认证名与端口都参与 `cmp -s` 的比较**，所以切换上游会正常触发一次 restart，而"每分钟重复写同一段"不会。

**怎么验证**：

```bash
/usr/local/sbin/unbound-upstream-sync.sh
stat -c '%y %n' /etc/unbound/unbound.conf.d/10-forward.conf
sleep 65
stat -c '%y %n' /etc/unbound/unbound.conf.d/10-forward.conf
systemctl show unbound -p NRestarts
```

两次 `stat` 的 mtime 应完全相同，`NRestarts` 不增长；再手工改一个字符并跑脚本，应看到 mtime 更新且 restart 计数加一。这两条一起才算验证了幂等。

**失败模式 / 回滚点**：最常见的错是把 `unbound-checkconf "$STAGING"` 写成检查 `$DEST`——那样检查的是旧文件，新错误配置照样被 install。另一个错是 `restart` 写成 `reload` 且同时改了 `interface`（第 4 章：interface 变更只有 restart 生效）。回滚：脚本生成的只有一个 `10-forward.conf`，用备份覆盖并 restart 即可；`logger` 记录给出了每次 install 的时间点，便于定位。

## 转发器的验收清单

| 检查项 | 期望 | 命令 / 依据 |
|---|---|---|
| 上游数量 | 恰好 1 条 | `unbound-control list_forwards` |
| 回退 | 关闭 | 配置里 `forward-first: no` |
| 传输 | 按设计只用 TCP / 853 | `tcpdump` 抓对应端口，不应出现明文 53 出向 |
| 证书认证名 | 已指定 `#name` | `forward-addr` 里 `#` 后非空 |
| CA 包 | 已配置且可读 | `tls-cert-bundle` + `openssl s_client` |
| 信任锚 | 转发器上不存在 | `grep -rn auto-trust-anchor-file` |
| 回环豁免 | 仅在本机多实例链上放开 | `do-not-query-localhost: no` |
| 幂等 | 配置不变则不重启 | `cmp -s` + `stat` mtime + `NRestarts` |
| 故障可见 | 上游断时明确 SERVFAIL | 断链演练，不是静默降级 |

最后一行是这一章的收敛点：fail-closed 的价值不在于"永远不失败"，而在于**失败时看得见**。两波都失败就回 SERVFAIL，让监控和人有东西可查；静默明文降级、静默绕过过滤表、每分钟静默清缓存，才是真正难查的三类故障。

## 本章要点

- `forward-zone: name "."` 加唯一 `forward-addr` 才是单上游；多条 `forward-addr` 是**并发竞速取最快**而非主备，会把查询送到既定上游之外，令过滤表失效。
- `forward-first: no` 是 fail-closed 核心，禁止 SERVFAIL 时逃出该区自行递归；`forward-first: yes` 会打开静默明文降级的通道。
- `forward-tcp-upstream: yes` 让上游只用 TCP 承载（跨境 UDP 被 QoS 时更稳）；`forward-tls-upstream: yes` 提供传输加密，必须同时配 `tls-cert-bundle` 与 `#认证名`，否则证书不被认证或任何名字都被接受。
- **加密传输（DoT / DoH / DoQ）与 DNSSEC 不互相替代**：前者是传输加密 / 隐私，后者是数据完整性 / 来源认证。
- 转发器上不要配 `auto-trust-anchor-file`：信任锚需自行向根 priming DNSKEY，全量转发时无法完成，日志持续 `failed to prime trust anchor` 并伴随间歇性全量 SERVFAIL；转发器与验证器二选一。
- `do-not-query-localhost: no` 只在本机另一实例作为设计中的下一跳时才用，且需保证端口、pidfile、控制接口互不重叠且拓扑不成环；巡检脚本用 `mktemp` + `cmp -s` + `unbound-checkconf` + `install` 保证幂等，配置没变就不 install、不 restart，否则每分钟都会清空缓存。

## 参考资料

- Unbound 官方手册，`unbound.conf(5)` 的 Forward Zone Options、Authority Zone Options 与 `tls-cert-bundle`：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound-control(8)` 的 `list_forwards` 与统计命令：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- Unbound 官方手册，DNS-over-TLS / DNS-over-HTTPS 主题页：https://unbound.docs.nlnetlabs.nl/en/latest/topics/privacy/dns-over-https.html
- NLnet Labs，Unbound 项目页与文档入口：https://nlnetlabs.nl/projects/unbound/about/
- RFC 7858，*Specification for DNS over Transport Layer Security (TLS)*：https://www.rfc-editor.org/rfc/rfc7858
- RFC 5011，*Automated Updates of DNS Security (DNSSEC) Trust Anchors*：https://www.rfc-editor.org/rfc/rfc5011
- RFC 8767，*Serving Stale Data to Improve DNS Resiliency*（转发器故障时的兜底语义）：https://www.rfc-editor.org/rfc/rfc8767
