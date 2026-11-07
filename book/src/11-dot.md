# DoT：端口 853 的服务端与客户端

端口 853 是 DoT（DNS-over-TLS）的专用端口：DNS 报文按 [RFC 1034] 的线格式原样封装在 TLS 记录里，前面不加 HTTP 层，也不改报文结构。它与 DNSSEC 完全是两件事——DNSSEC 提供数据完整性与来源认证，DoT 提供**这一段链路**的传输加密与隐私，两者不能互相替代。第 5 章已经把「转发器作为 DoT 客户端」的上游方向讲过，本章把另一侧补齐：让 Unbound 在 853 上作为服务端接受加密查询，并给出一套完整的客户端侧配置。

以下全部地址均为教学用示例地址，取自 [RFC 5737] 的 `192.0.2.0/24`、`198.51.100.0/24`，IPv6 用 `2001:db8::/32`；域名统一用 `resolver.example.net`、`dns.example.org`，不涉及任何真实公网地址。实验环境按「一台家庭网关 + 一台境外云主机的实验环境」理解。

## 服务端的最小骨架：`@853` 才是开关

Unbound 对 DoT 的启用方式很容易被误读：`tls-service-key` / `tls-service-pem` 只是**把证书装上去**，真正决定「哪个监听端口跑 TLS」的是 `interface` 行上的 `@853` 后缀与 `tls-port`。官方手册的说明是：`tls-port`（默认 853）上只对那些以 `@端口` 形式配置的 interface 提供 TLS 服务，而且这些端口**不再提供普通 DNS TCP 服务**。

```yaml
# /etc/unbound/unbound.conf.d/10-dot-server.conf
# 教学用示例地址，服务端为 cloud-01 上的 Unbound
server:
    # 公网侧：仅 853，且标为 TLS 端口
    interface: 0.0.0.0@853
    interface: ::0@853
    tls-port: 853

    # 本机明文入口：给本机存根解析器与本地诊断用，不暴露到公网
    interface: 127.0.0.1@5353
    interface: ::1@5353

    tls-service-key: "/etc/unbound/tls/server.key"
    tls-service-pem: "/etc/unbound/tls/server-chain.pem"
    tls-cert-bundle: "/etc/ssl/certs/ca-certificates.crt"

    username: "unbound"
    chroot: ""
    do-udp: yes
    do-tcp: yes
    access-control: 127.0.0.0/8 allow
    access-control: 192.0.2.0/24 allow
    access-control: 2001:db8::/32 allow
    access-control: 0.0.0.0/0 refuse
    access-control: ::0/0 refuse

    # 出向 EDNS 缓冲，DNS Flag Day 2020 的保守值
    edns-buffer-size: 1232
    max-udp-size: 1232

    qname-minimisation: yes
    qname-minimisation-strict: no
    harden-glue: yes
```

