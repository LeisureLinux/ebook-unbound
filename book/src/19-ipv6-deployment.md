# IPv6 部署：RA、RDNSS、ULA 与前缀轮换

> 本章出现的 `2001:db8::/32`、`fd00:53::/48`、`192.0.2.0/24` 均为**教学用示例地址**，分属 [RFC 3849]、[RFC 4193] 与 [RFC 5737] 文档段，不对应任何真实环境。主机名 `gw-01`、`cloud-01` 也只作教学代号。

## 19.1 「全屋无污染 DNS」的真正机制

把家里所有终端的 DNS 都干净地引到自己的解析器上，最容易想到的做法是「进光猫管理页面，把 DHCPv6 / DNS 选项改掉」。这条路既脆弱又常常走不通：光猫的固件不一定允许改，改了会在运营商下发配置时被覆盖，而且为了进这个页面去切换光猫的工作模式，会踩到 19.6 节那个代价极高的数据面缺陷。

真正稳定、且与客户端操作系统无关的机制只有一句话：

> **网关自己发 ICMPv6 路由器广告（Router Advertisement，RA），在 RA 的 RDNSS 选项里广告自己的 ULA 解析器地址；光猫里不需要配任何 DNS。**

这就是本书把 `ra-steer` 这类小程序放在网关上的原因。它不是 DHCPv6 服务器，也不碰光猫配置，只做一件事：周期性地在 LAN 侧发出一份内容可控的 RA。RA 是 IPv6 无状态地址自动配置（SLAAC）的原生入口，任何支持 IPv6 的终端——Android、iOS、Windows、macOS、Linux、智能电视——都必须处理它，才能拿到默认路由。既然一定要收，就可以顺带把 DNS 一起交给它。

这里有两层含义值得先分清：

| 层 | 谁负责 | 与 DNS 的关系 |
|---|---|---|
| 数据面 | 光猫（桥接/透传）、网关 | 只转发报文，不解释 DNS |
| 控制面 | 网关上的 RA 发送进程 + 网关上的 Unbound 实例 | 决定终端把查询发给谁 |

「无污染」不是靠在光猫上屏蔽某个端口实现的，而是靠**客户端自己把递归解析器地址填成了网关的 ULA**。第 13 章讲的 DDR 压制、浏览器 canary 域、以及第 9 章的 :53 劫持，都是这条主线之外的补漏手段：前者防止客户端绕过，后者兜住那些不理 RA 的存根解析器。

第 18 章已经把 RA / RDNSS 的协议状态机与 RFC 条文逻辑讲清楚了，本章不再重推导，只从部署视角列一份「报文里必须有、且必须验」的清单。

## 19.2 一份合格 RA 里的六项内容

### 组播与单播

RA 的默认目的地是 `ff02::1`（所有节点组播地址），周期由 `MaxRtrAdvInterval` 控制，典型值 600 秒。这个周期对「刚插上网线的手机」太慢，所以还要支持**单播 RA**：终端启动时发 RS（Router Solicitation）到 `ff02::2`，网关收到后立刻以单播回一份 RA，不必等下一个组播周期。

```text
# ra-steer 侧的时间参数（教学骨架，见 19.7 的脚本）
AdvSendAdvert=yes
MaxRtrAdvInterval=60        # 组播周期压到 60s，缩短新设备收敛时间
MinRtrAdvInterval=20
AdvManagedFlag=no           # 不用 DHCPv6 管地址，纯 SLAAC
AdvOtherConfigFlag=yes      # 允许客户端继续取「其他信息」（含 RDNSS）
```

**为什么这么设**：600 秒的默认周期意味着一台刚回到家的设备可能要等十分钟才拿到正确 DNS；压到 60 秒并配合单播回应 RS，可以把收敛拉到秒级。`AdvOtherConfigFlag=yes` 保证只靠 RA 也能携带 DNS，不依赖 DHCPv6 服务器。

