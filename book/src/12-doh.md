# DoH：与 Web 服务共存的加密解析

DoH（DNS-over-HTTPS，[RFC 8484]）把 DNS 报文塞进 HTTP 请求体，再经 TLS 送出，因此能跑在标准 443 端口上，与 Web 流量同形。这一形态带来两个必须一开始就讲清的事实：**Unbound 本身不是给公网做 DoH 前端的合适组件**，以及 **DoH 端点仍属第三方，日志留存方是谁要写进设计文档**。本章按「前端 → 后端 → 客户端 → 绕过面」的顺序落地配置。

以下地址均为教学用示例地址（`192.0.2.0/24`、`198.51.100.0/24`、`2001:db8::/32`），域名用 `dns.example.org`、`gw.example.net`，凭据一律写作 `<TOKEN>` 或 `example-token-0000`，不涉及任何真实公网地址、域名或密钥。

## 为什么 Unbound 不是 DoH 前端

Unbound 从 1.12.0 起提供下游 DoH 能力，但它要求编译时带 `libnghttp2`，**只支持 HTTP/2**，且实现的是固定的 `/dns-query` 端点（`http-endpoint` 可改名），媒体类型只有 `application/dns-message`。官方手册返回的状态码是协议层的：404（路径不匹配）、413/414（payload 过大）、415（media type 不对）、400（畸形请求）。这套能力用于「本机或内网里让 HTTPS 客户端能接入」是合适的，放到公网边缘就缺了几样东西：

| 公网前端要做的事 | Unbound 下游 DoH | dnsdist | nginx |
|---|---|---|---|
| 同一 443 上按路径分流 Web / DoH | 只能独占标记为 `https-port` 的端口 | 需要反代配合 | 原生 `location` |
| 自定义 Header 校验（Bearer / token） | 不支持 | 可用 HTTP 规则 | 原生 `map` / `auth_request` |
| 按路径、按 SNI、按客户端做限速与灰度 | 无 | 规则链完整 | 限速模块完整 |
| HTTP/1.1 回源兼容 | 只支持 HTTP/2 | 2.x 起 nghttp2 只支持 h2 | 回源可 h2c / grpc |
| 访问日志归属与脱敏 | 只有 DNS 层日志 | 有 HTTP 状态码计数 | 有 access log，可裁字段 |
| 与已有 Web 证书、ACME 续期复用 | 需单独管理证书 | 可共用 | 天然共用 |

结论不是「Unbound 不能做 DoH」，而是**分工**：Unbound 做后端的递归 / 缓存 / DNSSEC 验证；dnsdist 做 DNS 感知的 DoH 前端与分流；nginx 做与既有 Web 服务共端口的反代与认证。三者组合时，公网只暴露 443，Unbound 只监听回环。

```yaml
# /etc/unbound/unbound.conf.d/10-backend.conf
# 教学示例：Unbound 只做后端，绝不直接监听公网 443
server:
    interface: 127.0.0.1@5353
    interface: ::1@5353
    access-control: 127.0.0.0/8 allow
    access-control: ::1 allow
    access-control: 0.0.0.0/0 refuse
    edns-buffer-size: 1232
    max-udp-size: 1232
    qname-minimisation: yes
    qname-minimisation-strict: no
    harden-glue: yes
    module-config: "validator iterator"
    auto-trust-anchor-file: "/var/lib/unbound/root.key"
```

**为什么这么设**：后端只听回环，公网侧的 TLS 终止、路径校验、认证、限速全部由前端承担；后端没有公网攻击面，也不需要证书。`module-config` 里保留 `validator`，是因为 DNSSEC 的三种状态（无信任锚 / permissive / 真验证）由后端决定，前端只搬运 DNS 报文，不能替它验证（第 10 章）。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf.d/10-backend.conf
dig @127.0.0.1 -p 5353 example.org A +dnssec +short
ss -lntp | grep 5353
```

**失败模式 / 回滚点**：如果把后端也配上 `interface: 0.0.0.0@443` + `tls-service-key`，就等于绕过了前端的所有认证与日志策略。回滚：`ss -lntp` 确认 443 只被 nginx/dnsdist 占用，注释掉多余的 `interface` 行 restart（`interface` 变更 reload 不生效）。

## dnsdist：`addDOHLocal` + `newServer(pool)` 的分流

dnsdist 是 DNS 感知的负载均衡器，适合放在 Unbound 前面：它终止 DoH 的 HTTP/2，把 DNS 报文解出来，按规则选后端池，再以普通 DNS（或 DoH / DoT）发给后端。下面的例子给出「按 QNAME 后缀分流到不同池」的完整形态。

```lua
-- /etc/dnsdist/dnsdist.conf —— 教学示例，地址均为 RFC 5737 文档段
setLocal("127.0.0.1:5300")                    -- 控制台/明文 DNS 入口（可选）