**为什么这么设**：`0.0.0.0@853` 与 `::0@853` 把两个地址族都挂在 TLS 端口上；`127.0.0.1@5353` 与 `::1@5353` 是普通 DNS 端口，方便本机用 `dig -p 5353` 做不带 TLS 的对照诊断。`tls-service-pem` 必须是**完整证书链**（叶子证书 + 中间 CA，按此顺序拼接），否则某些客户端会因链不完整而失败。`tls-cert-bundle` 在这里不是给客户端看的下游证书，而是 Unbound **向外出向连接**（如加密上游、`auth-zone` 拉取）时用来认证对端的 CA 包，两项用途不要混。`access-control` 默认无匹配即 `refuse`，这里显式写出允许段与拒绝段，避免日后加接口时漏配。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf.d/10-dot-server.conf
ss -lntp | grep -E ':853|:5353'
```

`ss` 应显示 853 上只有 TCP 监听、5353 上同时有 UDP/TCP。`unbound-checkconf` 只校验语法与文件路径，不校验证书是否可被客户端信任，后者要靠 11.4 节的 `openssl s_client`。

**失败模式 / 回滚点**：最常见的错是只写了 `interface: 0.0.0.0` 加 `tls-service-key`，却没写 `@853`——结果 853 根本没被监听，或 53 上被当成了 TLS 端口而普通 TCP DNS 失效。第二个坑是证书文件放在 `root` 权限目录或 `chroot` 之外，此时换证必须 **restart**，`unbound-control reload` 读不到新文件（官方手册明确写了这点）；回滚点就是保留上一份 `server-chain.pem` 与 `server.key` 的备份，restart 前先做 `unbound-checkconf`。

### 证书文件与权限

```bash
install -d -m 0750 -o root -g unbound /etc/unbound/tls
# 实际部署时把私钥与完整链写入该目录，例如：
#   install -m 0640 -o root -g unbound server.key /etc/unbound/tls/server.key
#   cat leaf.pem intermediate.pem > /etc/unbound/tls/server-chain.pem
#   install -m 0644 -o root -g root /etc/unbound/tls/server-chain.pem /etc/unbound/tls/server-chain.pem
openssl x509 -in /etc/unbound/tls/server-chain.pem -noout -subject -dates
openssl pkey -in /etc/unbound/tls/server.key -check -noout
```

**为什么这么设**：私钥只让 `root` 与 `unbound` 组可读；证书链是公开信息，0644 即可。Unbound 在绑定端口后降权到 `username: unbound`，所以私钥的组权限必须包含 `unbound`，否则会以低权限身份读取失败。官方手册也提示，ACME 类客户端常按「服务以 root 启动」来设权限，需要主动检查。

**怎么验证**：

```bash
namei -l /etc/unbound/tls/server.key
unbound-checkconf /etc/unbound/unbound.conf.d/10-dot-server.conf
```

**失败模式 / 回滚点**：`namei -l` 任何一级目录缺执行位都会导致降权后读不到；日志里通常是 TLS 初始化失败而不是「证书过期」。回滚：恢复 0640/0750 权限后 restart，不要通过关掉 `username` 来「绕过」权限问题——那等于把整个递归解析器跑在 root 下。

## 证书的两条路线：Let's Encrypt 与自签 + 客户端 pin

DoT 的认证名来自客户端配置里的 `#名字`（见 11.3），服务端证书必须覆盖这个名字。两条路线各有边界。

### 公共 CA（以 Let's Encrypt 为例）

```bash
# 教学示例：用 DNS-01 挑战为 resolver.example.net 签发证书
certbot certonly --dns-rfc2136 \
  --dns-rfc2136-credentials /etc/letsencrypt/rfc2136.ini \
  -d resolver.example.net --non-interactive --agree-tos -m admin@example.org
cat /etc/letsencrypt/live/resolver.example.net/fullchain.pem \
  > /etc/unbound/tls/server-chain.pem
cat /etc/letsencrypt/live/resolver.example.net/privkey.pem \
  > /etc/unbound/tls/server.key
chown root:unbound /etc/unbound/tls/server.key && chmod 0640 /etc/unbound/tls/server.key
systemctl restart unbound
```

**为什么这么设**：公共 CA 的优势是客户端只要带系统 CA 包就能认证，不需要额外分发材料；代价是必须有一个可解析、可被 CA 验证的域名，并且续期要能触发 restart。用 DNS-01 而不是 HTTP-01，是因为这台机器上 80/443 可能根本不开放（见 11.5 节共端口取舍）。`fullchain.pem` 已经是叶子 + 中间 CA，直接可用，不要再手工拼一次。

**怎么验证**：

```bash
openssl s_client -connect 192.0.2.1:853 -servername resolver.example.net \
  -showcerts -CAfile /etc/ssl/certs/ca-certificates.crt </dev/null 2>/dev/null \
  | grep -E 'Verify return code|subject=|issuer='
```

期望 `Verify return code: 0 (ok)`。

**失败模式 / 回滚点**：certbot 的续期钩子如果只做 `reload`，而私钥路径在 chroot 之外，新证书不会被加载，表现为续期后某一天突然全量 TLS 握手失败（旧证书过期的那一刻）。钩子必须 `systemctl restart unbound`，并在钩子开头跑一次 `unbound-checkconf`。回滚：保留 `server-chain.pem.bak` / `server.key.bak`，restart 前校验。

### 自签 CA + 客户端 pin

自己签一张只用于这台解析器的证书，把 CA 公钥（或叶子证书的 SPKI 哈希）固化到客户端，可以避免「公共 CA 误签发」这一类信任面，也省掉域名与续期流程。

