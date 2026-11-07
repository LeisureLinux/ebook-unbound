# Kubernetes 中的 DNS 选型

> 本章所有地址、域名与集群名均为**教学用示例**：`192.0.2.0/24`、`198.51.100.0/24`、`2001:db8::/32` 属 [RFC 5737] 与 [RFC 3849] 文档段，`gw-01` / `cloud-01` 只是教学代号，`svc.example.internal`、`dns.example.org` 不代表任何真实服务。

## 20.1 先划清一条边界：服务发现 ≠ 递归解析

Kubernetes 里的 DNS 问题和本书前四篇讨论的不是同一个问题。前四篇的递归解析器（recursive resolver）面对的是**整个公网名字空间**：从根开始迭代、做 DNSSEC 验证、缓存、过滤、走加密上游。集群里的 DNS 面对的是**一个 closed world**：`svc.example.internal`、`kube-dns.kube-system.svc.example.internal`、`<pod-ip>.<namespace>.pod.example.internal` 这些名字只在集群内有意义，答案来自 API Server 里的 Service / EndpointSlice 对象，而不是任何权威服务器。

由此得出本章最重要的结论，后面所有拓扑都围绕它展开：

> **CoreDNS 负责集群内服务发现，Unbound 负责外部递归 / 验证 / 过滤 / 加密上游。两者不是替代关系。**

把 Unbound 直接装成 `kube-system` 里的 DNS Service 是行不通的：它不知道 Service 何时扩缩容，也不会 watch EndpointSlice，`dig @unbound mysvc.default.svc.example.internal` 只会往公网根区问一个根本不存在的名字。反过来，把 CoreDNS 当成公网递归解析器用也不成立：它的 `forward` 插件只是把查询转交给上游，验证、QMIN、负缓存策略、`edns-buffer-size` 这些都是交给别人的事；CoreDNS 自己没有 DNSSEC 验证链、没有根区 priming、没有信任锚（trust anchor）管理。第 5 章讲转发器、第 10 章讲验证器，这两件事在 Kubernetes 里分别落在 CoreDNS 的 `forward` 与集群外的 Unbound 上。

一个常见的误判是「CoreDNS 已经做了缓存，为什么还要 NodeLocal 或 Unbound」。因为缓存的**位置**决定了它能否绕开内核的 conntrack 竞态（20.5），而缓存的**语义**（验证与否、负缓存多久、是否 serve-stale）决定了它能否满足第三篇的加固基线（20.7）。

## 20.2 从 kube-dns 到 CoreDNS

早期集群里 `kube-dns` 是一个三容器组合：`kubedns`（watch Service/Endpoints 并维护一棵内存树）、`dnsmasq`（缓存 + 存根转发）、`sidecar`（健康与指标）。CoreDNS 自 Kubernetes 1.11 起成为可选、1.13 起成为默认（具体版本以官方发布说明为准，见参考资料）。更替不是因为功能表多了一行，而是四个工程原因：

| 维度 | kube-dns | CoreDNS |
|---|---|---|
| 结构 | 三个容器 + 进程间 DNS 转发 | 单进程，插件链（plugin chain）按 Corefile 顺序执行 |
| 性能 | dnsmasq 与 kubedns 之间多一次本机 UDP 转发，内存与 GC 开销更高 | 单进程内完成匹配，资源占用更低 |
| 可维护性 | 改行为要动 Go 代码、重新构建三个镜像 | 改 Corefile 增删插件即可，`reload` 热加载 |
| 生态 | 项目基本停止演进 | CNCF 毕业项目，插件与上游生态持续维护 |

「插件链」是理解 Corefile 的钥匙。CoreDNS 收到一个查询后，按每个 server block 的插件顺序逐个尝试；`kubernetes` 插件命中集群内名字就作答并终止，`forward` 插件把剩余名字交给上游，`cache` 在链中位置决定了它缓存的是「插件处理后的答案」。顺序写错会直接改变语义，例如把 `cache` 放在 `kubernetes` 之前，就会把 Service 答案按 TTL 缓存住而感知不到 EndpointSlice 的变化（CoreDNS 的 `kubernetes` 插件默认给很低/零 TTL，但链的顺序仍应谨慎）。