-- 后端池：default 走本机递归验证器；internal 走内网权威/转发器
newServer({address="127.0.0.1:5353", pool="default", name="unbound-recursive"})
newServer({address="198.51.100.53:53", pool="internal", name="lab-forwarder"})

-- DoH 前端：公网 443，路径带 token 前缀（见 12.4 节）
addDOHLocal("192.0.2.10:443",
  "/etc/dnsdist/tls/fullchain.pem",
  "/etc/dnsdist/tls/privkey.pem",
  {"/dns-query/<TOKEN>"},
  {reusePort=true, keepIncomingHeaders=true})

-- 按 QNAME 后缀选池：内部域及其子域进 internal，其余进 default
addAction(QNameSuffixRule("internal.example.org."), PoolAction("internal"))
addAction(AllRule(), PoolAction("default"))

-- 控制面与观测
controlSocket("127.0.0.1:5199")
setKey("<TOKEN>")
addAction("use-application-dns.net.", RCodeAction(DNSRCode.NXDOMAIN))
```

**为什么这么设**：`addDOHLocal` 的第四参数是接受查询的 URL 路径列表，这里把 `<TOKEN>` 放进路径，是因为 iOS 的 `DNSSettings` 只能配 `ServerURL`、不能加自定义请求头（12.5 节）；`keepIncomingHeaders=true` 让 `HTTPHeaderRule()` 与 `DNSQuestion:getHTTPHeaders()` 可用，便于审计 Header。`newServer` 的 `pool` 把后端分组，`PoolAction` 按 QNAME 把查询投进对应池——这就是「分流」在 DoH 前端的形态，与第 14 章的 nft 策略分流是不同层次。`QNameSuffixRule` 匹配后缀及其子域（`QNameRule` 只精确匹配一个名字），`AllRule()` 做兜底，`RCodeAction(DNSRCode.NXDOMAIN)` 在本地把浏览器 canary 域压成 NXDOMAIN（12.6 节）。

注意 `PoolAction` 的 `stop` 默认为 `true`：命中第一条规则后不再处理后续规则，所以能用作兜底的 `AllRule()` 必须放在最后。未指定 `pool` 的后端会落在默认池（空字符串 `''`）；上面把所有后端都显式命名，避免「规则没命中就落到空池」的歧义。实际部署时应按所用 dnsdist 版本的 `setPoolServerPolicy` / `PoolAction` 语义核对，见官方手册对应章节。

**怎么验证**：

```bash
dnsdist --check-config
curl -sS --http2 -H 'content-type: application/dns-message' \
  --data-binary @query.bin "https://dns.example.org/dns-query/<TOKEN>" -o reply.bin -w '%{http_code}\n'
dnsdist -c 127.0.0.1:5199 -k '<TOKEN>' -e 'showDOHResponseCodes()'
dnsdist -c 127.0.0.1:5199 -k '<TOKEN>' -e 'showServers()'
```

`showDOHResponseCodes()` 若出现大量 404，先查路径与 `addDOHLocal` 的 `urls` 是否完全一致；出现 415 则是 `content-type` 没设对。`query.bin` 可以用 `kdig +https` 生成，或先用一个已知的 DNS 线格式报文。

**失败模式 / 回滚点**：dnsdist 2.x 起用 nghttp2 实现入向 DoH，**只支持 HTTP/2，不支持 HTTP/1.1**（官方手册明确写了）。如果前面再加一层 nginx 且回源用 HTTP/1.1，会直接握手失败；解决办法见 12.3 节。另一个坑是证书/私钥权限：dnsdist 通常以非 root 运行，私钥必须对它可读，否则 `addDOHLocal` 启动时报错。回滚：把 `addDOHLocal` 注掉、保留 `addLocal` 明文入口，先恢复 DNS 可用性，再逐项加回。

### 生成一次可复用的 DoH 请求体

```bash
# 用 kdig 直接发 DoH，省去手工构造线格式报文
kdig -d @dns.example.org +https=/dns-query/<TOKEN> \
     +tls-ca +tls-hostname=dns.example.org example.org A