```bash
# 教学示例：生成自签 CA 与服务端证书（仅在实验环境内使用）
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout /etc/unbound/tls/lab-ca.key \
  -out /etc/unbound/tls/lab-ca.pem \
  -subj "/CN=Lab DoT CA" -addext "basicConstraints=critical,CA:TRUE"

openssl req -newkey rsa:2048 -nodes \
  -keyout /etc/unbound/tls/server.key \
  -out /etc/unbound/tls/server.csr \
  -subj "/CN=resolver.example.net"
openssl x509 -req -in /etc/unbound/tls/server.csr \
  -CA /etc/unbound/tls/lab-ca.pem -CAkey /etc/unbound/tls/lab-ca.key \
  -CAcreateserial -days 825 -sha256 -out /etc/unbound/tls/leaf.pem \
  -extfile <(printf 'subjectAltName=DNS:resolver.example.net\nextendedKeyUsage=serverAuth\n')

cat /etc/unbound/tls/leaf.pem /etc/unbound/tls/lab-ca.pem \
  > /etc/unbound/tls/server-chain.pem
chmod 0640 /etc/unbound/tls/server.key && chown root:unbound /etc/unbound/tls/server.key

# 生成客户端要 pin 的 SPKI SHA-256（Base64）
openssl x509 -in /etc/unbound/tls/leaf.pem -pubkey -noout \
  | openssl pkey -pubin -outform DER \
  | openssl dgst -sha256 -binary | openssl base64 -A
```

**为什么这么设**：`subjectAltName` 必须写 DNS 名，现代 TLS 栈不再看 CN；`extendedKeyUsage=serverAuth` 防止这张证书被拿去做别的用途。生成 SPKI 哈希而不是证书指纹，是因为**换证书时可以复用同一把私钥**，pin 不失效；如果 pin 的是整张证书指纹，每次续期都要重发所有客户端配置。

**怎么验证**：

```bash
openssl verify -CAfile /etc/unbound/tls/lab-ca.pem /etc/unbound/tls/leaf.pem
openssl s_client -connect 192.0.2.1:853 -servername resolver.example.net \
  -CAfile /etc/unbound/tls/lab-ca.pem </dev/null 2>/dev/null \
  | grep 'Verify return code'
```

**失败模式 / 回滚点**：自签路线的典型故障是客户端拿系统 CA 包去验证自签证书，必然失败——此时不是「DoT 不通」，而是「认证材料不对」。回滚：客户端侧改回带公共 CA 的上游，或把 `lab-ca.pem` 正确分发为 `tls-cert-bundle`。pin 值一旦泄露不敏感（它是公钥哈希），但**换私钥必须同步换 pin**，这是运维上最容易漏的一步。

## 客户端：单上游、TLS 认证名、fail-closed

下面是一份完整可复制的客户端配置。客户端是网关上的 Unbound 转发器，上游是云主机上的 DoT 服务端（用公共解析器地址 `192.0.2.53` 或 `1.1.1.1` 作教学占位均可，重点是 `@853#名字` 的形态）。

```yaml
# /etc/unbound/unbound.conf.d/10-dot-client.conf
# 教学用示例地址：上游为 192.0.2.53@853（可替换为公开 DoT 服务）
server:
    interface: 127.0.0.1@53
    interface: ::1@53
    access-control: 127.0.0.0/8 allow
    access-control: 192.168.68.0/24 allow
    access-control: 0.0.0.0/0 refuse

    # 认证上游证书所用的 CA 包；自签场景换成 lab-ca.pem
    tls-cert-bundle: "/etc/ssl/certs/ca-certificates.crt"

    edns-buffer-size: 1232
    max-udp-size: 1232
    qname-minimisation: yes
    qname-minimisation-strict: no
    harden-glue: yes

    # fail-closed：禁止 SERVFAIL 时逃出本区自行递归 / 走明文
    # 转发器上不要配 auto-trust-anchor-file（见第 5 章）
    do-not-query-localhost: yes

forward-zone:
    name: "."
    # @853 指定端口，#认证名 指定证书要匹配的名字
    forward-addr: 192.0.2.53@853#resolver.example.net
    forward-tls-upstream: yes
    forward-first: no
```

若上游用公开 DoT 服务，`forward-addr` 的形态同样是 `IP@853#认证名`（例如 `1.1.1.1@853#cloudflare-dns.com`）；这里统一用 `192.0.2.53@853#resolver.example.net` 作为教学占位，避免把章内示例绑到某家具体厂商的名字上。认证名必须与该服务的证书 SAN 一致，不是随便填的标签。