**怎么验证**：`tcpdump -i lan0 -n 'icmp6 && ip6[40] == 134' -vv -c 4`，观察是否同时出现组播 RA 与回应 RS 的单播 RA。

**失败模式 / 回滚点**：周期过短会在电池供电的终端上造成可感知的唤醒开销；若观察到终端耗电异常，把 `MaxRtrAdvInterval` 退回 600，并保留单播回应 RS 作为快速通道。改坏时停掉发送进程即可，网关上原有的 `radvd` 配置备份在 `/etc/ra-steer/ra.conf.bak`。

### SLLAO：源链路层地址选项

RA 里必须带 SLLAO（Source Link-Layer Address Option）。缺了它，客户端无法把「默认路由器」映射成邻居缓存里的一个可达项，表现为能收到 RA、能配上地址，但发不出去包，或者每包都要先做一次邻居发现。

**为什么这么设**：SLLAO 让客户端一次性建立 `网关 IPv6 链路本地地址 → LAN MAC` 的邻居表项，省掉一次 NDP 往返；在 Wi-Fi 上还能减少组播邻居请求。

**怎么验证**：`tcpdump -i lan0 -n 'icmp6 && ip6[40] == 134' -vv | grep -i 'source link-layer'`。

**失败模式 / 回滚点**：SLLAO 里填错 MAC（例如填了 WAN 口而不是 LAN 口）会导致全屋 IPv6 黑洞，且现象很容易被误判成「运营商没给前缀」。核对 `ip -6 neigh show` 里的路由器表项；回滚时把 SLLAO 改为由脚本从 `lan0` 的 `address` 属性读取，不要手写常量。

### hop limit 255：iOS 的硬性要求

[RFC 4861] 要求接收端校验 RA 的 IPv6 Hop Limit 必须是 255，用来保证报文没有跨过路由器。多数 Linux 客户端只是宽松检查，而 iOS 是**强制**的：hop limit 不是 255 的 RA 会被静默丢弃，其他一切都正常，只有 iPhone / iPad 拿不到 DNS。正因为这个差异，故障很容易被归到「iOS 兼容性问题」而拖很久。

```bash
# 发送 RA 前必须确认出站报文的 hoplimit 未被 nftables/ip6tables 改写
ip6tables -t mangle -L -n -v | grep -i hl || true
nft list ruleset | grep -i 'hoplimit' || true
```

**为什么这么设**：任何在 `mangle`/`inet` 表里做 `hoplimit set` 的 QoS 或策略路由规则，都可能把 RA 的 255 改掉；先确认没有这类规则，再谈客户端兼容。

**怎么验证**：`tcpdump -i lan0 -n 'icmp6 && ip6[40] == 134' -vv -c 1` 的输出里 `hlim 255`（有的版本写作 `hop limit 255`）。

**失败模式 / 回滚点**：若抓到 254 或 64，回滚点是 `nft flush chain inet mangle ...` 之前保存的 `nft list ruleset > /etc/nftables.conf.bak`；不要靠重启客户端掩盖，iOS 不会改变这个要求。

### RDNSS：广告自己的 ULA，并用 lifetime=0 压掉上级广告

RDNSS 选项（[RFC 8106]，取代 [RFC 6106]）允许在一个 RA 里携带 1–3 个递归解析器地址。客户端会把**所有路由器**广告的 RDNSS 合并成一张列表，而不是「后到覆盖先到」。所以只发自己的地址是不够的：光猫还在同一个二层域里发 RA，它的 DNS 地址会一直留在列表里，客户端随时可能挑中它。

工程上的做法是让网关的 RA 同时携带两类 RDNSS 选项：

```text
# /etc/ra-steer/ra.conf 片段（教学骨架）
RDNSS fd00:53::1 fd00:53::2 {
    AdvRDNSSLifetime 60;        # 自己的两个 ULA 解析器地址，正常寿命
};
RDNSS fd00:53::9 {
    AdvRDNSSLifetime 0;         # 光猫广告的那个地址：显式撤销
};
```