# 或明确 content-type 用 curl POST 一个二进制 DNS 报文
printf '\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03org\x00\x00\x01\x00\x01' \
  > /tmp/query.bin
curl -sS --http2-prior-knowledge -X POST \
  -H 'content-type: application/dns-message' \
  -H 'accept: application/dns-message' \
  --data-binary @/tmp/query.bin \
  "https://dns.example.org/dns-query/<TOKEN>" -o /tmp/reply.bin -w '%{http_code}\n'
```

**为什么这么设**：[RFC 8484] 规定 DoH 的请求与应答媒体类型都是 `application/dns-message`，GET 用 base64url 的 `dns` 参数、POST 用请求体承载原始 DNS 线格式。用 `curl` 手工发一次，可以把「HTTP 层问题」与「DNS 层问题」分开：400/404/415 是 HTTP 层，200 但应答里 SERVFAIL 才是 DNS 层。

**怎么验证**：`%{http_code}` 应为 200；`kdig` 输出里应有 `status: NOERROR` 与 ANSWER SECTION。

**失败模式 / 回滚点**：`curl` 不带 `--http2` 时默认用 HTTP/1.1，遇到只支持 h2 的前端会失败；这不是服务端挂了。回滚：加 `--http2` 或改用 `kdig +https`。

## nginx 反代：`grpc_pass` / `proxy_pass` 与 h2c 的坑

已有 nginx 在 443 上跑 Web 时，最省事的是给 DoH 加一个 `location`，把 `/dns-query` 反代给本机 dnsdist。这里要处理两个独立的坑。

### 坑一：nginx 回源不是 HTTP/2

nginx 的 `proxy_pass` 到上游默认走 **HTTP/1.1**（`proxy_http_version 1.1` 是默认值），而 dnsdist 2.x 的入向 DoH 只支持 h2。官方手册给出的绕行方案是用 `grpc_pass`：gRPC 本身就要求 HTTP/2，nginx 的 `grpc_pass` 会向上游发起 h2（或 h2c）连接，从而满足 dnsdist 的要求。

```nginx
# /etc/nginx/conf.d/doh.conf —— 教学示例
server {
    listen 443 ssl http2;
    server_name dns.example.org;

    ssl_certificate     /etc/nginx/tls/fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/privkey.pem;

    # 路径里带 token：iOS 描述文件只能配 ServerURL，不能加自定义头
    location ~ ^/dns-query/(?<doh_token>[A-Za-z0-9_-]+)$ {
        if ($doh_token != 'example-token-0000') { return 403; }

        # 用 grpc_pass 让回源走 HTTP/2（dnsdist 2.x 不收 HTTP/1.1）
        grpc_pass grpcs://127.0.0.1:8443;
        grpc_set_header Host $host;
        grpc_set_header X-Real-IP $remote_addr;
        grpc_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        access_log /var/log/nginx/doh.access.log combined;
    }

    # Bearer 校验形态（客户端能加自定义头时用）
    location /dns-query {
        if ($http_authorization != 'Bearer <TOKEN>') { return 403; }
        proxy_http_version 1.1;
        proxy_set_header content-type 'application/dns-message';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_pass http://127.0.0.1:8053/dns-query;
    }

    location / {
        return 404;
    }
}
```

**为什么这么设**：两条 `location` 演示两种认证形态。第一条把 token 放在**路径**里，用 nginx 的正则捕获并比对，适用于「客户端只能配 URL」的场景；第二条用 `Authorization: Bearer <TOKEN>`，适用于浏览器扩展、`curl`、自写客户端等能加头的场景。`grpc_pass grpcs://` 对应 dnsdist 侧仍带 TLS 的 DoH 监听；若回源是明文 h2c，则写 `grpc_pass grpc://127.0.0.1:8053`（dnsdist 的 `addDOHLocal("127.0.0.1:8053", nil, nil, ...)` 不提供证书即为明文 DoH）。

要注意 `grpc_pass` 是给 gRPC 用的：它把请求按 HTTP/2 送到上游，但**不会替你做 DoH 的媒体类型转换**。因此这个方案只适用于上游能够接受该形态的 DoH 实现；若上游是 Unbound 自身的下游 DoH（同样只支持 h2），也应先用一个最小请求验证，而不是假定「换成 grpc_pass 就通了」。

**怎么验证**：

