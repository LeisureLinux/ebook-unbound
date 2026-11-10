# HTTPDNS：为什么引入、如何用 Unbound 自建、大厂后端如何架构

本章讨论的是一种**不属于 DNS 协议体系**的解析服务形态。它不是加密传输的又一种写法，也不是某个 RFC 的实现，而是应用用 HTTP(S) 去调一个私有 API、拿回一组 IP、再自己改写网络请求的过程。第 12 章的 DoH [RFC 8484] 与它名字相近，但载荷、客户端形态与信任模型都不同；本书把那层区分收在此章统一讲，其他章不再复述。



## 先把它和标准 DNS 分开

一次普通解析是「存根解析器 → 递归解析器 → 权威服务器」，载荷始终是 DNS wire format，QNAME、QTYPE、RCODE、TTL、RRSIG 都有固定位置。第 3 章的四种角色、第 10 章的 DNSSEC 验证、第 7 章的缓存与负缓存，全都建立在这份报文结构上。

HTTPDNS 的一次调用则是：

```text
GET /d?host=api.example.com&uid=app-001&ts=<TS>&nonce=<NONCE>&sig=<SIG> HTTP/1.1
Host: 192.0.2.53            ← 入口是 IP 字面量，不是待解析的域名

→ {"host":"api.example.com","ips":["192.0.2.10","192.0.2.11"],
   "ttl":60,"client_ip":"198.51.100.23"}
```

请求里没有 DNS 报文，应答里没有 answer section，也没有可以挂 RRSIG 的位置。它是**应用层 API**，只是恰好在做「名字 → 地址」这件事。因此它没有 RFC，也不该被叫作「某种 DNS 协议」：HTTP 本身有 RFC，但 URL 格式、参数名、返回字段、鉴权、错误码、SDK 行为全部由各家产品自定义。厂商文档自己也是这么区分的——把私有协议写成「支持身份鉴权、业务调度、批量解析加速、便于扩展」，把 [RFC 8484] DoH 写成「标准协议，格式固定，部分网络库原生支持，但无鉴权机制、不可扩展、不支持传递业务参数，因此不支持自定义解析」。

这条界线决定了后面所有取舍：**DoH 能挂进第 12 章那套 dnsdist + nginx + Unbound 链路，是因为它仍是一次规规矩矩的 DNS 查询；HTTPDNS 挂不进去，因为 Unbound 收不到 DNS 报文。**

| 维度 | 传统 DNS / DoH | HTTPDNS（私有 API） |
|---|---|---|
| 载荷 | DNS wire format；DoH 外加 HTTPS 封装 | JSON 或分号分隔的纯文本 IP 列表 |
| 客户端 | 系统存根、浏览器、支持 DoH 的网络库 | App 内嵌 SDK，或改造过的网络库 |
| 端点来源 | 本地配置 / URI Template，管理员可控 | SDK 内置，通常硬编码 IP 或自举域名 |
| 业务参数 | 无法携带 | 可带 `uid`、App 版本、运营商、自定义标识 |
| 鉴权 | 标准协议无 | Account ID + 密钥 / HMAC / 参数加密 |
| 自定义解析 | 无 | 按客户端出口 IP、UIN、版本返回不同结果 |
| 缓存与 TTL | 递归解析器与 HTTP 缓存 | SDK 自己实现，可预解析、持久化、测速排序 |
| DNSSEC / dnstap / 过滤表 | 全部仍在链路上 | 全部被跳过 |

表里最后一行是运维最该记住的：一旦 App 走了私有 API，管理员在网关上改 DHCP option、改 RDNSS、压 DDR 都影响不到它。第 13 章讨论的「客户端绕过」在这里不是绕过某条规则，而是绕过了整套 DNS 子系统。本章不评价这种取舍的好坏，只把它作为引入动机与自建约束的前提写清楚。

## 为什么要引入 HTTPDNS

HTTPDNS 在国内移动端兴起，不是因为「HTTP 比 UDP 快」——恰恰相反，多一次 HTTPS 往返通常更慢。它要解决的是**运营商 LocalDNS 不可控**带来的一组具体问题。厂商产品文档归纳的四条收益是「防劫持、精准调度、0ms 解析延迟、快速生效」，中文实践文章则把 LocalDNS 的痛点列得更细：劫持、TTL 不生效、解析延迟、调度不准、缺少异地容灾。把这些合并成工程语言，是下面五条。

### 痛点一：劫持与污染