**为什么这么设**：`Lifetime=0` 的语义是「该地址不再作为 RDNSS 使用」，用来在客户端的合并表里把上级路由器广告的地址标为失效；自己的两个 ULA 地址给 60 秒寿命，随每次 RA 续期。两条都是网关自己的 ULA 实例地址，对应第 7 章的多实例分工。

**怎么验证**：在 Android 上 `adb shell dumpsys connectivity | grep -i -A3 'DnsAddresses'`，期望只列出 `fd00:53::1` 与 `fd00:53::2` 两条；iOS 侧用 `tcpdump` 抓 RA 核对选项内容，或用描述文件下发前的「Wi-Fi → 配置 DNS」页面确认没有第三条。

**失败模式 / 回滚点**：并非所有客户端都接受「由另一台路由器撤销第三方广告的地址」，某些实现只认原广告者的撤销。若验证时仍看到第三条，退路是在网关上阻断来自 WAN 侧的 RA（只放行自己的 RA），而不是去改光猫 DNS；这一步会改变二层拓扑，务必先备份 `nft list ruleset`。另一个坑是撤销必须**持续重复发送**：终端重新联网后会重新接收光猫的 RA，一次性撤销很快失效。

### PIO 与 RIO：/64 和 /48 各管一件事

PIO（Prefix Information Option）告诉客户端「你可以在这个 /64 上配 SLAAC 地址」；RIO（Route Information Option，[RFC 4191]）告诉客户端「去这个前缀要走我」。两者的粒度不能混用，这里有个一天之内栽两次的坑。

Android 内核的 `accept_ra_rt_info_min_plen=48` 会**丢弃前缀长度小于 48 的 RIO**。因此下游路由广告必须写成 /48（或更细），写 /32 之类的聚合会被静默忽略，现象是「路由表里什么都没多」。但前缀变更检测脚本必须盯的是 PIO 里的 **/64**：运营商轮换的是 LAN 的 /64，而 /48 聚合在这个轮换中不变。

第一次栽在 DDNS 更新脚本：脚本从 RIO 里取 /48 拼出记录值，运营商换 /64 后 DDNS 纹丝不动。第二次栽在 RA 通知脚本：同一个 /48 变量被用来判断「前缀是否变化」，于是既不发新 RA 通知，也不触发临时地址重建。两处故障表现完全不同，根因是同一个。

```bash
# 只从 PIO 取 LAN /64，绝不从 RIO 推导
rdisc6 -1 lan0 | grep -i 'Prefix' || true
ip -6 route show | grep '^2001:db8' || true
```

**为什么这么设**：把「路由聚合粒度」和「地址变更粒度」拆成两个独立数据源，避免一个变量被两处脚本复用而同时失效。

**怎么验证**：`ip -6 addr show dev lan0 scope global`，确认地址来自当前下发的 /64；轮换后该命令输出的 /64 必须变化，而 RIO 生成的 /48 路由保持不变即为正确。

**失败模式 / 回滚点**：若脚本已经把 /48 写进 DDNS，先手工更新一次记录再回滚 DDNS 脚本变量；不要靠清缓存解决，负缓存会按 SOA minimum 续命（第 22 章案例 ⑤）。

## 19.3 ULA 做 DNS 地址，GUA 做上网地址

家宽下发的 GUA（Global Unicast Address）前缀会轮换，教学示例里从 `2001:db8:1:1::/64` 变成 `2001:db8:1:9::/64` 是常态。若把 RDNSS 里的解析器地址写成 GUA，那么每次轮换：

1. 所有客户端手里的 DNS 地址失效；
2. 新 RA 到达前的一段时间里查询全部超时；
3. 即便新 RA 到达，负缓存和应用的连接池还可能继续用旧地址（见第 22 章案例 ⑤）。