另需记住：CoreDNS 是 CNCF 毕业项目，这一身份说明它解决的是**云原生服务发现**问题，而不是「公网递归解析器」问题。CNCF 的成熟度评价不构成把它当验证器使用的依据。

## 20.3 四种 dnsPolicy 与 resolv.conf 的形态

Pod 的 `dnsPolicy` 决定 kubelet 往容器里写一份什么样的 `/etc/resolv.conf`。这是一个很具体的、可以 `kubectl exec` 出来看的差异。

```yaml
# 教学示例：四种 dnsPolicy 的最小对照
apiVersion: v1
kind: Pod
metadata:
  name: dns-policy-demo
  namespace: default
spec:
  dnsPolicy: ClusterFirst          # 依次改成下列四值观察
  containers:
    - name: tools
      image: registry.example.internal/net-tools:latest
      command: ["sleep", "3600"]
```

**为什么这么设**：用一个只有 `sleep` 的探针 Pod，把变量收敛到 `dnsPolicy` 一项，避免业务镜像自带的 `/etc/resolv.conf` 挂载干扰观察。

**怎么验证**：`kubectl exec dns-policy-demo -- cat /etc/resolv.conf`。

**失败模式 / 回滚点**：`dnsPolicy` 是 Pod 级不可变字段，改它要靠重建 Pod（Deployment 改模板触发滚动更新）；回滚就是把模板改回 `ClusterFirst`。`None` 配错时 Pod 会完全无法解析任何名字，包括集群内名字，业务容器可能起不来，务必先在探针 Pod 上验。

| dnsPolicy | `/etc/resolv.conf` 形态 | 适用 |
|---|---|---|
| `ClusterFirst`（默认） | `nameserver` 指向集群 DNS Service 的 ClusterIP（教学示例 `192.0.2.10`），`search` 为 `<ns>.svc.example.internal svc.example.internal example.internal`，`options ndots:5` | 普通 Pod |
| `ClusterFirstWithHostNet` | 与 `ClusterFirst` 相同 | 使用 `hostNetwork: true` 的 Pod（如 CNI 组件、节点级 agent） |
| `Default` | 继承节点宿主机的 `/etc/resolv.conf`（可能是云厂商 VPC 内的解析器） | 明确要走节点解析器、不查集群内名字的 Pod |
| `None` | 不自动生成；必须配合 `dnsConfig` 手写 `nameserver` / `searches` / `options` | 需要完全自定义的应用，或接 NodeLocal / 节点级 Unbound |

两个容易踩的点：

其一，`hostNetwork: true` 的 Pod 若不显式写 `ClusterFirstWithHostNet`，kubelet 会按 `Default` 处理，容器直接读宿主机 resolv.conf，于是 `mysvc.default.svc.example.internal` 解析失败，而同节点其他 Pod 都正常。这是「只有某几个系统组件解析不了」的典型根因。

其二，`None` 不是「禁用 DNS」，而是「kubelet 完全放手」。写 `None` 却忘了 `dnsConfig`，容器里会是一份空的或继承镜像自带配置的 resolv.conf。

```yaml
# dnsPolicy: None 的完整写法（教学示例，指向节点级缓存地址）
spec:
  dnsPolicy: None
  dnsConfig:
    nameservers:
      - "192.0.2.10"       # NodeLocal DNSCache 的节点本地监听地址示例
    searches:
      - "default.svc.example.internal"
      - "svc.example.internal"
    options:
      - name: ndots
        value: "2"
      - name: edns0
```

**为什么这么设**：显式把 `ndots` 降到 2 并自己给全 `searches`，既保留集群内短名解析，又避免默认 `ndots:5` 带来的查询放大（20.4）。

**怎么验证**：`kubectl exec <pod> -- cat /etc/resolv.conf` 应只出现上面三个 `search` 与 `ndots:2`；`kubectl exec <pod> -- getent hosts mysvc` 成功。

**失败模式 / 回滚点**：`searches` 写漏 `svc.example.internal` 会让跨命名空间的短名失败；回滚把 `dnsPolicy` 改回 `ClusterFirst` 并删掉 `dnsConfig`。注意 `dnsConfig` 的 `searches` 最多 6 项（上限以官方 API 文档为准），不要照抄一份很长的域列表。

## 20.4 ndots:5 的查询放大，以及 search 域与绝对域名