LocalDNS 由运营商下发，链路明文，中间设备可以抢答、改写或注入。对移动 App 来说，轻则被插广告，重则核心 API 返回不可用地址。私有 API 走 HTTPS 到自己的服务端，链路上的中间设备看不到也改不了；入口用 IP 字面量，连「解析解析服务域名」这一步都不交给 LocalDNS。这里的边界要说准：**HTTPS 保护的是 App 到解析服务端这一段，不保护 App 拿 IP 之后的业务连接**，后者仍靠业务自己的 TLS。

### 痛点二：TTL 不生效、变更慢

权威侧把一条 A 记录从 `192.0.2.10` 改成 `192.0.2.20`，客户端多久能看到？取决于链路上每一层缓存。部分 LocalDNS 不严格遵循权威 TTL，会把旧结果留很久；中文实践材料记录过 TTL 被拉到数十小时量级的情况。故障时这最要命：切流已经做完，一批用户还连着已经下线的 IP。

HTTPDNS 服务端可以给客户端返回一个**独立于权威 TTL 的 `ttl`**，并在控制台改解析配置后主动让边缘缓存失效。第 7 章讲过负缓存会把瞬时故障放大成小时级故障，HTTPDNS 绕开 LocalDNS 后，这类放大不再由不受控的第三方决定——代价是缓存策略变成自己的责任。

### 痛点三：调度不准

CDN 与 GTM 靠 DNS 把用户导到就近边缘节点，判断依据是**递归解析器的地址**，不是真实客户端地址。当客户端配置了别处的公共递归解析器、或运营商把查询转发到异地节点时，权威侧看到的出口 IP 与用户实际位置可能差得很远，结果是跨网、跨地域访问。

HTTPDNS 服务端在 TCP 连接里直接看到客户端出口 IP，应答里甚至会把 `client_ip` 回显出来（本章开头的示例响应即是这个形状）。这让「按客户端地区与运营商选节点」重新成为可能。厂商文档把这一点写成「直接得到客户端的出口网关 IP，从而更准确地判断客户端地区和运营商」；注意它和 [RFC 7871] 的 EDNS Client Subnet 不是一回事：ECS 是把网段信息塞进 DNS 报文给权威侧，HTTPDNS 是根本不发 DNS 报文，在应用层自己拿。

### 痛点四：容灾与切换粒度

LocalDNS 对业务方是黑盒：它挂了、被污染了、转发到异地了，App 只能等超时。私有 API 的客户端握着一组服务 IP，可以做轮转重试、按网络切换刷新、失败降级回 `LocalDNS`。厂商的最佳实践文档把这套写成明确要求：内置多个启动 IP、冷启动 / 网络切换 / 每 8 小时 / 全部失败时更新服务 IP 列表、单 IP 失败轮转到下一个、全部失败才降级。这种粒度是 DHCP 下发的单一 DNS 地址给不了的。

### 痛点五：业务身份参与调度

标准 DNS 报文里放不进「这个用户属于哪个灰度桶」「这个客户端版本要走哪组接入点」。私有 API 可以带 `uid`、版本、地域等参数，服务端据此返回不同 IP。这是「带业务身份的流量调度」，不是传统意义上的域名解析；也正因为需求超出了 DNS 报文的表达能力，厂商才没有把它做成 RFC，而是各发一套互不兼容的 SDK。

### 什么时候不该引入

五条痛点不等于所有场景都该上 HTTPDNS。厂商产品文档明确写着：它**适用于 C/S 架构**，因为拿到 IP 后必须能定制网络请求；浏览器 / B/S 架构里网络实现对开发者是黑盒，不适合。再叠上本章后面要讲的客户端改造成本（SNI、证书校验、代理检测、WebView 适配），合理的落地范围通常是「核心 API、支付、登录、直播推拉流」这类少数域名，其余仍走系统 DNS。

这里有一条和第 12 章呼应的判断：**如果你的目标只是「链路加密、别让链路上的设备看到查询」，用 DoH 就够了，而且成本低得多**——系统或浏览器原生支持，本机过滤表、DNSSEC 验证、dnstap 观测全都还在。只有需要「真实出口 IP 调度、按业务身份自定义解析、变更秒级生效、SDK 侧缓存策略」时，才轮到私有 API。把两者混为一谈，最常见的后果是照着 DoH 教程配了一遍 dnsdist，最后发现拿不到 `client_ip`、也没法按 uid 分流。

## 用 Unbound 自建：三层缺一不可

### 认知前提：Unbound 不是 HTTPDNS 服务器

按第 1 章定下的边界，Unbound 只做递归、缓存、DNSSEC 验证与非常有限的本地权威服务（`local-zone` / `local-data`）。它不提供 HTTP JSON API，也不会替客户端改写网络请求。**只把 Unbound 开成 DoH 端口，得到的是标准 DoH，不是 HTTPDNS。**