```bash
nginx -t
curl -sS --http2 -o /dev/null -w '%{http_code}\n' -H 'content-type: application/dns-message' \
  --data-binary @/tmp/query.bin 'https://dns.example.org/dns-query/example-token-0000'
curl -sS --http2 -o /dev/null -w '%{http_code}\n' \
  -H 'authorization: Bearer <TOKEN>' -H 'content-type: application/dns-message' \
  --data-binary @/tmp/query.bin 'https://dns.example.org/dns-query'
```

第一条应 200，第二条在 token 正确的前提下也应 200；把 token 改错，两条都应 403。

**失败模式 / 回滚点**：最典型的错是把 `proxy_pass` 直接指向 dnsdist 的 DoH 端口，nginx 以 HTTP/1.1 回源 → dnsdist 直接拒绝，日志里既没有 DNS 查询也没有 200，只有连接层错误。第二个错是 `grpc_pass` 与 `proxy_pass` 写在同一 `location` 里，nginx 配置检查会报错。回滚：先用 `curl --http2-prior-knowledge http://127.0.0.1:8053/dns-query` 直连 dnsdist 确认回源可用，再接上 nginx。

### 坑二：h2c 与「明文 HTTP/2」

**h2c（HTTP/2 over cleartext TCP）** 不使用 TLS，靠 HTTP/1.1 Upgrade 或先验知识（prior knowledge）建立 h2 连接。它在「nginx → 本机 dnsdist」这一段很常见，因为公网侧 TLS 已在 nginx 终止，回源再加密一次意义不大。但有两个必须写明的边界：

1. `proxy_http_version 1.1` 不会升级成 h2；`proxy_pass` 无法直接回源 h2c。要么用 `grpc_pass grpc://`（h2 prior knowledge），要么让 dnsdist 侧接受 HTTP/1.1（2.x 已不支持）。
2. h2c 是**明文**，因此只在回环或严格隔离的内网使用；跨主机回源要走 TLS 或另加隧道。客户端到 nginx 的那段仍是 HTTPS，不受影响。

```nginx
# 回源为明文 h2c 时的最小形态（与 12.3.1 二选一）
location /dns-query {
    if ($http_authorization != 'Bearer <TOKEN>') { return 403; }
    grpc_pass grpc://127.0.0.1:8053;
}
```

**为什么这么设**：`grpc://` 明确不加密，`grpcs://` 明确加密，两者不能混写；dnsdist 侧对应 `addDOHLocal("127.0.0.1:8053", nil, nil, "/dns-query")` 这种不传证书的形态。

**怎么验证**：

```bash
nginx -t && nginx -s reload
curl -sS --http2-prior-knowledge -o /dev/null -w '%{http_code}\n' \
  -H 'content-type: application/dns-message' --data-binary @/tmp/query.bin \
  -H 'authorization: Bearer <TOKEN>' http://127.0.0.1:8053/dns-query
```

直连回源的 200 证明 h2c 通路成立；再经 443 测一次，区分「回源问题」与「公网侧问题」。

**失败模式 / 回滚点**：把 `grpc://` 写成 `grpcs://`（或反过来）会表现为 TLS 握手失败或明文被当成 TLS，日志里通常是 upstream 连接错误。回滚：统一前后端的加密形态，并保留 `nginx -t` 通过前的配置备份。

## `/dns-query` 的 `content-type` 与端点语义

[RFC 8484] 把 DoH 的请求/应答媒体类型定为 `application/dns-message`，路径惯例是 `/dns-query`。Unbound 下游 DoH 的默认 `http-endpoint` 也是 `/dns-query`，dnsdist 自 1.5.0 起默认同样如此。几个容易踩的细节：

- 请求没有 `content-type` 头时，Unbound 会**当作** `application/dns-message` 处理；但只要带了任何其他值就返回 415。因此「漏设头」和「设错头」表现不同，排查时不能混为一谈。
- GET 用 `?dns=<base64url>`，URL 过长会 414；POST 用请求体，过长会 413。移动端偶发失败时，先看是不是走了 GET 且查询被 EDNS 选项撑大。
- 路径不匹配一律 404；把 token 放路径里时，`addDOHLocal` 的 `urls`、nginx 的 `location`、描述文件里的 `ServerURL` 三处必须**完全一致**。
- DoH 端点仍属第三方：即使查询内容加密，**TLS 终止方（nginx / dnsdist 所在主机）能看到完整 HTTP 层信息**——路径、Header、客户端 IP、时序、查询量。DNS 层则由后端 Unbound 看到完整 QNAME。两处的日志留存方都要写清楚。