`ndots: n` 的语义是：当查询名里的点数**少于 n** 时，存根解析器先按 `search` 列表逐项拼接，全部试过之后才把原始名字当绝对域名（FQDN）再查一次。默认 `ndots:5` 配上 3 个 search 域，意味着：

- `mysvc`（0 个点）→ 最多 3 次带 search 的查询 + 1 次原名 = **4 次**；
- `api.dns.example.org`（3 个点）→ 仍然先做 3 次 `api.dns.example.org.default.svc.example.internal` 之类的无效查询，再查原名 = **4 次**；
- 只有带**尾点**的绝对域名 `api.dns.example.org.`（4 个点且以点结尾表示 FQDN）才会跳过 search 直接查一次。

对外部域名，前 3 次查询会在集群内被判为 NXDOMAIN 并可能进入负缓存，然后才轮到真正那一次。这不是「多几个 UDP 包」这么简单：它把每个公网查询放大成 4 倍，直接压在 CoreDNS 与 conntrack 上，也是 20.5 里 UDP 竞态被触发的放大器。

```bash
# 观察放大：在 Pod 内抓本机出向 53
kubectl exec dns-policy-demo -- tcpdump -ni any -c 12 'port 53' &
kubectl exec dns-policy-demo -- getent hosts api.dns.example.org
```

**为什么这么设**：用最朴素的 `getent` 触发存根解析器行为，数一数实际发出的查询数，比读文档更直观。

**怎么验证**：抓包里应出现 4 个不同的 QNAME（3 个带 search 后缀 + 1 个原名）；把 `ndots` 改成 2 后，`api.dns.example.org` 只剩 1 个 QNAME。

**失败模式 / 回滚点**：降 `ndots` 到 2 之后，**集群内短名** `mysvc`（0 点）仍会走 search，没问题；但形如 `mysvc.default`（1 点）的写法会先被当成绝对域名查一次，可能拿到公网 NXDOMAIN 或慢一拍。回滚点是把该 Pod 的 `dnsConfig.options` 改回 `ndots:5`，或要求业务统一使用带尾点的 FQDN。

什么时候该降：

| 场景 | 建议 |
|---|---|
| 大量调用外部 API（3 个点以内的域名居多） | 降到 `2` 或 `3`，或代码里统一写带尾点 FQDN |
| 主要访问集群内服务，外部调用少 | 保持 `5`，收益不明显，改动反而有风险 |
| 混合型，且能改应用代码 | 优先改代码用 FQDN 带尾点，不动集群默认值 |
| 已上 NodeLocal 且 CoreDNS 压力仍高 | 降 `ndots` + 缩短 `searches`（只保留真实用到的） |

「绝对域名」的正确写法是末尾那个点：`dig api.dns.example.org. +short`。在 Corefile 与 `dnsConfig` 里写域名时也应保持一致：CoreDNS 的 `kubernetes` 插件用 `cluster.local` 风格的 `example.internal` 作为集群域，写完整域名时带尾点可避免被 server block 的后缀匹配误吞。

## 20.5 NodeLocal DNSCache：大规模集群几乎必需

NodeLocal DNSCache 以 DaemonSet 方式在每个节点跑一个轻量缓存（通常用 CoreDNS），在节点本地监听一个地址，Pod 的 DNS 查询先到这里，缓存未命中才转发给集群 DNS Service。它解决的不是「缓存命中率」这种锦上添花的事，而是两个很硬的问题：

**其一，绕开 conntrack 的 UDP 竞态。** 在 Linux 上，ClusterIP 到 Endpoint 的 DNAT 走 conntrack。UDP 五元组在并发查询下可能出现竞态：同一 Pod 几乎同时发出的两个查询（正是 `ndots:5` 放大出来的那种）被映射到同一条 conntrack 表项，或回包到达时表项已过期，表现为**随机 5 秒超时**、偶发 SERVFAIL。DNS 的默认超时与重试是 5s 量级，于是应用层的毛刺被放大成接口超时。NodeLocal 把查询终止在本机（`iptables`/`ipvs` 规则把节点本地地址直接交给本机进程，不经过 Service DNAT），这条链上的 conntrack 竞态就不存在了。