ULA（`fd00:53::/48` 这一段）由网关自己生成，[RFC 4193] 要求用随机 40 位全局 ID 生成 /48，一旦选定就不随运营商变化。它不用于访问公网，只用于「在自家二层域里稳定地寻址自己的服务」——DNS 正好是这种服务。

```text
# 网关 LAN 口的地址分工（教学示例）
lan0: fd00:53::1/64        # RDNSS 第一条，永久
lan0: fd00:53::2/64        # RDNSS 第二条（第二个 Unbound 实例），永久
lan0: 2001:db8:1:1::1/64  # 由运营商下发的 GUA，会轮换，不作 DNS 地址
```

**为什么这么设**：把「会变的」和「不能变的」彻底分开，DNS 引导路径上不出现任何依赖运营商下发状态的常量。

**怎么验证**：`dig @fd00:53::1 +short signed.dns.example.org A` 应成功，且 `ip -6 route get fd00:53::1` 显示直连 LAN，不经过默认路由。

**失败模式 / 回滚点**：ULA 不是公网可路由地址，若某台终端把 `fd00:53::1` 当成公网目标往隧道里送，会立刻超时；回滚点在 nftables 的策略路由规则里保留一条「目的 ULA → 直连 LAN」的豁免。生成 ULA 时不要图省事写全零全局 ID，按 [RFC 4193] 随机生成，避免以后合并网络时撞前缀。

## 19.4 前缀轮换 × 临时地址寿命

[RFC 4941] 隐私扩展生成的临时地址，在 Linux 上默认 `temp_valid_lft` 是 **48 小时**（172800 秒），`temp_prefered_lft` 是 24 小时。运营商换前缀之后，旧前缀上的临时地址依然有长达两天的有效寿命，而 [RFC 6724] 的地址选择规则**优先选临时地址作为源地址**。结果是一个特别难查的现象：

> 客户端拿到了新前缀、配了新地址、默认路由也正确，但出站连接的源地址仍是旧前缀 → 上游不路由回包 → 「IPv6 全断，ping6 网关却通」。

把寿命压到 30 分钟，让旧临时地址在轮换后尽快退场：

```bash
# /etc/sysctl.d/90-ipv6-privacy.conf
net.ipv6.conf.all.use_tempaddr      = 2
net.ipv6.conf.default.use_tempaddr  = 2
net.ipv6.conf.all.temp_valid_lft    = 1800
net.ipv6.conf.default.temp_valid_lft= 1800
net.ipv6.conf.all.temp_prefered_lft = 600
net.ipv6.conf.default.temp_prefered_lft = 600
```

**为什么这么设**：`use_tempaddr = 2` 表示「生成并优先使用临时地址」；`temp_valid_lft = 1800` 把旧前缀的最长残留时间从 48 小时压到 30 分钟，把一次前缀轮换的故障窗口收进可接受范围。

**怎么验证**：`sysctl net.ipv6.conf.lan0.temp_valid_lft` 必须返回 `1800`；只查 `all.*` 不算数。

**失败模式 / 回滚点**：这份文件只对**之后新建的接口**生效。已存在的接口不会被 `all.*` 回溯改写，NetworkManager 还会在连接激活时把 `use_tempaddr` 写成 `-1`（继承内核默认）覆盖你的值。发现不生效时按 19.7 的三处补齐；回滚只需删除该文件并 `sysctl --system`，但注意删除后旧接口的值仍留在运行时，要显式写回。

三处都要设，这是 Linux sysctl 的一个反直觉边界：

| 写入位置 | 作用范围 | 只写它会怎样 |
|---|---|---|
| `all.*` | 全局「上限/聚合」视图 | **对已存在的接口无效**，最典型的假成功 |
| `default.*` | 之后新建的接口 | 当前 LAN 接口不受影响 |
| `<iface>.*` | 这个具体接口 | 只写它，新接口（USB 网卡、热点）又漏掉 |