完整自建是三层：

```text
[App / SDK]  ← 第三层：客户端改造，不能省
   │ ① HTTPS GET /d?host=...&sig=...（入口为 IP 字面量）
   ▼
[HTTP(S) 前端]  ← 新增的唯一组件：鉴权、取 client_ip、查规则、发 DNS 查询
   │ ② 标准 DNS 报文（UDP/53 或 TCP/53，回环）
   ▼
[Unbound]  ← 已有能力：递归 / DNSSEC / 缓存 / local-zone 自定义视图 / dnstap
   ▼
[JSON 响应] → App 拿 IP 直连，保留 Host 头与 SNI
```

前端与 Unbound 之间**仍然是标准 DNS 报文**。这是本章最关键的架构判断：私有 API 的「私有」只体现在 App ↔ 前端那一段；后端完全可以、也应该复用一台正常的递归解析器，别去重造解析逻辑。

### 拓扑与端口规划

教学环境用一台网关 `gw-01`，上面跑三个角色，端口互不冲突：

```yaml
# /etc/unbound/unbound-httpdns.conf —— 专供 HTTPDNS 前端的实例（示意）
server:
    interface: 127.0.0.1@5358
    port: 5358
    access-control: 127.0.0.0/8 allow
    access-control: 0.0.0.0/0 refuse
    do-ip4: yes
    do-ip6: no
    # 与第 4 章一致的 EDNS 与缓存基线
    edns-buffer-size: 1232
    max-udp-size: 1232
    msg-cache-size: 32m
    rrset-cache-size: 64m
    prefetch: yes
    serve-expired: yes
    # 本实例仍做真验证，别因为「只是给前端查」就关掉
    auto-trust-anchor-file: "/var/lib/unbound/root.key"
    val-permissive-mode: no
    # 私有地址应答要放行，否则内网视图会被 private-address 剔除
    private-address: 192.168.0.0/16
    private-address: 10.0.0.0/8
    private-domain: "internal.example.net."
    # 自定义视图：对应厂商收费的「自定义解析 / SDNS」
    local-zone: "internal.example.net." static
    local-data: "api.internal.example.net. 300 IN A 192.0.2.10"
    local-data: "api.internal.example.net. 300 IN A 192.0.2.11"

# 观测：前端额外记 client_ip / view，Unbound 侧 dnstap 照常保留
dnstap:
    dnstap-enable: yes
    dnstap-socket-path: "/run/unbound-httpdns/dnstap.sock"
    dnstap-log-client-query-messages: yes
    dnstap-log-client-response-messages: yes
```

**① 为什么这么设**：单独起一个实例而不是复用网关对 LAN 的 `:53`，有三个理由。一是对外服务的那个实例带过滤表、dnstap 与面向全网的 `access-control`，把私有 API 的查询混进去会让计费、限流和日志口径全乱；二是自定义解析可能与过滤表冲突（`always_null` 会先于递归命中，第 24 章讲过优先级）；三是 HTTPDNS 前端是本机回环调用，只需要 `127.0.0.0/8 allow`，暴露面可以压到最小。`local-zone` + `local-data` 承担「按视图返回不同 IP」，`private-domain` 给内网地址开白名单，正是第 9 章那条 `private-address` 坑的标准处理。

**② 怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound-httpdns.conf
dig @127.0.0.1 -p 5358 api.internal.example.net. A +short
dig @127.0.0.1 -p 5358 example.org. A +dnssec | grep -E 'status:|flags:'
```

第一条应回两条文档地址；第二条应 `NOERROR` 且 flags 含 `ad`，证明这个实例不是「为了专用而关掉验证」的空壳。

**③ 失败模式 / 回滚点**：最常见的是 `private-address` 把自定义视图里的内网地址剔掉，表现为「前端问得到 SOA、拿不到 A」。回滚是补 `private-domain`；若 `local-data` 与过滤表同名区撞车，删掉冲突的 `local-zone` 行并 reload。整章改动只影响 `5358`，把前端指回原来的 `:53` 即可退回，不动客户端。

### 前端：HTTP(S) → DNS 的转换层

网关上若没有 nginx / caddy，用 Python 标准库就能跑最小骨架；生产环境建议把 TLS 交给成熟的反向代理。下面这段只做四件事：鉴权、取真实出口 IP、查自定义规则、把查询转成标准 DNS 报文发给上面的 Unbound 实例。

```python
#!/usr/bin/env python3
"""httpdns-front.py —— 教学骨架：HTTP(S) 私有 API → 标准 DNS 查询。
仅监听回环；生产须由 TLS 反向代理终止，并补齐限流、共享 nonce 存储与审计日志。"""
import hmac, hashlib, json, time, struct, socket, secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