**为什么这么设**：`@853` 让客户端连 TLS 端口而不是 53；`#resolver.example.net` 是**认证名**，官方手册说得很清楚：不写 `#` 时「任何名字都被接受」，证书仍需匹配 `tls-cert-bundle` 里的某个 CA——这等于把认证从「验证特定主体」退化成「任意可信 CA 签发的证书都行」，中间人只要能拿到任一可信 CA 的证书就能冒充。因此 `#` 后必须非空且与实际证书 SAN 一致（组合顺序是 `@` 在前、`#` 在后）。`forward-first: no` 是第 5 章的 fail-closed 结论沿用：上游 SERVFAIL 或超时，两波都失败就给客户端 SERVFAIL，绝不静默退回明文 53。`forward-addr` 只写一条——多条是并发竞速取最快，不是主备，会把查询送到既定上游之外。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf.d/10-dot-client.conf
dig @127.0.0.1 example.org A +short
tcpdump -ni any 'tcp port 853 or udp port 53 or tcp port 53' -c 20
```

出向抓包里应只有 `tcp port 853` 的 TLS 流量，**不应出现**向 `192.0.2.53:53` 的明文查询。再断掉上游 853，重复 `dig`，应看到 SERVFAIL 而不是「慢一点但还是返回答案」。

**失败模式 / 回滚点**：三个高频错误：① 漏写 `tls-cert-bundle` → 连接无法认证，日志里是 TLS 相关错误而非超时；② 漏写 `#认证名` → 表面上能通，但认证强度已下降，这是**静默的**；③ 误把 `forward-first` 设成 `yes` 或加了第二条 `forward-addr` → 静默明文降级或过滤表失效。回滚：恢复 `10-dot-client.conf.bak` 并 restart；断链演练后记得检查链路各层负缓存（第 13 章）。

### 自签场景的客户端写法

```yaml
# /etc/unbound/unbound.conf.d/10-dot-client-pinned.conf
server:
    interface: 127.0.0.1@53
    tls-cert-bundle: "/etc/unbound/tls/lab-ca.pem"
forward-zone:
    name: "."
    forward-addr: 192.0.2.53@853#resolver.example.net
    forward-tls-upstream: yes
    forward-first: no
```

**为什么这么设**：`tls-cert-bundle` 换成自签 CA 的 PEM，认证链闭合在我们自己手里；`#` 后的名字仍要与自签证书的 SAN 一致，Unbound 不会因为是自签 CA 就跳过主机名检查。换个角度说，「pin」在这里是通过**缩窄 CA 信任集**实现的，而不是靠某个 pin 参数（Unbound 的 `forward-addr` 没有 SPKI pin 选项；SPKI pin 用在 `kdig` 这类诊断客户端上，见 11.4）。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf.d/10-dot-client-pinned.conf
dig @127.0.0.1 example.org A +short
```

**失败模式 / 回滚点**：把 `lab-ca.pem` 分发错版本（比如分发了私钥或旧 CA）会导致所有客户端一起 TLS 失败，且各客户端表现一致，容易被误判为「上游挂了」。回滚：客户端 `tls-cert-bundle` 指回系统 CA 包，同时上游临时换回公共 CA 证书。

## 验证命令：`openssl s_client` 与 `kdig`

服务端的 TLS 层与 DNS 层要分开验证。只看 `dig` 成功，无法证明「证书被正确认证」。

```bash
# ① 证书链 / 认证名 / SNI（把 192.0.2.1 换成实际服务端教学地址）
openssl s_client -connect 192.0.2.1:853 \
  -servername resolver.example.net \
  -CAfile /etc/ssl/certs/ca-certificates.crt \
  -showcerts -tlsextdebug </dev/null 2>/dev/null \
  | grep -E 'Verify return code|subject=|issuer=|ALPN|server name'

# ② 用 kdig 走 DoT 实际发一次查询（严格认证）
kdig -d @192.0.2.1 +tls-ca +tls-hostname=resolver.example.net \
     +tls-sni=resolver.example.net example.org A

# ③ 用 kdig 走 SPKI pin（自签或不想依赖 CA 时）
kdig -d @192.0.2.1 +tls-pin=<BASE64_SPKI_SHA256> \
     +tls-hostname=resolver.example.net example.org A