```bash
# 对已存在接口补写，幂等，可重复执行
IFACE=lan0
for k in use_tempaddr temp_valid_lft temp_prefered_lft; do
  v=$(awk -v k="$k" '$0 ~ "net.ipv6.conf.all."k {print $3}' /etc/sysctl.d/90-ipv6-privacy.conf)
  sysctl -w "net.ipv6.conf.${IFACE}.${k}=${v}"
done
```

**为什么这么设**：把「配置文件的意图」逐项投放到运行时已存在的接口上，绕开 `all.*` 不回溯的限制；写成循环而不是硬编码，避免以后改了文件却忘了改命令。

**怎么验证**：`sysctl -a | grep 'net.ipv6.conf.lan0.temp'` 三行都应为 `2 / 1800 / 600`。

**失败模式 / 回滚点**：NetworkManager 连接激活会再次覆盖接口级值，长期方案是给连接加 `ipv6.ip6-privacy` 或在 `NetworkManager.conf` 里禁用该连接的 sysctl 改写。回滚时把接口级值写回内核默认，并保留一份 `sysctl -a > /var/tmp/sysctl-before.txt` 快照。

### STALE PIO 为什么清不掉

一个自然的想法是：轮换时发一个 `Valid Lifetime = 0` 的 PIO，让客户端立刻丢掉旧前缀地址。在 Linux 上这**不生效**。[RFC 4862] 第 5.5.3 节为抵御伪造 RA 的拒绝服务攻击，规定对仍剩较长有效期的地址不允许被一个短寿命（或零寿命）的 PIO 立刻缩短——只能把有效期夹到 2 小时量级，具体夹取规则见该节。于是不管怎么发「撤销」，旧地址都会赖着不走，这就是所谓 STALE PIO 清理无效。

真正有效的只有两条路：**等**（靠上面把 `temp_valid_lft` 压短），以及**在客户端侧主动删地址**（由前缀变更检测脚本调用 `ip -6 addr flush ... scope global dynamic` 一类的动作，且要避开 ULA）。不要指望 RA 单方面完成清理。

## 19.5 光猫只当管道用：一次数据面缺陷的教训

为了进管理页面改 DNS，实验里曾把光猫从桥接模式改回路由模式。配置页面确实进去了，IPv6 地址也照常下发，随后出现一个非常干净、也非常迷惑的故障分层：

| 测试 | 结果 | 说明 |
|---|---|---|
| ICMPv6 ping 通外网文档地址 | 通 | 控制面看起来完好 |
| TCP 三次握手 | 通 | `connect()` 成功，SYN/SYN-ACK/ACK 完整 |
| TCP 数据阶段（首个有载荷的段） | **静默丢包** | 无 RST、无 ICMPv6 报错，连接挂住直到超时 |
| 换回桥接模式 | 全部恢复 | 与 DNS、RA、Unbound 配置无关 |

抓包看到的现象是握手包很小所以能通过，一旦出现接近 MTU 的载荷就再也收不到 ACK，重传也无回应；换成小包手动分片又能零星走一点。这是光猫在路由模式下某个 IPv6 数据面路径（分段/重组或硬件加速表项）的缺陷，不是 DNS 配置错误，也不是 MTU 设错——同一条链路在桥接模式下完全一致。

代价是：一个本来只想改 DNS 的动作，把整条 IPv6 数据面换成了不可信的实现，排查方向还被「ping 通 / 握手通」误导了好几个小时。由此定下一条部署纪律：

> **光猫只当管道用。** 保持桥接/透传，所有三层及以上能力（RA、DNS、分流、加密隧道、防火墙）都放在自己可控的网关 `gw-01` 上；任何需要「进光猫改点什么」的方案，先问一句能不能用网关侧发 RA 替代。

这条纪律与 19.1 是同一个结论的两面：RDNSS 方案之所以值得写透，正是因为它让「改光猫」这个动作变得完全不必要。第 14 章的加密通道选路、第 21 章的拓扑拼装，都建立在这个前提上。