**其二，减少对中心 DNS Service 的压力。** 每个节点吸收掉热查询与放大的 NXDOMAIN 查询，CoreDNS 副本看到的 QPS 从「Pod 数 × 4」降到「节点数 × 未命中率」。上百节点的集群里，这个差别决定了要不要给 CoreDNS 堆副本。

```yaml
# 教学骨架：NodeLocal 的关键参数（完整清单见官方 addon 清单）
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: node-local-dns
  namespace: kube-system
spec:
  template:
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet   # 20.3：hostNetwork 必须显式写这一项
      containers:
        - name: node-cache
          image: registry.example.internal/dns/k8s-dns-node-cache:latest
          args:
            - "-localip"
            - "192.0.2.10,fd00:53::10"     # 节点本地监听（教学示例与文档段地址）
            - "-conf"
            - "/etc/Corefile"
            - "-upstreamsvc"
            - "kube-dns-upstream-service"  # 未命中时转发给该 Service
```

**为什么这么设**：`hostNetwork: true` 让节点本地地址不经过 Service 网络；`-localip` 同时给 IPv4 与 ULA IPv6 地址，与第 19 章的 ULA 用法一致；`-upstreamsvc` 指向一个**绕过自身**的上游 Service，避免节点缓存与中心 CoreDNS 形成环。

**怎么验证**：`kubectl -n kube-system get pods -l k8s-app=node-local-dns -o wide` 应每节点一个 Running；在节点上 `ss -lunp | grep 192.0.2.10` 能看到监听；`dig @192.0.2.10 mysvc.default.svc.example.internal +short` 返回 ClusterIP。

**失败模式 / 回滚点**：最典型的故障是**转发环**——NodeLocal 转发到中心 CoreDNS，中心 CoreDNS 又被规则引回节点本地地址。表现为所有查询 5 秒超时且 CoreDNS 日志里查询数暴涨。回滚点：保留原 `kube-dns` Service 的 ClusterIP 不动，通过修改节点上 NodeLocal 的 iptables 规则（`iptables-save > /var/tmp/iptables-before-nodelocal.txt` 快照）或直接 `kubectl delete ds node-local-dns` 恢复；删除后 Pod 的 resolv.conf 需要滚动更新才会回到中心 DNS，别指望即时生效。

第二个坑是 `dnsPolicy` 写成 `Default`（20.3）：DaemonSet 用了 `hostNetwork`，若漏掉 `ClusterFirstWithHostNet`，node-cache 容器自己解析 `kube-dns-upstream-service` 就会去问宿主机解析器，直接失败。

## 20.6 一份可复制的 Corefile

下面这份 Corefile 用于**中心 CoreDNS**（服务发现 + 转发给外部验证器），不是 NodeLocal 的那份。

```text
# CoreDNS ConfigMap（教学示例，集群域用 example.internal）
.:53 {
    errors
    health {
        lameduck 5s
    }
    ready
    log
    kubernetes example.internal in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
        ttl 30
    }
    prometheus :9153
    cache {
        success 9984 30
        denial 9984 5
    }
    rewrite stop {
        name regex (.*)\.gw\.example\.internal dns.example.org
        answer name dns.example.org {1}.gw.example.internal
    }
    forward . 192.0.2.53 {
        policy sequential
        expire 10s
        max_fails 1
        health_check 5s
    }
}
```

**为什么这么设**：把 `kubernetes` 放在链首保证集群内名字不出集群；`fallthrough` 只放行反向域，不把整个集群域漏给公网；`cache` 的 denial TTL 压到 5s 防负缓存放大；`forward` 单上游且 `max_fails 1` + `health_check 5s` 快速摘除，避免把查询挂在黑掉的验证器上。注意 `forward` 到 Unbound 走的是明文 53（集群内网络），跨节点的加密传输、fail-closed 与 DoT 上游由 Unbound 侧负责（第 11、14 章），不要指望 CoreDNS 的 `forward` 提供 TLS。

**失败模式 / 回滚点**：`rewrite` 规则写错会把集群内名字改写到公网，`cache` 放到 `kubernetes` 之前会让 EndpointSlice 变化滞后；回滚点是 `kubectl -n kube-system get cm coredns -o yaml > /var/tmp/coredns-cm-before.yaml` 快照 + `kubectl apply -f` 还原并 `rollout restart`；`reload` 插件可热加载 Corefile，但插件增删仍建议滚动重启。