# ④ 一次调用里复用同一条 TLS 连接，验证连接复用
kdig @192.0.2.1 +tls-ca +tls-hostname=resolver.example.net +keepopen \
     example.org A example.net AAAA

# ⑤ 不带认证名的机会式（Opportunistic）模式，仅作连通性对照
kdig -d @192.0.2.1 +tls example.org A
```

**为什么这么设**：`openssl s_client` 只验证 TLS 握手与证书链，`Verify return code: 0 (ok)` 加上 `subject=` 中的 SAN 匹配，才算服务端这一侧装对了；`kdig +tls-ca +tls-hostname` 则同时验证「DNS 报文能装进 TLS 并拿回答案」，是端到端的检查。`+tls-pin` 用 Base64 编码的 SPKI SHA-256 做带外密钥 pin（Out-of-Band key-pinned privacy profile），对应 [RFC 7858] 的认证模型；`+tls-sni` 显式设置 SNI。`+keepopen` 让同一次 `kdig` 调用里的多个查询共用一条连接，用来观察连接复用是否生效。第 ⑤ 条的 `+tls` 是机会式模式，不做认证，**只用于判断端口通不通**，绝不能当作验收通过。

**怎么验证**（验收判据）：

```bash
kdig @192.0.2.1 +tls-ca +tls-hostname=resolver.example.net example.org A \
  | grep -E 'status: NOERROR|ANSWER SECTION|TLS'
unbound-control stats_noreset | grep -E 'num.query.tcpout|num.query.tls' || true
```

`status: NOERROR` 且有 ANSWER SECTION 才算通过；若第 ① 步 `Verify return code` 非 0，不要往下查 DNS，先回去修证书链。

**失败模式 / 回滚点**：`+tls-ca` 未指定文件时默认用系统证书库，自签场景必须 `-CAfile` 指向 `lab-ca.pem`（`kdig` 用 `+tls-ca=/path` 形式）。另一个坑是 `+tls-hostname` 与 `-servername` 写错成 IP——证书里通常没有 IP 的 SAN，会报主机名不匹配。回滚：先用第 ⑤ 条确认 853 可连，再逐项加回认证参数，二分定位。

## 与 nginx / HAProxy 共端口的取舍

853 是专用端口，链路边界清晰：防火墙放行 853/TCP 即可，抓包能一眼区分「加密传输（DoT / DoH / DoQ）」与「普通 Web」。但专用端口也意味着在只放行 443 的网络里会被直接丢掉。想让 443 同时承载 Web 与 DoT，有两条路。

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| 独占 853 | Unbound 直接 `0.0.0.0@853` | 配置最简、审计最清楚、无需额外组件 | 只放行 443 的网络里不通 |
| nginx `stream` 按 SNI 分流 | nginx 监听 443，SNI 为 DNS 名时转给本机 853，其余转 Web | 只占一个公网端口 | 多一层进程、TLS 终止位置变化、日志归属变复杂 |
| HAProxy TCP 模式 | 按 SNI / ALPN 或源地址分流，后端指向 853 | 与健康检查、限速、多后端集成方便 | 同上，且要正确处理 PROXY protocol |
| 改用 DoH | 443 上跑 HTTPS，见第 12 章 | 穿透性最好，与 Web 完全同形 | 引入 HTTP 层、端点仍属第三方、日志归属要重写 |

nginx 的 `stream` 模块可以按 TLS SNI 分流，但这里要非常小心：**分流是在 nginx 上看 ClientHello 的 SNI，TLS 本身仍由后端 Unbound 终止**（nginx 只做 TCP 转发，不解密）。因此客户端证书认证的对象依然是 Unbound，nginx 不需要、也不应该持有私钥。

```nginx
# /etc/nginx/stream.d/dot-multiplex.conf —— 教学示例，仅示意分流形态
stream {
    map $ssl_preread_server_name $backend {
        resolver.example.net  127.0.0.1:853;
        default               127.0.0.1:8443;
    }
    server {
        listen 443;
        ssl_preread on;
        proxy_pass $backend;
        proxy_connect_timeout 5s;
    }
}
```

**为什么这么设**：`ssl_preread` 只读 ClientHello 里的 SNI，不解密流量，所以后端 Unbound 仍独立完成 TLS 握手与证书认证；`default` 指向本地 Web 的 8443，避免把普通 HTTPS 误送进 853。

**怎么验证**：

```bash
nginx -t
openssl s_client -connect 192.0.2.1:443 -servername resolver.example.net \
  -CAfile /etc/ssl/certs/ca-certificates.crt </dev/null 2>/dev/null \
  | grep -E 'Verify return code|subject='