如果已经改了工作模式且暂时无法改回，不要试图在光猫里调 MTU 兜底：把 IPv6 出向流量整体绕过它（例如只走加密隧道承载的 IPv4 通道）是更可控的临时方案，同时把隧道 MTU 与 `edns-buffer-size: 1232` 一起核对（见第 22 章案例 ③ 与第 13 章）。

## 19.6 可复制的 sysctl 与 RA 引导脚本骨架

下面是一份**骨架**，不是开箱即用的发行版包：它把需要显式决策的量都留成变量，避免把某个环境的接口名、ULA、上游写死。配置块只给引导层，Unbound 实例本身的配置见第 5、7 章。

```bash
#!/bin/sh
# /usr/local/sbin/ra-steer-boot.sh —— 教学骨架
set -eu
IFACE="${IFACE:-lan0}"
ULA_DNS1="${ULA_DNS1:-fd00:53::1}"
ULA_DNS2="${ULA_DNS2:-fd00:53::2}"
UPSTREAM_DNS="${UPSTREAM_DNS:-fd00:53::9}"   # 需要撤销的上级广告地址
CONF=/etc/ra-steer/ra.conf
BAK=/etc/ra-steer/ra.conf.bak

# 1) 快照，回滚点
sysctl -a > /var/tmp/sysctl-before-ra-steer.txt
[ -f "$CONF" ] && cp -a "$CONF" "$BAK"

# 2) 三处 sysctl：全局 / 默认 / 接口级
sysctl -w net.ipv6.conf.all.accept_ra=2
sysctl -w net.ipv6.conf.default.accept_ra=2
sysctl -w "net.ipv6.conf.${IFACE}.accept_ra=2"
for k in use_tempaddr:2 temp_valid_lft:1800 temp_prefered_lft:600; do
  key=${k%%:*}; val=${k##*:}
  sysctl -w "net.ipv6.conf.all.${key}=${val}"
  sysctl -w "net.ipv6.conf.default.${key}=${val}"
  sysctl -w "net.ipv6.conf.${IFACE}.${key}=${val}"
done

# 3) 生成 RA 配置：自己的 ULA 正常寿命 + 上级地址 lifetime=0
cat > "$CONF" <<EOF
interface ${IFACE} {
  AdvSendAdvert on;
  MaxRtrAdvInterval 60;
  MinRtrAdvInterval 20;
  AdvDefaultLifetime 180;
  AdvSourceLLAddress on;
  AdvManagedFlag off;
  AdvOtherConfigFlag on;
  prefix 2001:db8:1:1::/64 { AdvAutonomous on; AdvValidLifetime 3600; };
  route 2001:db8:1::/48 { AdvRouteLifetime 3600; };
  RDNSS ${ULA_DNS1} ${ULA_DNS2} { AdvRDNSSLifetime 60; };
  RDNSS ${UPSTREAM_DNS} { AdvRDNSSLifetime 0; };
};
EOF

# 4) 交给 RA 发送器；hop limit 由内核置 255，禁止任何规则改写
exec /usr/local/sbin/ra-steer --conf "$CONF" --iface "$IFACE" --hoplimit 255
```

**为什么这么设**：`accept_ra=2` 是网关开启 IPv6 转发时仍接收 RA 的必要条件；PIO 用 /64、RIO 用 /48，落实 19.2 的粒度分离；`; AdvSourceLLAddress on` 对应 SLLAO；`--hoplimit 255` 对应 iOS 的硬性要求；第 1 步先做快照，保证任何一步失败都有可退回的状态。

**怎么验证**：`tcpdump -i lan0 -n -vv 'icmp6 && ip6[40] == 134' -c 1` 应同时看到 `hlim 255`、SLLAO、/64 PIO、/48 RIO、一条正常 RDNSS 与一条 lifetime 0 的 RDNSS。