UNBOUND = ("127.0.0.1", 5358)
KEY = b"<TOKEN>"                      # 每 App 一把，由控制台下发，不进客户端包
ALLOWED = {"api.example.com", "api.internal.example.net."}
WINDOW = 300
_seen = {}                            # 教学用；多节点须换带 TTL 的共享存储

def canonical(p):
    return "&".join(f"{k}={quote(str(p[k]), safe='')}"
                    for k in sorted(p) if k != "sig")

def sign(p):
    return hmac.new(KEY, canonical(p).encode(), hashlib.sha256).hexdigest()

def dns_query(name, qtype=1):
    """构造最小 DNS 查询并解析 answer 段里的 A/AAAA；教学骨架，不处理 CNAME 链。"""
    qid = secrets.randbelow(65536)
    labels = b"".join(bytes([len(x)]) + x.encode() for x in name.rstrip(".").split("."))
    pkt = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\x00" \
          + struct.pack("!HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(pkt, UNBOUND)
    data, _ = s.recvfrom(4096)
    qd, an = struct.unpack("!HH", data[4:8])
    off = 12
    for _ in range(qd):                       # 跳过 question
        while data[off]: off += data[off] + 1
        off += 5
    ips, ttl = [], 60
    for _ in range(an):                       # 只取 IN/A 的简明形态
        if data[off] & 0xC0 == 0xC0: off += 2
        else:
            while data[off]: off += data[off] + 1
            off += 1
        rt, rc, ttl, ln = struct.unpack("!HHIH", data[off:off+10])
        off += 10
        rd = data[off:off+ln]; off += ln
        if rt == 1 and rc == 1 and ln == 4:
            ips.append(".".join(map(str, rd)))
    return ips, ttl

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        p = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        got = p.pop("sig", "")
        # 四道检查：白名单 / 时间窗 / 常量时间比较 / nonce 去重
        if u.path != "/d" or p.get("host") not in ALLOWED:
            return self._send(403, {"error": "forbidden"})
        try: ts = int(p.get("ts", "0"))
        except ValueError: return self._send(401, {"error": "bad ts"})
        if abs(time.time() - ts) > WINDOW or not p.get("nonce"):
            return self._send(401, {"error": "stale/missing ts or nonce"})
        if not hmac.compare_digest(sign(p), got):
            return self._send(401, {"error": "bad signature"})
        nk = f"{p['nonce']}:{ts}"
        if nk in _seen: return self._send(401, {"error": "replay detected"})
        _seen[nk] = ts + WINDOW

        client_ip = self.client_address[0]        # HTTPDNS 的核心：真实出口 IP
        try:
            ips, ttl = dns_query(p["host"])
        except (socket.timeout, OSError):
            return self._send(503, {"fallback": "localdns"})   # 让 App 降级
        self._send(200, {"host": p["host"], "ips": ips, "ttl": ttl,
                         "client_ip": client_ip,
                         "view": "lan" if client_ip.startswith("192.168.") else "wan"})

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

ThreadingHTTPServer(("127.0.0.1", 8080), H).serve_forever()
```

**① 为什么这么设**：`ALLOWED` 白名单不能省，否则这台东西就变成对任意域名开放的解析放大器；HMAC 用 `compare_digest` 做常量时间比较，避免逐字节时序侧信道；`ts` + `nonce` 必须一起验，只有 HMAC 的话攻击者抓一个合法 URL 就能原样重放；`client_ip` 取自 socket peer 而不是请求参数，正是「精准调度」这条收益的来源；`dns_query()` 发的是标准 DNS 报文，所以 Unbound 侧的 DNSSEC 验证、缓存、dnstap 全都照常工作。第 5 章那条「单上游不是冗余」的教训在这里同样适用：前端只指向一个 Unbound 实例，切换交给外部巡检，别在代码里塞两个目标做竞速。

**② 怎么验证**：

```bash
# 1) 后端先自证可用
dig @127.0.0.1 -p 5358 api.example.com. A +short
# 2) 前端正常请求（ts/nonce/sig 由同一个 canonical() 生成）
python3 - <<'PY'
import time, secrets, hmac, hashlib
from urllib.parse import quote
KEY=b'<TOKEN>'
p={'host':'api.example.com','uid':'app-001','ts':str(int(time.time())),
   'nonce':secrets.token_hex(8)}