kdig -d @192.0.2.1 -p 443 +tls-ca +tls-hostname=resolver.example.net example.org A
```

**失败模式 / 回滚点**：DoT 客户端**不一定发送 SNI**（协议并不强制），一旦客户端省略 SNI，`$ssl_preread_server_name` 为空，查询会被送到 Web 后端，表现为「偶尔 TLS 握手成功但 DNS 无响应」。这类间歇故障极难查。回滚：退回独占 853，或在 443 上改用 DoH（第 12 章），DoH 的 HTTP 层天然带 `:authority`，不依赖 SNI 分流。

### SNI 与隐私的边界

SNI 是明文的：即使 DNS 报文被 TLS 加密，ClientHello 里的 `servername` 仍会向链路上的观察者暴露「客户端在连哪台解析器」。因此 SNI 不能作为隐私增强手段，它只是**分流与虚拟主机**的机制。想减少这类元数据，可考虑：客户端不配 SNI 而靠 IP + 认证名认证（但会破坏上面的分流方案）、或使用 ECH 一类机制（超出本章范围）。

更实际的边界是：**SNI 暴露的是「你连了谁」，不是「你查了什么」**。查询内容在 TLS 内，QNAME 不外泄给链路观察者；但查询量、时序、报文长度仍是可观测的元数据，[RFC 7830] 的 EDNS(0) padding 与 [RFC 9230] 的相关机制可用于对齐长度（协议层对比见第四篇第 17 章）。

## 连接复用、TCP fastopen 与 MTU / EDNS 1232

### 连接复用是 DoT 的性能前提

TLS 握手 + TCP 三次握手的成本远高于一次 UDP 查询。如果每条查询都新建连接，DoT 的延迟会退化到不可接受。因此 DoT 依赖**长连接 + 多查询复用**，相关参数在官方手册里是一组 TCP 选项：

```yaml
# /etc/unbound/unbound.conf.d/20-tcp-tuning.conf
server:
    tcp-idle-timeout: 30000        # 毫秒，空闲连接保持 30 秒
    tcp-reuse-timeout: 60000       # 毫秒，出向持久化连接保持 60 秒
    max-reuse-tcp-queries: 200     # 单条连接最多承载 200 次查询
    edns-tcp-keepalive: yes
    edns-tcp-keepalive-timeout: 120000
    incoming-num-tcp: 50
    outgoing-num-tcp: 50
    so-reuseport: yes
    stream-wait-size: 4m
    http-nodelay: yes
```

**为什么这么设**：`tcp-idle-timeout` 管入向（客户端连到我们 853）、`tcp-reuse-timeout` 管出向（我们连权威服务器），两者常被混为一谈；`max-reuse-tcp-queries` 防止单条连接无限占用工作线程，达到上限后重建。`edns-tcp-keepalive` 启用后，`edns-tcp-keepalive-timeout` 会覆盖 `tcp-idle-timeout`，由服务端把建议的空闲时长告诉客户端，避免「客户端以为连接还在、服务端已经关了」的半开状态。`stream-wait-size` 限制 TCP/TLS 流上排队应答的总内存，是 DoT/DoH 高并发下的内存护栏。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf.d/20-tcp-tuning.conf
ss -tnp 'sport = :853' | head -20
kdig @192.0.2.1 +tls-ca +tls-hostname=resolver.example.net +keepopen \
     example.org A example.net AAAA example.com TXT
unbound-control stats_noreset | grep -E 'num.query.tcp|num.query.tcpout'
```

`ss` 里同一条连接应承载多次查询；`+keepopen` 的三次查询在抓包里应只出现一次 TLS 握手。

**失败模式 / 回滚点**：把 `tcp-idle-timeout` 设得极短（例如 200ms）会让移动端频繁重握手、耗电且延迟抖动；设得极长则连接表被空闲客户端占满，官方手册说明在空闲缓冲低于阈值时会**自动逐级缩短**这个值，所以「参数没生效」不一定是配置错。回滚：恢复默认值（30s / 60s / 200 次），观察 `incoming-num-tcp` 是否触顶。