**怎么验证**（一组可复制命令）：

```bash
kubectl -n kube-system get configmap coredns -o yaml | grep -A40 'Corefile'
kubectl -n kube-system rollout status deployment/coredns --timeout=90s
kubectl -n kube-system rollout restart deployment/coredns
kubectl -n kube-system rollout status deployment/coredns --timeout=90s

# 集群内名字走 kubernetes 插件
kubectl run -it --rm dnsprobe --image=registry.example.internal/net-tools:latest \
  --restart=Never -- dig +short mysvc.default.svc.example.internal A

# 外部名字走 forward → Unbound；带尾点避开 search 放大
kubectl run -it --rm dnsprobe --image=registry.example.internal/net-tools:latest \
  --restart=Never -- dig +short api.dns.example.org. A

# 负缓存只留 5 秒：连查两次不存在的名字，比较时间
kubectl run -it --rm dnsprobe --image=registry.example.internal/net-tools:latest \
  --restart=Never -- sh -c 'dig +short no-such.dns.example.org.; sleep 6; dig +short no-such.dns.example.org.'

# 指标与上游健康
kubectl -n kube-system port-forward deploy/coredns 9153:9153 &
curl -s 127.0.0.1:9153/metrics | grep -E 'coredns_(cache|forward)_' | head -20
```

**为什么这么设**（验证命令）：先确认 ConfigMap 已生效与副本就绪，再分别从 `kubernetes` 插件、`forward` 插件、负缓存、指标四条路径核对，任一条不对都能定位到具体插件而不是整份 Corefile。

**怎么验证**（验证命令）：`kubectl -n kube-system rollout status deployment/coredns --timeout=90s` 显示 successfully rolled out；三条 `dig` 分别返回 ClusterIP、公网地址、以及 6 秒后重新发起的查询。

**失败模式 / 回滚点**（验证命令）：`kubectl run -it --rm dnsprobe` 在集群里没有 RBAC 或镜像拉不到时会失败，此时改用已有 Pod 内 `exec` 执行 `dig`；`port-forward` 未建立时 `curl` 会连不上 9153，先确认 CoreDNS 副本就绪。Corefile 本身的失败模式（`rewrite` 写错把集群内名字改写到公网、`cache` 放到 `kubernetes` 之前导致 EndpointSlice 滞后 30s）与回滚点（`kubectl -n kube-system get cm coredns -o yaml > /var/tmp/coredns-cm-before.yaml` 快照 + `rollout restart`）见 20.6 开头的那段；`reload` 插件可热加载 Corefile，但插件增删仍建议走一次滚动重启。

逐项说明这条插件链：

| 插件 | 作用 | 与本书前文的呼应 |
|---|---|---|
| `errors` / `log` | 错误与查询日志 | 对应第 7 章可观测性；生产环境 `log` 要限量，避免 dnstap 式的元数据自留问题 |
| `health` / `ready` | 存活与就绪探针（`:8080` / `:8181`） | 滚动更新时不把未就绪副本挂进 Service |
| `kubernetes` | 唯一的服务发现来源 | 见 20.1，Unbound 不能替代 |
| `prometheus :9153` | 指标导出 | 结合 `coredns_cache_*`、`coredns_forward_*` 看命中率与上游时延 |
| `cache` | `success 9984 30`（成功缓存 30s）、`denial 9984 5`（NXDOMAIN 只缓存 5s） | **负缓存**故意压到 5 秒，对应第 22 章案例 ⑤：上游瞬时 NXDOMAIN 不应在集群里续命 3600s |
| `rewrite` | 把内部别名改写到外部域名，并改写应答里的名字 | 只做名字整形，不做验证 |
| `forward` | 把所有集群外名字交给 Unbound 验证器（教学示例 `192.0.2.53`） | 单个上游、fail-closed：见下文 |