**失败模式 / 回滚点**：`prefix` 行写死的教学 /64 必须替换成运行时从上游下发的真实值，否则轮换后会把旧前缀继续广告出去（正是 19.2 那个坑）；生产脚本应从 `dhcpcd`/`dhclient` 的钩子读入变量。回滚顺序：停掉 `ra-steer` → 用 `$BAK` 覆盖 `$CONF` → `sysctl --system` 加上 `/var/tmp/sysctl-before-ra-steer.txt` 逐项对照 → 让光猫原有的 RA 重新填满客户端列表。若担心操作失误把自己锁在外面，用 `systemd-run --on-active=10m systemctl stop ra-steer` 设一个十分钟的自动回滚定时器，确认无误后再取消。

## 19.7 验证命令与验收清单

引导脚本跑起来之后，按「链路 → 报文 → 客户端 → 解析器」四层验，不要跳层。

| 层 | 命令 | 通过标准 |
|---|---|---|
| 链路 | `ip -6 addr show dev lan0 scope global` | 有 ULA `fd00:53::1`，GUA 为当前下发的 /64 |
| 报文 | `tcpdump -i lan0 -n -vv 'icmp6 && ip6[40] == 134' -c 2` | `hlim 255`、SLLAO、PIO /64、RIO /48 齐全 |
| 报文 | `rdisc6 -1 lan0` | 能看到两条 RDNSS，且撤销条目的 lifetime 为 0 |
| 客户端（Android） | `adb shell dumpsys connectivity \| grep -i -A3 DnsAddresses` | **只显示自己的两条** ULA 地址 |
| 客户端（Linux） | `resolvectl dns lan0` 或 `cat /etc/resolv.conf` | 同上，无第三条 |
| 客户端（iOS） | 抓 RA 核对；描述文件未接管时查看 Wi-Fi 的 DNS 栏 | 无光猫下发的 DNS |
| 解析器 | `dig @fd00:53::1 +dnssec signed.dns.example.org A` | 返回答案且 `flags:` 含 `ad`（第 10 章） |
| 解析器 | `dig @fd00:53::2 +short +dnssec signed.dns.example.org A` | 第二个实例同样可用（第 7 章） |

关于「手机 DNS 应只显示自己的两条」需要说清期望从哪来：`fd00:53::1` 与 `fd00:53::2` 是网关上两个 Unbound 实例的 ULA 地址。如果只部署了一个实例，列表里就只该有一条；出现第三条（尤其是光猫的链路本地地址或一个 GUA 地址），说明 RDNSS 撤销没被接受、或者光猫的 RA 没被隔离，回到 19.2 的退路。

还要验一次**轮换演练**，否则前面所有配置都只是静态正确：

```bash
# 模拟一次 /64 轮换（教学：手工触发，不改光猫）
ip -6 addr flush dev lan0 scope global dynamic
ip -6 addr add 2001:db8:1:9::1/64 dev lan0
/usr/local/sbin/ra-steer-boot.sh >/var/tmp/ra-steer-reload.log 2>&1
sleep 35
ip -6 addr show dev lan0 scope global | grep -c '2001:db8:1:1' || true   # 期望 0
```

**为什么这么设**：静态验证查不出「旧临时地址赖着不走」和「DDNS 用 /48 推不出变化」这两类问题，必须让前缀真的变一次。35 秒略大于 `temp_valid_lft` 之外的观察窗，用来确认旧地址没有被重新生成。

**怎么验证**：`ip -6 addr show dev lan0 scope global` 里不再出现旧 /64；`adb shell dumpsys connectivity | grep -i -A3 DnsAddresses` 仍是那两条 ULA。

**失败模式 / 回滚点**：`flush` 会把 ULA 之外的动态地址一并清掉，若误加到 `fd00:53::1` 会立刻让全屋 DNS 失联；执行前确认 `grep -q 'fd00:53::1' /var/tmp/sysctl-before-ra-steer.txt` 之类的快照存在，回滚用 `ip -6 addr add fd00:53::1/64 dev lan0` 先救回 DNS，再谈其余。

## 19.8 回滚点汇总