```bash
# 一次最小验收：分别触发 200 / 415 / 404
curl -sS --http2 -o /dev/null -w 'ok=%{http_code}\n' \
  -H 'content-type: application/dns-message' --data-binary @/tmp/query.bin \
  'https://dns.example.org/dns-query/example-token-0000'
curl -sS --http2 -o /dev/null -w 'wrong-type=%{http_code}\n' \
  -H 'content-type: application/json' --data-binary @/tmp/query.bin \
  'https://dns.example.org/dns-query/example-token-0000'
curl -sS --http2 -o /dev/null -w 'wrong-path=%{http_code}\n' \
  -H 'content-type: application/dns-message' --data-binary @/tmp/query.bin \
  'https://dns.example.org/other'
```

**为什么这么设**：这三条把端点行为固定成可回归的断言。以后换证书、改路径、加一层反代，重跑这三条就能定位到 HTTP 层的哪一段变了。

**怎么验证**：期望依次 `ok=200`、`wrong-type=415`（或前端自定义 403/415，取决于哪一层先拦）、`wrong-path=404`。

**失败模式 / 回滚点**：如果 415 变成 200，说明中间有组件把 `content-type` 改写了；如果 404 变成 200，说明 `location` 匹配范围过宽，token 校验被绕过。后者是安全问题，必须立刻收紧正则。

## iOS / Android 描述文件：`ServerURL` 与认证限制

移动端配 DoH 有两种常见途径：MDM 下发的 `com.apple.dnsSettings.managed` 描述文件（iOS/iPadOS 14+），以及 Android 的「私人 DNS / Private DNS」设置。二者的能力边界不同，直接决定了 token 该放哪。

### iOS：`DNSSettings` 只能配 `ServerURL`

Apple 的 `DNSSettings` 描述文件里，DoH 由 `ServerURL` 指定「URI 模板」（[RFC 8484] 语义），并要求 `https://` scheme；系统用 URL 里的主机名或地址去验证服务器证书。同一个字典里还有 `ServerName`（DoT 的主机名，按 [RFC 7858] 校验证书）、`ServerAddresses`、`SupplementalMatchDomains`、`PayloadCertificateUUID`（用身份证书向解析器认证客户端）等键。关键限制是：**描述文件里没有「自定义 HTTP 请求头」这一项**。因此 `Authorization: Bearer <TOKEN>` 在 iOS 上配不出来，唯一能由我们控制、又能随请求带上的字符串就是 URL 本身。

```xml
<!-- 教学示例：MDM 下发的 com.apple.dnsSettings.managed 片段 -->
<dict>
  <key>PayloadType</key><string>com.apple.dnsSettings.managed</string>
  <key>PayloadIdentifier</key><string>example.org.doh.lab</string>
  <key>PayloadUUID</key><string>00000000-0000-0000-0000-000000000001</string>
  <key>PayloadVersion</key><integer>1</integer>
  <key>DNSSettings</key>
  <dict>
    <key>DNSProtocol</key><string>HTTPS</string>
    <key>ServerURL</key>
    <string>https://dns.example.org/dns-query/example-token-0000</string>
    <key>SupplementalMatchDomains</key>
    <array/>
  </dict>
</dict>
```

**为什么这么设**：`ServerURL` 里带 token 前缀路径，等价于把「认证」折进 URL；`SupplementalMatchDomains` 留空数组表示所有域都走该解析器。注意 Apple 文档注明：从 MDM 安装时设置只作用于受管 Wi-Fi 网络，手工安装时才同时作用于蜂窝网络——这会影响「全屋覆盖」的验收结论。此外该 payload 在新版本系统中已被标记为 deprecated（改用声明式管理配置），落地时要按目标系统版本核对官方手册对应章节，不要照抄旧键名。

**怎么验证**：

```bash
# 服务端侧看请求路径是否带上了 token、有没有 403
grep 'dns-query' /var/log/nginx/doh.access.log | tail -20
# 客户端侧：装好描述文件后访问一个测试域，确认解析走的是自建端点
kdig +https=/dns-query/example-token-0000 @dns.example.org example.org A
```

**失败模式 / 回滚点**：token 放路径意味着它会进 nginx access log、CDN 日志、以及任何做 URL 记录的中间设备——**这不是秘密，只是凭据**，必须按凭据管理：定期轮换、日志里脱敏、不要复用同一 token 给所有设备。另一个坑是 URL 里的 `{?dns}` 模板语法与自定义路径混写，系统实际请求路径与 `addDOHLocal` 的 `urls` 不一致 → 404。回滚：先用不带 token 的 `/dns-query` 确认连通，再加路径段。