c='&'.join(f'{k}={quote(str(p[k]),safe="")}' for k in sorted(p) if k!='sig')
print(c+'&sig='+hmac.new(KEY,c.encode(),hashlib.sha256).hexdigest())
PY
# 3) 反例：改一个 sig 字符应 401；同一串发两次第二次应 replay detected
curl -sS 'http://127.0.0.1:8080/d?<上面的串>'
# 4) 确认前端确实问了 Unbound（dnstap 里应出现对应 QNAME）
```

**③ 失败模式 / 回滚点**：`dns_query()` 是教学骨架，只解析直接的 A 记录、不追 CNAME、不处理压缩指针的完整形态、不做 TCP 回退，遇到真实 CDN 的 CNAME 链会返回空列表——生产请换成成熟的 DNS 库，并保留 CNAME 链与 AAAA。另一个高频坑是签名规范化：`quote(safe='')` 把空格编成 `%20`，`urlencode()` 默认把空格编成 `+`，两端用不同函数算出的签名永远对不上，必须把 canonical 串固定成同一份实现。回滚：前端进程停掉即可，Unbound 侧无需改动；App 侧按契约收到非 200 就降级回系统 DNS。

### 鉴权与放大面

HMAC 解决的是「请求来自持密钥的 App / 参数没被改 / 不是旧请求重放」，它是完整性加身份认证，**不是加密**——`host` 仍在 URL 里明文。四道检查缺一不可：

| 检查 | 作用 | 漏掉的后果 |
|---|---|---|
| 域名白名单 | 限定可解析的名字 | 变成开放解析器，被拿去扫全网域名 |
| `abs(now - ts) <= 300s` | 压缩重放窗口 | 抓到的 URL 永久有效 |
| `compare_digest` | 常量时间比较 | 时序差异泄露签名前缀，可逐字节爆破 |
| `nonce` 去重 | 窗口内一次有效 | 合法 URL 被重放一万次，按次计费直接爆表 |

多节点部署时 `_seen` 必须换成带 TTL 的共享存储，否则请求轮询到另一台边缘节点就漏了重放检查。密钥粒度按 App 或按 SDK 发放，便于单独轮换、限流与计费；客户端终究可被逆向，所以白名单与频率限制是最后一道，不能指望密钥永远不泄露。要求更严时可以把 `host` 也加密（部分厂商用 AES/DES 加密域名参数），代价是排障时看不到明文 QNAME。

`Cache-Control: no-store` 要显式写：这类响应里带 `client_ip` 与视图信息，落到共享缓存里既泄漏又会让 TTL 语义出现两套时钟——第 12 章讨论 DoH 时遇到过同样的问题。

### 客户端那一半

拿到 IP 只是上半段。私有 API 的定义性特征在下半段：**URL 里是 IP，Host 头里是域名。**

```text
① GET https://192.0.2.53/d?host=api.example.com&...&sig=...
   → {"ips":["192.0.2.10"],"ttl":60,"client_ip":"198.51.100.23"}
② POST https://192.0.2.10/v1/pay HTTP/1.1
   Host: api.example.com          ← 必须手工保留
   （TLS SNI 也设为 api.example.com，证书校验用原域名）
```

HTTP 要手动写 `Host`；HTTPS 要设 SNI 并用**原域名**校验证书，否则 `certificate doesn't match IP`；按 `ttl` 缓存；网络切换时刷新（CDN 线路可能已变）；代理开启时强制关闭私有解析；全部失败时降级 `LocalDNS`。厂商文档给出的降级写法很直白：Android 侧 `okhttp3.Dns.SYSTEM.lookup(host)`，iOS 侧只需不做 host 替换、保持原有请求。

厂商《客户端接入概述》把接入方式归纳成三类，正好对应本章三层的不同取舍：SDK、HTTP API、以及 DoH。SDK 屏蔽交互复杂度并附带完整的高可用与性能优化；HTTP API 留给无法集成 SDK 的场景，但上表那些缓存、预解析、降级全得自己写；DoH 适配成本最低、网络库行为与 LocalDNS 一致，却拿不到业务参数与自定义解析。**「封闭网络库优先用 DoH，自有 App 核心域名用 SDK」是厂商文档给出的选型结论，不是本书的偏好。**

### 生产化清单（把骨架变成能上线）

厂商的 HTTP API 最佳实践几乎就是一份自建需求规格书，逐条对应到本章三层：