`forward` 这里只给**一个目标地址**（教学示例 `192.0.2.53`；双栈部署由该地址对应的服务同时提供 v4/v6 可达性，不列第二个目标）。`policy sequential` 在只有一个目标时等价于固定目标。这与第 5 章的教训完全一致：给转发器配第二个 `forward-addr`，Unbound 是「并发竞速取最快」而非主备，会把查询送到不该去的解析器，过滤表失效。CoreDNS 的 `forward` 也有多上游与 `policy random` / `round_robin` 等选项，在「上游即策略边界」的场景里同样不该开竞速。要做切换，用外部巡检脚本改 ConfigMap，不要靠多上游。

### stubDomains 与 upstreamNameservers 的历史写法

在 kube-dns 时代，`kube-dns` 的 ConfigMap 支持两个字段：

```yaml
# 历史写法（kube-dns ConfigMap，仅作对照，勿在新集群使用）
apiVersion: v1
kind: ConfigMap
metadata:
  name: kube-dns
  namespace: kube-system
data:
  stubDomains: |
    {"corp.example.internal": ["192.0.2.54"]}
  upstreamNameservers: |
    ["192.0.2.53", "2001:db8::53"]
```

**为什么这么设**（仅作历史对照）：这两个字段是 kube-dns 时代把「特定域走指定解析器」与「其余走上游」塞进一个 ConfigMap 的办法，列出它是为了给出迁移映射，不是建议在新集群使用。

**怎么验证**：`kubectl -n kube-system get cm coredns -o yaml | grep -E 'stubDomains|upstreamNameservers' || true` 应无输出，说明集群已走 CoreDNS 的 server block 写法。

**失败模式 / 回滚点**：迁移时若把 `upstreamNameservers` 的多个地址照抄成 CoreDNS `forward` 的多个目标，会重新引入案例 ① 的竞速；回滚点同样是 `kubectl -n kube-system get cm coredns -o yaml > /var/tmp/coredns-cm-before.yaml` 快照。

它们分别表达「某些域名走指定解析器」和「其余域名走这些上游」。CoreDNS 里没有这两个字段，对应关系是：

| kube-dns 字段 | CoreDNS 对应写法 |
|---|---|
| `stubDomains: {"corp...": ["192.0.2.54"]}` | 另起一个 server block：`corp.example.internal:53 { forward . 192.0.2.54 ; cache ; errors }` |
| `upstreamNameservers: [...]` | 根 server block 里的 `forward . <地址> ...` |
| 两者的优先级 | CoreDNS 按 **server block 的最长后缀匹配**选择，天然实现「更具体的域先命中」 |

```text
# CoreDNS 里 stubDomains 的等价物
corp.example.internal:53 {
    errors
    cache 30
    forward . 192.0.2.54
}
```

**为什么这么设**：用独立的 server block 而不是在根 block 里堆 `rewrite`，可以让 `log` 与 `prometheus` 按域分别统计，也避免 rewrite 规则互相干扰。

**怎么验证**：`kubectl run -it --rm dnsprobe --image=registry.example.internal/net-tools:latest --restart=Never -- dig +short host.corp.example.internal A`，并在 `192.0.2.54` 侧确认收到了查询。

**失败模式 / 回滚点**：server block 的域名必须与 `kubernetes` 插件的集群域不重叠，否则服务发现被遮蔽；写 `corp.example.internal:53` 时不要漏 `:53`。回滚同样是还原 ConfigMap 快照。另一个历史坑：`upstreamNameservers` 里列多个地址会被当成竞速/随机，迁移到 CoreDNS 时**不要**照抄成多个 `forward` 目标，按 20.1 的分工只留一个验证器入口。

## 20.7 三种拓扑与取舍

分工确定之后，Unbound 放在链路的哪一层仍有三个可行位置。下表所有地址都是教学示例。

| 拓扑 | 结构 | 优点 | 代价 / 取舍 |
|---|---|---|---|
| **A. NodeLocal + Unbound 上游** | Pod → NodeLocal(节点本机) → CoreDNS(服务发现) → Unbound(集群内 Service 或集群外) → 权威 | 改动最小，复用现有 DNS Service；conntrack 竞态在节点侧已消除 | Unbound 仍是共享入口，故障域覆盖全集群；DNSSEC 验证集中在少数副本 |
| **B. 每节点 Unbound sidecar** | Pod → 节点级 Unbound(本机 :53) → CoreDNS(仅集群域名，另一 server block) → 加密上游 | 验证/过滤/QMIN/serve-stale 全部节点本地；单点故障域缩到一个节点；可做每节点缓存 | DaemonSet 数量随节点增长，信任锚与配置分发、版本一致性、内存开销都要自己管 |
| **C. 集群外中央验证器 + DoT 上游** | Pod → NodeLocal → CoreDNS → 集群边界的 `cloud-01` Unbound → DoT/DoQ 到外部 → 权威 | 配置集中、审计集中；DoT 上游与第 11、14 章一致；跨集群复用 | 跨节点/跨机房多一跳；链路被 QoS 时 UDP 不稳，需要按第 22 章案例 ⑩ 优先 TCP 承载 |