### Android：Private DNS 与 DoH 的边界

Android 的「私人 DNS」在用户层面配置的是一个 **DoT 主机名**（系统按 [RFC 7858] 用它做证书校验），而不是任意 DoH URL；较新版本的解析模块支持 DoH 的能力由系统实现与版本决定，配置面并不等同于 iOS 的 `ServerURL`。因此在本书的口径下：**要给 Android 一个可控的加密端点，优先用第 11 章的 DoT 853，而不是假定描述文件能配 DoH URL**。

```bash
# 教学示例：Android Private DNS 里填的是 DoT 主机名，不是 URL
#   resolver.example.net
# 服务端验证该名字能被系统解析器解析、且 853 证书 SAN 覆盖它
openssl s_client -connect 192.0.2.1:853 -servername resolver.example.net \
  -CAfile /etc/ssl/certs/ca-certificates.crt </dev/null 2>/dev/null | grep 'Verify return code'
```

**为什么这么设**：把两种机制分开写，避免把「iOS 描述文件的 ServerURL」误套到 Android 上。DoT 主机名形态还带来一个好处：它不依赖 HTTP 层的 token 路径，认证完全由证书与主机名校验完成。

**怎么验证**：`Verify return code: 0 (ok)`；再在设备上确认「私人 DNS」状态为已验证而非「无法连接」。

**失败模式 / 回滚点**：若填的是带 `https://` 的 URL，设备会把它当主机名做 DNS 查询与证书校验，必然失败。回滚：改回纯主机名，并确保该名字在系统当前 DNS 下可解析（自签 CA 场景需要先装 CA）。

## 浏览器 canary 域：让内置 DoH 主动关闭

自建 DoH 端点最容易被绕过的方式，不是链路被破解，而是**浏览器根本没用你的解析器**。Firefox 与 Chrome 各自有「是否启用内置 DoH」的判断机制，理解它们的差异比按 IP 封 443 有效得多。

### Firefox：`use-application-dns.net` 回 NXDOMAIN

Firefox 在启用内置 DoH 前会查询 canary 域 `use-application-dns.net.`。如果本地解析器对该名字返回 NXDOMAIN（或该查询被阻断导致无应答），Firefox 就不启用内置 DoH，改用系统解析器——于是流量回到我们可控的链路上。

```yaml
# /etc/unbound/unbound.conf.d/40-canary.conf
server:
    local-zone: "use-application-dns.net." always_nxdomain
    local-zone: "_dns.resolver.arpa." always_nodata
```

**为什么这么设**：`always_nxdomain` 忽略该区下的 `local-data`，保证整区一律 NXDOMAIN，不会因为某条遗留白名单而漏过；`_dns.resolver.arpa.` 的 `always_nodata` 是 DDR 压制（第 13 章展开），两者配合覆盖「浏览器自动发现加密解析器」这条绕过路径。

**怎么验证**：

```bash
dig @127.0.0.1 -p 5353 use-application-dns.net A +short
dig @127.0.0.1 -p 5353 use-application-dns.net A | grep 'status:'
dig @127.0.0.1 -p 5353 _dns.resolver.arpa. SVCB +short
```

期望 `status: NXDOMAIN`，SVCB 查询返回 NODATA（无记录、无错误）。

**失败模式 / 回滚点**：`always_nxdomain` 与 `static` 语义不同——前者忽略 `local-data`，若日后需要给个别名字留白名单要改用 `transparent`。另一个坑是只在本机 Unbound 配了 canary，但客户端的 DNS 被 nft 劫持到了别的实例（第 14 章），那台实例没配 → Firefox 照样启用内置 DoH。回滚：确认链路上**每一层**解析器都返回 NXDOMAIN。

### Chrome：靠系统解析器匹配已知提供商列表

Chrome 的内置 DoH 升级逻辑是：当系统解析器属于已知的 DoH 提供商列表时，才把该解析器升级为对应的 DoH 端点。指向私有解析器（例如 `192.168.68.1`、`fd00::1` 或 `192.0.2.1`）时，名字不在列表里，Chrome **不会自行升级**，会继续用系统解析器。因此不需要按域名封，也不需要额外 canary 域。