| 要求 | 落点 | 本章对应 |
|---|---|---|
| 启动 IP 冗余；冷启动 / 网络切换 / 每 8h / 全失败时更新服务 IP | 客户端 | 前文「容灾粒度」 |
| 单 IP 失败轮转下一个，全失败降级 LocalDNS | 客户端 | 降级契约 |
| 按 TTL 缓存；TTL 80% 预解析；持久化缓存 | 客户端 | 客户端那一半 |
| 网络切换主动刷新；按 ICMP/TCP 测速排序 | 客户端 | 精准调度 |
| 异步化，避免业务主流程同步等待 | 客户端 | 骨架里的超时与 503 |
| 鉴权解析、HTTPS 证书校验、会话 ID 排查 | 前端 + 客户端 | 鉴权与放大面 |
| 连接复用 | 前端 | 反代 TLS 层 |

还有两条容易被忽略：测速排序会**改变业务服务器原本的负载均衡策略**；网络切换时刷新所有域名会带来额外流量。它们都是「客户端自己掌握调度权」之后的必然代价，不是实现缺陷。第 26 章讲 anycast 集群时讨论过缓存不共享，这里同理：SDK 的本地缓存与 Unbound 的递归缓存是两套独立的时钟，排查「为什么改了记录还不生效」时两边都要看。

## 大厂后端是怎么架构的

公开材料里能看清的层次，可以归纳成「管控层 + 解析层 + 边缘入口」三段。注意这是**从产品文档与公开论文归纳出的共同形态**，不是某一家厂商的内部设计图；不同厂商的实现细节不公开，本章不臆测具体组件名与容量数字之外的细节。

### 三段职责

```text
[控制台 / 管控层]
   添加域名、发放 Account ID 与密钥、配置自定义解析（SDNS）、
   按次计费与额度、解析变更下发、健康检查触发切流
        │ 配置 / 策略 / 失效通知
        ▼
[解析层：全球部署的递归服务器集群]
   就近接入、多级递归节点、缓存与预取、按 client_ip / 地域 / 运营商
   决定返回哪组 IP、与权威 DNS / GTM 联动做秒级刷新
        │
        ▼
[边缘入口：Anycast / BGP 任播 IP]
   客户端 IP 直连（避免「先解析解析服务」的鸡生蛋问题），
   多集群互备、故障域隔离、DDoS 防护、SLA
```

产品文档把前两层写得很明确：HTTPDNS 是**递归 DNS 服务**，「与权威 DNS 不同，并不具备决定解析结果的能力，而是主要负责解析过程的实现」；解析层是全球部署的解析服务器集群，配大量二级递归节点覆盖主要城市。这句话值得和第 3 章的角色表并排读——大厂的 HTTPDNS 后端在角色上仍然是**递归解析器**，私有 API 只是它面向 App 的一层外观；真正的权威数据仍在各域名自己的权威服务器上。

这正好解释了本章自建方案为什么成立：Unbound 本来就是递归解析器，前端只是给它换了个入口协议。差别在于规模与管控——厂商多了控制台、策略下发、计费、全球边缘、容灾编排，而这些都不是 DNS 协议本身的能力。

### 与 CDN 的关系：三个判断要拆开

**① 它是 CDN 的入口优化层，不是 CDN 本身。** CDN 靠 DNS 调度把用户导到边缘节点，LocalDNS 的 ECS 缺失、递归转发与 TTL 乱缓存会让调度失准；HTTPDNS 能看到真实客户端出口 IP，因此返回更准的节点。它只返回 IP，不缓存内容、不加速传输、不卸载源站。

**② 服务端的部署范式与 CDN 同构。** Anycast 入口、全球多集群、就近接入、边缘节点、多地容灾——这些都是第 26 章讨论过的规模化手段，与 CDN 的分布式边缘架构是同一套工程。但同构不等于同物：一个是内容分发网络，一个是解析服务的接入层。

**③ 它和 GTM / 权威 DNS 联动，但不替代它们。** 解析变更要能秒级刷新到边缘缓存，健康检查要能触发切流；这些动作的「事实来源」仍是权威区与全局流量管理配置。厂商文档明确写 HTTPDNS 不具备决定解析结果的能力，就是这条边界。

### 大规模解析集群的公开参照

最贴近「大厂解析层」的公开架构论文是 SIGCOMM 2020 的 Akamai DNS（权威侧，不是 HTTPDNS，但规模化范式直接可借鉴）。论文披露的要点：系统在 2019 年 12 月的一个观测窗口里服务约 3600 亿次 DNS 查询；用 24 组 IPv4/IPv6 anycast 前缀对构成 anycast cloud，每个 cloud 前挂路由器与 BGP speaker；每台机器运行专用 nameserver 软件并配本地健康监控，检测到问题时由 BGP speaker 撤回该 anycast 前缀的宣告；采用 **two-tier delegation**，为不同区从 anycast cloud 集合里选一个 delegation set，实现故障域隔离与快速重配置；另有独立的映射/监控系统持续发布元数据给 nameserver，决定「应该返回哪些 IP」。