三者不互斥：常见落地是 **A 起步、C 收口**——先在节点上消除 conntrack 竞态，再把 CoreDNS 的 `forward` 指到集群外那台做验证与 DoT 的 Unbound；B 适合对隐私与故障域有更严要求、且已有配置分发体系的团队。第 21 章的综合实验拓扑本质上是 C 的缩小版。

无论哪一种，落到 Unbound 侧的控制项应与第三篇逐条对齐：

| 控制项 | 放在哪一层 | 要点 |
|---|---|---|
| DNSSEC 验证 | **Unbound**（验证器），或集群外中央验证器 | CoreDNS 不做验证；有信任锚才会对坏签名域返回 SERVFAIL；不要把 `auto-trust-anchor-file` 配在纯转发器上（第 22 章案例 ④） |
| DoT / DoH / DoQ 上游 | Unbound 出向 | 加密传输与 DNSSEC 是两件事，不互相替代；`forward-tls-upstream: yes` 一类配置见第 11 章 |
| 查询名最小化（QMIN） | Unbound 出向 | 别开 `qname-minimisation-strict: yes`，违法 [RFC 8020] 的 CDN 区会整片解析失败，且每层缓存都要刷（第 13、22 章） |
| EDNS 1232 | 全链路 | `edns-buffer-size: 1232`（官方默认，DNS Flag Day 2020）；**出向**靠它，`max-udp-size` 只管入向。节点网络、隧道 MTU 1280 下尤其重要（第 22 章案例 ③） |
| 负缓存 | 每层都要单独设 | CoreDNS `cache { denial ... 5 }`、NodeLocal 的 cache、Unbound `cache-max-negative-ttl` 三层各自有默认值；上游瞬时 NXDOMAIN 会被按 SOA minimum 缓存，**链路上每个实例都要刷**，见第 22 章案例 ⑤ |
| 过期缓存应答（serve-stale） | Unbound | `serve-expired-client-timeout` 的单位是**毫秒**（默认 1800 = 1.8 秒），不是 1800 秒；CoreDNS 的 `cache` 没有等价的 serve-stale 语义，别误以为开了 `cache` 就等于有了抗上游抖动能力 |

特别提醒「每层缓存都要刷」在 Kubernetes 里比裸机更麻烦：一次错误的 NXDOMAIN 可能同时躺在 NodeLocal、中心 CoreDNS、Unbound、以及应用自身的解析器里。刷缓存时要按 `NodeLocal → CoreDNS → Unbound` 的顺序逐层来，只刷最后一层会看到「明明修好了却还在失败」。

```bash
# 逐层刷缓存（教学示例；具体命令以各组件实际启用方式为准）
kubectl -n kube-system rollout restart ds/node-local-dns
kubectl -n kube-system rollout restart deploy/coredns
unbound-control -c /etc/unbound/unbound.conf flush_negative
unbound-control -c /etc/unbound/unbound.conf flush_zone dns.example.org
```

**为什么这么设**：从离客户端最近的一层开始清，避免清完最外层后，近端缓存又把旧答案吐出来。

**怎么验证**：`dig +short no-such.dns.example.org.` 在每一层分别 `@192.0.2.10`（NodeLocal）、`@<CoreDNS-ClusterIP>`、`@192.0.2.53`（Unbound）直到结果一致。

**失败模式 / 回滚点**：`unbound-control` 没启用远程控制时只能重启实例刷缓存（第 22 章案例 ⑤）；`rollout restart` 会让该层短暂降级，滚动窗口内要保证 `serve-expired` 开着。回滚点同上：保留 ConfigMap 与 Unbound 配置快照。

## 20.8 验收清单