### TCP fastopen 的适用范围

TCP Fast Open（TFO）允许在 SYN 里带数据，省掉一次 RTT。它对「短连接、每次重连」收益明显，对本章的长连接复用场景收益有限，且依赖内核与中段网络不丢弃带数据的 SYN。客户端诊断可用 `kdig +fastopen` 观察，服务端侧是否启用要看系统与 Unbound 编译/平台支持，不能假定「配了就一定生效」。

```bash
# 客户端侧诊断：显式要求 TFO，并用 -d 看协商细节
kdig -d @192.0.2.1 +tls-ca +tls-hostname=resolver.example.net +fastopen \
     example.org A
sysctl net.ipv4.tcp_fastopen
```

**为什么这么设**：给出可观测的命令而不是「建议开启」，是因为在跨境链路上带数据的 SYN 被中间设备丢弃的概率不低（第 14 章会给出 UDP/QUIC 被 QoS 的实测对比）。在已经复用长连接的前提下，TFO 省下的一次 RTT 通常不如把连接保持住来得实在。

**怎么验证**：`sysctl` 返回非 0 仅说明内核侧允许；真正是否用了 TFO 要靠 `kdig -d` 的输出与抓包中的 TCP Fast Open 选项判断。

**失败模式 / 回滚点**：TFO 引发的连接异常往往表现为「偶发首次查询超时、重试后正常」，很容易被误判成上游抖动。回滚：`+fastopen` 去掉即可，Unbound 侧不必改动。

### MTU 与 EDNS 1232

`edns-buffer-size` 与 `max-udp-size` 的默认值都是 **1232**（DNS Flag Day 2020 建议值）。这个数字不是随手取的：IPv6 最小 MTU 是 1280，扣掉 40 字节 IPv6 头与 8 字节 UDP 头，剩 1232；这样即便链路上 MTU 只有 1280（例如 WireGuard 隧道、PPPoE 1492 上的封装叠加）也不会 IP 分片。历史上常见的 `edns-buffer-size: 1472` 在以太网 1500 下看似安全，一旦套上隧道就分片，而分片在很多网络里被直接丢弃，表现为「等超时后重试 TCP」。

```yaml
# /etc/unbound/unbound.conf.d/30-edns.conf
server:
    edns-buffer-size: 1232   # 出向：通告给对端的 EDNS 重组缓冲
    max-udp-size: 1232      # 入向：允许返回给客户端的最大 UDP 应答
```

**为什么这么设**：两个参数方向不同，`max-udp-size` **只管入向应答**，出向上游看到的是 `edns-buffer-size` 通告的值；第 4 章已经强调过这个区分。DoT 走 TCP/TLS，本身没有 UDP 分片问题，但**服务端自己向权威服务器发出的迭代查询仍有 UDP 段**，所以这两个参数在 DoT 服务端上照样要设对；否则客户端会看到「DoT 连上了，但某些大应答超时后变慢」。

**怎么验证**：

```bash
dig @127.0.0.1 -p 5353 +bufsize=4096 example.org A | grep -E 'EDNS|MSG SIZE'
tcpdump -ni any 'ip[6:2] & 0x1fff != 0 or ip6[6] = 44' -c 10   # 观察分片
ip link show wg0 | grep mtu || true
```

`dig` 输出里的 `udp: 1232` 证明入向被限制住了；抓分片只是为了确认链路上没有碎片。

**失败模式 / 回滚点**：把 `edns-buffer-size` 降到 512 能绕过最严苛的 MTU，但会制造大量 TC=1 与 TCP 重试，官方手册称其为「极端值」。回滚：改回 1232，并检查隧道 MTU（`ip link show`）而不是只调 DNS 参数。

## 一张验收清单

| 检查项 | 期望 | 命令 |
|---|---|---|
| 853 监听 | 仅 TCP，无 UDP | `ss -lntp 'sport = :853'` |
| 证书链 | `Verify return code: 0` | `openssl s_client -connect 192.0.2.1:853 ...` |
| 认证名 | SAN 与 `#名字` 一致 | `openssl x509 -in server-chain.pem -noout -text \| grep -A1 'Subject Alternative'` |
| 端到端 DNS | NOERROR + ANSWER | `kdig @192.0.2.1 +tls-ca +tls-hostname=... example.org A` |
| 单上游 | 恰好一条 `forward-addr` | `unbound-control list_forwards` |
| 无明文出向 | 抓包无 `dst port 53` | `tcpdump -ni any 'dst port 53' -c 20` |
| fail-closed | 断 853 后 SERVFAIL | 断链演练 |
| EDNS | 出向 / 入向均 1232 | `dig +bufsize=4096 ... \| grep udp:` |
| 连接复用 | 一次握手多次查询 | `kdig +keepopen` + 抓包 |