把这套读成 HTTPDNS 后端的参照，有四条可以直接迁移：

- **入口与解析解耦**：客户端打的是任播 IP，真正干活的是后面的解析集群；入口挂了撤路由，不需要改客户端配置。本章的「IP 字面量入口 + 回环 Unbound」是这个结构的单机最小版。
- **策略与应答分离**：「返回哪些 IP」由独立的映射/健康系统决定，nameserver 订阅它。对应到自建方案，就是前端的自定义规则表与 Unbound 的 `local-data` 分开维护，别把业务策略塞进解析器的缓存。
- **故障域按区切分**：two-tier delegation 让不同区落在不同 anycast cloud 集合上，避免一次故障拖垮全部域名。第 28 章的故障域隔离在这里是同一条思路。
- **健康探测驱动撤路由**：本地 agent 连续探测，异常即 withdraw，比靠 TTL 自然收敛快得多。

要如实标注差异：Akamai DNS 是**权威**基础设施，HTTPDNS 后端是**递归**服务，两者在 DNS 层级里位置不同；论文里的 24 组任播前缀、two-tier delegation 是权威侧的设计，不能直接说成「某厂 HTTPDNS 就是这么搭的」。能借鉴的是规模化范式，不是逐字照搬的组件。ESORICS 2024 关于加密 DNS 基础设施依赖与集中化风险的测量工作则提供了另一面：入口高度集中本身就是新的故障域，多集群互备不是可选项。

### 为什么必然是「按次售卖的 SaaS + 各发一套 SDK」

把这三层合起来看，就能解释产品形态：边缘入口、全球集群、管控台、策略下发、计费与额度，都是重资产；而客户端必须改造网络库，且各家参数名、签名规范化串、加密方式、错误码互不兼容，换厂商要换 SDK、重做证书与 Host 处理。于是它天然长成「云厂商按解析次数售卖的 SaaS」，而不是一个可以标准化互操作的协议。微信小程序那一类「平台层代调第三方解析服务」（`enableHttpDNS` + 服务 ID）也是同一逻辑的变体：由平台统一接管，服务商须通过平台的安全认证，错误码由平台定义。

这也给出一条务实的选型边界：**自建 HTTPDNS 在工程上完全可行，但你要自己承担的不是「解析」，而是入口冗余、策略下发、SDK 适配、降级契约与容灾编排。** Unbound 把最难的递归与验证解决掉了，剩下的全是分布式系统工程。

## 它不可能覆盖 App 的全部解析

最后一节用来给本章的适用面封边。私有 API 在 App 里必然是**分层混合**，不是「全量替换」，原因有五条，全部是设计约束而非实现不彻底。

**自举问题。** 解析服务自己的地址怎么找？主流做法是客户端内置一组 IP 字面量（Anycast），但 IP 变更需要发版，所以厂商都留「调度服务接口 / 服务 IP 动态更新」这条兜底，文档建议冷启动、网络切换、每 8 小时、全失败时各更新一次。那一次更新仍可能走系统 DNS。

**SDK 只能改自己控制的网络栈。** App 自己的 OkHttp / NSURLSession / Cronet 能接管；系统 WebView 里的 H5 只能靠平台专门方案（内核级 DNS 托管一类的能力）；广告、推送、埋点、登录、地图等第三方 SDK 各用自己的网络栈；APNs/FCM、NTP、captive portal、OCSP/CRL 完全不受 App 控制；小程序走平台层。典型 App 里十几个第三方 SDK，绝大多数仍走系统 DNS。

**降级是规范，不是异常。** 所有厂商文档都写死：解析超时、返回空、鉴权失败、额度耗尽 → 必须回退 LocalDNS。既然兜底路径明写着要走系统 DNS，「一部分查询一定走 OS DNS」就不是缺陷。

**代理与企业网络。** 系统 HTTP 代理开启时，直连 IP 会破坏代理语义；企业 DNS 视图、内网域、802.1X 认证网、IPv6-only 合成等场景，系统 DNS 才是正确路径。部分平台在检测到代理时主动强制关闭私有解析。

**TLS 成本。** 拿到 IP 后要手工设 SNI、保留 Host、改证书校验，成本不低。多数团队只对核心 API / 支付 / 直播域名开启，图片、日志、统计、静态资源仍走系统 DNS。

所以厂商控制台那一步「添加域名到解析列表」不只是计费口径，它同时定义了覆盖范围：白名单之外的域名 SDK 根本不接管。整个形态是：