| 检查 | 命令 | 通过标准 |
|---|---|---|
| dnsPolicy 生效 | `kubectl exec <pod> -- cat /etc/resolv.conf` | 与 20.3 表格一致；hostNetwork Pod 是 `ClusterFirstWithHostNet` |
| 集群内服务发现 | `dig +short mysvc.default.svc.example.internal A` | 返回 Service ClusterIP |
| 外部名字分流 | `dig +short api.dns.example.org. A` | 由 Unbound 递归得到答案，CoreDNS `forward` 计数上升 |
| search 放大已消除 | 抓 `port 53` 数 QNAME | 带尾点域名只剩 1 个 QNAME |
| 负缓存时长 | CoreDNS `/metrics` 的 `coredns_cache_*` + 两次连查对比 | denial 缓存不超过 5s |
| DNSSEC 在正确的层 | `dig +dnssec badsig.dns.example.org. A` | 返回 SERVFAIL（验证器侧真验证时） |
| EDNS 尺寸 | `dig +bufsize=4096 +dnssec @192.0.2.53 dns.example.org DNSKEY` | 不出现分片丢弃后的 TCP 重试 |
| conntrack 竞态 | `conntrack -S \| grep -i -E 'insert_failed|drop'` | 上 NodeLocal 前后对比，随机 5s 超时消失 |

最后再强调一次选型结论：**别把 Unbound 塞进 `kube-system` 去替换 CoreDNS**。它该出现在 `forward` 的另一端、节点本地的 sidecar 里、或者集群边界那台跑 DoT 的验证器上。集群内服务发现只有一个数据源，就是 API Server；公网递归与验证只有一个负责人，就是 Unbound。第 21 章会把这个分工和第 19 章的网关引导、第 3 篇的加固控制项拼成一台网关加一台云主机的完整实验。

## 本章要点

1. **分工**：CoreDNS 负责集群内服务发现（数据源是 API Server），Unbound 负责外部递归 / DNSSEC 验证 / 过滤 / 加密上游；二者不是替代关系，Unbound 不替换 `kubernetes` 插件。
2. **四种 dnsPolicy**：`ClusterFirst`（默认）、`ClusterFirstWithHostNet`（`hostNetwork: true` 必须显式写）、`Default`（继承节点）、`None`（必须配 `dnsConfig`）；`None` 不是禁用 DNS。
3. **`ndots:5` 把 3 点以内的外部域名放大成最多 4 次查询**；外部调用密集的 Pod 降到 2–3，或统一使用带尾点的绝对域名，同时缩短 `searches`。
4. **NodeLocal DNSCache** 在大规模集群几乎必需：节点本地终止查询以绕开 ClusterIP DNAT 的 conntrack UDP 竞态（随机 5s 超时），并大幅削减中心 DNS 的 QPS；注意 `hostNetwork` 与避免转发环。
5. **Corefile 是一条插件链**，顺序改变语义；`cache` 的 denial TTL 要压到秒级，`forward` 只留一个上游（竞速会破坏策略边界），`kubernetes` 放链首。
6. **第三篇的控制项落在 Unbound 那一层**：DNSSEC 验证、DoT 上游、QMIN（不开 strict）、`edns-buffer-size: 1232`、负缓存与 serve-stale；且**每层缓存都要单独刷**、单独设。

## 参考资料

- Kubernetes 官方文档 *DNS for Services and Pods*：<https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/>
- Kubernetes 官方文档 *Using NodeLocal DNSCache in Kubernetes clusters*：<https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/>
- Kubernetes 官方文档 *Customizing DNS Service*（stubDomains / upstreamNameservers 迁移说明）：<https://kubernetes.io/docs/tasks/administer-cluster/dns-custom-nameservers/>
- CoreDNS 官方站点与插件文档：<https://coredns.io/plugins/>
- CoreDNS `forward` 插件文档：<https://coredns.io/plugins/forward/>
- Kubernetes 官方博客 *CoreDNS for Kubernetes Service Discovery*：<https://kubernetes.io/blog/2018/07/10/coredns-ga-for-kubernetes-service-discovery/>
- Unbound 配置手册：<https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html>
- RFC 8020, *NXDOMAIN: There Really Is Nothing Underneath*：<https://www.rfc-editor.org/rfc/rfc8020>