最后一行回到本章的边界：DoT 保护的是**客户端 ↔ 解析器这一段链路**。服务端自己向权威服务器发出的迭代查询通常仍是明文 53，除非把递归放到自己可控的机器上（第 6 章的本地根区镜像、第 14 章的加密隧道）。「开了 853」不等于「整条解析路径都加密了」，也不等于「应答数据可信」——后者是 DNSSEC 的职责。

## 本章要点

- 启用服务端 DoT 需要三件事同时到位：`tls-service-key` + `tls-service-pem`（完整链）、`tls-cert-bundle`（出向认证用 CA）、以及 `interface: 0.0.0.0@853` / `tls-port: 853`；`@853` 的端口不再提供普通 DNS TCP。证书文件在 chroot 外或仅 root 可读时，换证必须 **restart** 而非 reload。
- 公共 CA（Let's Encrypt）省去客户端分发，但要域名与续期钩子；自签 CA 把信任集缩到自己手里，客户端 `tls-cert-bundle` 必须指向该 CA，SPKI pin 适合诊断客户端而非 Unbound 的 `forward-addr`。
- 客户端 `forward-addr: 192.0.2.53@853#resolver.example.net` 里 `@` 指定端口、`#` 指定认证名；**不写 `#` 时任何名字都被接受**，是静默的认证退化。`forward-tls-upstream: yes` 与 `forward-first: no` 一起构成 fail-closed，且只留一条上游。
- 验证要分两层：`openssl s_client` 验 TLS 链与 SNI，`kdig +tls-ca +tls-hostname` 验端到端 DNS；`+tls` 机会式模式只能用来判断端口连通性，不作验收。
- nginx `stream` + `ssl_preread` 可按 SNI 让 443 与 DoT 共存，但 DoT 客户端不强制发 SNI，省略 SNI 会被分流到 Web 后端；SNI 本身明文，暴露「连了谁」而非「查了什么」。
- DoT 性能依赖长连接复用（`tcp-idle-timeout` 管入向、`tcp-reuse-timeout` 管出向、`max-reuse-tcp-queries` 限单连接次数）；`edns-buffer-size` / `max-udp-size` 默认 1232 是 IPv6 最小 MTU 1280 的推导结果，1472 在隧道上分片会被丢。

## 参考资料

- Unbound 官方手册，`unbound.conf(5)`：`tls-service-key`、`tls-service-pem`、`tls-cert-bundle`、`forward-addr` 的 `#认证名` 与 TCP 选项：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，DNS-over-HTTPS / TLS 相关主题页（下游加密服务与 HTTP/2 要求）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/privacy/dns-over-https.html
- Unbound 官方手册，Performance Tuning（`so-reuseport`、线程与缓冲区）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/core/performance.html
- NLnet Labs，Unbound 项目文档入口：https://nlnetlabs.nl/documentation/unbound/
- RFC 7858，*Specification for DNS over Transport Layer Security (TLS)*：https://www.rfc-editor.org/rfc/rfc7858
- RFC 8310，*Usage Profiles for DNS over TLS and DNS over DTLS*（机会式与带外 pin 认证模型）：https://www.rfc-editor.org/rfc/rfc8310
- RFC 7766，*DNS Transport over TCP - Implementation Requirements*（连接复用与流水线）：https://www.rfc-editor.org/rfc/rfc7766
- RFC 7830，*The EDNS(0) Padding Option*：https://www.rfc-editor.org/rfc/rfc7830
- OpenSSL 官方手册，`openssl-s_client(1)`：https://www.openssl.org/docs/man3.0/man1/openssl-s_client.html
- Knot DNS 官方手册，`kdig(1)`（`+tls-ca`、`+tls-pin`、`+fastopen`、`+keepopen`）：https://www.knot-dns.cz/docs/3.3/html/man_kdig.html