| 变更 | 快照 | 回滚动作 | 影响面 |
|---|---|---|---|
| RA 发送进程 | `/etc/ra-steer/ra.conf.bak` | 停进程 + 还原配置 + 让光猫 RA 恢复 | 客户端 DNS 列表回到改动前 |
| sysctl | `/var/tmp/sysctl-before-ra-steer.txt` | `sysctl --system` 并逐项写回接口级值 | 临时地址寿命恢复 48h |
| GUA / ULA 地址 | `ip -6 addr show > /var/tmp/addr-before.txt` | `ip -6 addr add` 逐条补回 | 全屋 DNS 可达性 |
| nftables（若做了 RA 隔离） | `nft list ruleset > /etc/nftables.conf.bak` | `nft -f /etc/nftables.conf.bak` | 二层 RA 来源 |
| 光猫工作模式 | 改动前拍照记录 | 改回桥接/透传 | 整个 IPv6 数据面 |

三点纪律：其一，回滚顺序**先救 DNS 地址，再救路由，最后救策略**，因为所有远程排障手段都依赖 DNS；其二，改动前设十分钟自动回滚定时器，防止配置错误把自己关在门外；其三，遇到任何「看起来像 DNS、又不太像」的故障，按第 22 章那条经验——**先看 nftables，再怀疑别的**。

## 本章要点

1. 「全屋无污染 DNS」的机制是网关自己发 RA 广告 RDNSS（ULA 地址），**不需要去光猫里配任何 DNS**；光猫只当管道用。
2. 一份合格 RA 要有：组播 + 单播回应 RS、SLLAO、**hop limit 255**（iOS 强制）、自己的 ULA RDNSS、对上级广告地址的 `lifetime=0` 撤销；PIO 用 /64、RIO 用 /48，两种粒度不能互推。
3. ULA 永久不变适合作 DNS 地址，GUA /64 会轮换绝不能写进 RDNSS；前缀变更检测必须盯 /64，否则 DDNS 与 RA 通知会同一天各栽一次。
4. Linux 上 `temp_valid_lft` 默认 48h，前缀轮换后旧临时地址仍被 [RFC 6724] 优先选作源地址；改 30min 且 `all.*` / `default.*` / 接口级**三处都要设**，NetworkManager 会把 `use_tempaddr` 写成 `-1`。
5. 发零寿命 PIO 清不掉旧地址：[RFC 4862] 第 5.5.3 节不允许把已存在的长寿命地址立刻缩短，只能靠短寿命 + 客户端侧主动清理。
6. 为改 DNS 去动光猫工作模式，可能踩到「ICMP 通、TCP 握手通、数据阶段静默丢包」的数据面缺陷；这是把可控性交出去的代价。

## 参考资料

- RFC 4861, *Neighbor Discovery for IP version 6*（SLLAO、hop limit 255）：<https://www.rfc-editor.org/rfc/rfc4861>
- RFC 4862, *IPv6 Stateless Address Autoconfiguration*（第 5.5.3 节地址寿命）：<https://www.rfc-editor.org/rfc/rfc4862>
- RFC 8106, *IPv6 Router Advertisement Options for DNS Configuration*（取代 [RFC 6106]）：<https://www.rfc-editor.org/rfc/rfc8106>
- RFC 4191, *Default Router Preferences and More-Specific Routes*（RIO）：<https://www.rfc-editor.org/rfc/rfc4191>
- RFC 4941, *Privacy Extensions for SLAAC*（临时地址；另见 [RFC 6724] 源地址选择、[RFC 4193] ULA）：<https://www.rfc-editor.org/rfc/rfc4941>
- RFC 6724, *Default Address Selection for IPv6*：<https://www.rfc-editor.org/rfc/rfc6724>
- Linux 内核 `ip-sysctl` 文档（`temp_valid_lft`、`accept_ra_rt_info_min_plen`）：<https://docs.kernel.org/networking/ip-sysctl.html>
- Unbound 配置手册：<https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html>