```text
层 0  系统 DNS（必然存在，且先于一切）
      自举 / 调度接口、控制台与上报、推送、系统服务、第三方 SDK、
      降级兜底、代理与内网场景
层 1  私有 HTTP API（按域名白名单接管）
      核心业务 API、支付、登录、直播、需按 uid / 版本调度的域名
层 2  IP 直连（私有 API 的结果）
      URL 是 IP，Host / SNI 是原域名，按返回的 ttl 缓存
```

这也回到本章开头那条界线：HTTPDNS 绕开 LocalDNS 的同时，也绕开了本机过滤表、DNSSEC 验证与 dnstap 观测。对运维而言，判断「这一次解析归谁管」比判断「解析对不对」更靠前——第 32 章给出的排查顺序是「先定位改写者」，第 36 章的工具箱里 `dig @server` 会绕开存根与本地策略，在这里同样适用：**指定服务器看到的是那台服务器怎么答，不是 App 实际拿到了什么。**

## 本章要点

1. HTTPDNS 没有 RFC，它是应用层私有 API：载荷是 JSON / 纯文本 IP，不是 DNS wire format，因此 DNSSEC、负缓存、过滤表与 dnstap 全都挂不上去。DoH [RFC 8484] 仍是标准 DNS 报文，两者只是名字相近。
2. 引入动机是 LocalDNS 不可控的五个具体痛点：劫持与污染、权威 TTL 不生效导致变更慢、递归出口与客户端位置不符导致 CDN 调度失准、容灾切换粒度粗、业务身份无法参与调度。只想要链路加密时用 DoH 成本更低。
3. 自建 = **Unbound（递归 / 验证 / `local-zone` 自定义视图 / dnstap）+ HTTP(S) 前端（鉴权、取 `client_ip`、发标准 DNS 查询）+ 客户端改造（IP 直连、保留 `Host` 与 SNI、按 `ttl` 缓存、失败降级）**，三层缺一不可。前端与 Unbound 之间仍是标准 DNS 报文，私有性只在 App ↔ 前端那一段。
4. 前端必须做四道检查：域名白名单、时间窗、常量时间签名比较、`nonce` 去重；签名规范化串两端要用同一个 URL 编码函数；多节点时 nonce 存储须共享。教学骨架不追 CNAME 链、不做 TCP 回退，不能直接上生产。
5. 大厂后端可归纳为「管控层 + 递归解析层 + Anycast 边缘入口」三段：产品文档明确它是递归服务、不决定权威结果；Akamai DNS（SIGCOMM 2020）的任播前缀、two-tier delegation、健康探测撤路由、策略与应答分离，是可借鉴的规模化范式，但它是权威侧架构，不能逐字照搬。
6. 私有 API 在 App 里必然按域名白名单分层混合：自举、第三方 SDK、系统服务、代理场景与降级兜底都绕不开系统 DNS。控制台「添加域名」既是计费口径，也是覆盖范围。

## 参考资料

- RFC 8484, *DNS Queries over HTTPS (DoH)* — https://www.rfc-editor.org/rfc/rfc8484
- RFC 7871, *Client Subnet in DNS Queries* — https://www.rfc-editor.org/rfc/rfc7871
- RFC 5737, *IPv4 Address Blocks Reserved for Documentation* — https://www.rfc-editor.org/rfc/rfc5737
- 阿里云 EMAS，《HTTP 接口自定义接入最佳实践》— https://help.aliyun.com/zh/document_detail/2868007.html
- 阿里云 EMAS，《客户端接入概述》— https://help.aliyun.com/zh/document_detail/2979687.html
- 阿里云 EMAS，《DoH 接入指南》（私有协议与 RFC 8484 的官方对比）— https://help.aliyun.com/zh/document_detail/2979559.html
- 阿里云，《HTTPDNS 产品简介》（C/S 适用、B/S 不适用、递归服务定位）— http://docs-aliyun.cn-hangzhou.oss.aliyun-inc.com/pdf/httpdns-product-introduction-cn-zh-2020-07-01.pdf
- 微信开放文档，《移动解析 HttpDNS》— https://developers.weixin.qq.com/miniprogram/dev/framework/ability/HTTPDNS.html
- Kyle Schomp 等，*Akamai DNS: Providing Authoritative Answers to the World's Queries*, SIGCOMM 2020 — https://groups.cs.umass.edu/wp-content/uploads/sites/3/2020/07/sigcomm2020-final289.pdf
- RIPE-823, *DNS Resolver Recommendations* — https://www.ripe.net/publications/docs/ripe-823