```bash
# 验证「系统解析器是谁」，而不是去封 443
cat /etc/resolv.conf
dig @127.0.0.1 -p 5353 whoami.example.org A +short
# 观察有没有绕过本地解析器直连外部 DoH 的流量
tcpdump -ni any 'tcp port 443 and (dst net 192.0.2.0/24 or dst net 198.51.100.0/24)' -c 20
```

**为什么这么设**：这是「白名单匹配」而不是「黑名单封禁」，所以**指向私有解析器天然不升级**。反过来，如果哪天把系统解析器改成了某家公共解析器，Chrome 就可能升级——那时封 IP 也拦不住，因为端点会换 IP、换域名。

**怎么验证**：结合 12.6.1 的 canary 断言与这里的 `tcpdump`，确认没有「客户端直接向外部 443 发 DoH」的连接；企业环境还可用浏览器策略显式禁用内置 DoH，见官方策略文档。

**失败模式 / 回滚点**：**按 IP 封 443 是 denylist 不是证明**。一方面会误伤同 IP 上的正常 Web 服务，另一方面端点换地址就失效，而且它无法告诉你「客户端到底有没有绕过」——只能证明「这几个 IP 不通」。真正要测的是「能不能绕过」：用一台客户端把系统 DNS 改成公共解析器，看浏览器是否升级到内置 DoH，以及本地 canary 是否仍生效。回滚：恢复系统解析器指向自建端点，并复查 canary 域。

## 日志留存方是谁：DoH 并不等于「无人记录」

把这一节单独列出来，是因为「加密」最容易被误读成「不留痕」。一次 DoH 查询会在至少三处留下可关联的记录：

| 位置 | 能看到什么 | 谁负责留存 | 建议 |
|---|---|---|---|
| nginx / dnsdist（TLS 终止方） | 客户端 IP、User-Agent、URL 路径（含 token）、Header、时序、状态码、报文长度 | 前端运维 | access log 裁掉 token 字段，明确保留期 |
| Unbound 后端（DNS 层） | 完整 QNAME / QTYPE、应答、缓存状态；若开 dnstap 则是查询流的完整副本 | 解析器运维 | dnstap 保留期写进配置（例如 30 天），见第 13 章 |
| 上游 / 权威服务器 | 解析器发出的迭代查询（通常仍是明文 53），看到的是**解析器 IP** 而非客户端 IP | 第三方 | 明确「DoH 端点仍属第三方」这一事实 |
| 客户端设备 / MDM | 描述文件、证书、设备标识 | 设备管理员 | 轮换token、按设备分发 |

关键结论：**DoH 端点仍属第三方**。哪怕客户端到端点这一段是加密的，端点运营方依然拥有完整的查询元数据；客户端把查询从「本地解析器」搬到「自建 DoH 端点」，本质是把信任从网关搬到了那台前端主机。若前端是同一台家庭网关，信任边界没变；若前端在云主机上，就要把「云主机上谁能读日志」写进威胁模型（第 8 章）。

```bash
# 审计：确认日志里没有把 token 原样落盘
grep -c 'example-token-0000' /var/log/nginx/doh.access.log || true
# 保留期复核（示例：30 天）
find /var/log/nginx -name 'doh.access.log*' -mtime +30 -print
```

**为什么这么设**：这两条是自查而不是配置。第一条检查凭据是否泄漏进日志，第二条检查保留期是否真的执行——「写了 30 天」和「logrotate 真的按 30 天删」是两件事。

**怎么验证**：`grep -c` 应为 0（若前端把 token 放路径，需要在 `log_format` 里用 `map` 把该段替换成 `REDACTED`）；`find` 在稳定运行的系统上不应列出 30 天前的归档。

**失败模式 / 回滚点**：最常见的失败是「前端日志脱敏做了，dnstap 全量查询流却无限期留着」——抗元数据聚合在自己门口破功。回滚：为 dnstap 单独设保留期与清理任务；日志策略变更要同步通知所有能读日志的人。

## 验收清单

| 检查项 | 期望 | 命令 |
|---|---|---|
| 公网端口 | 只有 443，Unbound 只听回环 | `ss -lntp` |
| 媒体类型 | 200 / 415 / 404 三态正确 | 12.4 节三条 `curl` |
| h2 回源 | `grpc_pass` 或 h2c，非 HTTP/1.1 | `nginx -t` + 直连回源 `curl --http2-prior-knowledge` |
| token | 路径或 Bearer 二选一，校验生效 | 改错 token 应 403 |
| iOS `ServerURL` | 路径与 `addDOHLocal` urls 一致 | 描述文件 + nginx access log |
| Android | 填 DoT 主机名而非 URL | `openssl s_client -servername` |
| canary | NXDOMAIN | `dig use-application-dns.net A` |
| DDR | `_dns.resolver.arpa.` NODATA | `dig _dns.resolver.arpa. SVCB` |
| 日志 | token 脱敏、保留期执行 | `grep -c` + `find -mtime` |

## 本章要点

- Unbound 有下游 DoH（1.12.0 起，需 `libnghttp2`、只支持 HTTP/2、只认 `application/dns-message`），但不适合单独做公网前端：**用 dnsdist 做 DNS 感知的 DoH 前端与池分流，用 nginx 做与 Web 共 443 的反代与认证，Unbound 只监听回环做递归验证**。
- `addDOHLocal(地址, 证书, 私钥, {路径}, {选项})` 定义 DoH 监听；`newServer({pool=...})` 把后端分组，`PoolAction` 按 QNAME 分流。dnsdist 2.x 用 nghttp2 实现入向 DoH，**不支持 HTTP/1.1**。
- nginx `proxy_pass` 默认 HTTP/1.1 回源会打不通 dnsdist 2.x；官方手册给出的绕行是 `grpc_pass`（`grpcs://` 加密 / `grpc://` 明文 h2c），且 `grpc_pass` 不负责媒体类型转换，需自行验证端点行为。
- `/dns-query` 的请求与应答媒体类型都是 `application/dns-message`；漏设该头时 Unbound 会当作默认处理，设成别的值返回 415；路径不匹配 404，GET 过长 414、POST 过长 413。token 放路径时三处路径必须完全一致。
- iOS 的 `DNSSettings` 描述文件只有 `ServerURL`（以及 DoT 用的 `ServerName` 等），**不能加自定义请求头**，所以 Bearer 校验用不上，token 必须折进 URL；Android「私人 DNS」填的是 DoT 主机名而非 DoH URL，优先按第 11 章的 853 方案落地。URL 里的 token 是凭据不是秘密，要轮换、要在日志里脱敏。
- Firefox 查 `use-application-dns.net` 回 NXDOMAIN 即关闭内置 DoH；Chrome 靠系统解析器匹配已知提供商列表，指向私有解析器时不升级。**按 IP 封 443 是 denylist 不是证明**，验收要能测出「客户端能否绕过」。
- DoH 端点仍属第三方：TLS 终止方看到 HTTP 元数据，后端看到完整 QNAME，dnstap 是自己的查询流副本。三处日志的留存方与保留期都要写明，否则「抗元数据聚合」在自己门口破功。

## 参考资料

- RFC 8484，*DNS Queries over HTTPS (DoH)*：https://www.rfc-editor.org/rfc/rfc8484
- RFC 7858，*Specification for DNS over Transport Layer Security (TLS)*：https://www.rfc-editor.org/rfc/rfc7858
- RFC 9462，*Discovery of Designated Resolvers (DDR)*：https://www.rfc-editor.org/rfc/rfc9462
- Unbound 官方手册，DNS-over-HTTPS 章节（HTTP/2、`http-endpoint`、状态码、`libnghttp2`）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound.conf(5)` 的 `local-zone` 类型（`always_nxdomain` / `always_nodata`）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- dnsdist 官方手册，`addDOHLocal` / `newServer` 配置参考：https://www.dnsdist.org/reference/config.html
- dnsdist 官方手册，DNS-over-HTTPS 指南（URL 路径、HTTP/1 支持、nginx `grpc_pass` 绕行、outgoing DoH）：https://www.dnsdist.org/guides/dns-over-https.html
- nginx 官方手册，`ngx_http_grpc_module`：https://nginx.org/en/docs/http/ngx_http_grpc_module.html
- nginx 官方手册，`ngx_http_proxy_module`：https://nginx.org/en/docs/http/ngx_http_proxy_module.html
- Mozilla Support，Canary domain `use-application-dns.net`：https://support.mozilla.org/en-US/kb/canary-domain-use-application-dnsnet
- Mozilla Support，企业网络禁用 DoH 的配置说明：https://support.mozilla.org/en-US/kb/configuring-networks-disable-dns-over-https
- Apple Developer，`DNSSettings` MDM payload（`ServerURL` / `ServerName`）：https://developer.apple.com/documentation/devicemanagement/dnssettings
