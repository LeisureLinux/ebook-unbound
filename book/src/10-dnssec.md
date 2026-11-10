# DNSSEC 验证：信任锚、放行与拒绝

一台递归解析器把 DNSSEC 打开，最容易产生的错觉是「配置里写了 `validator`，所以我已经安全了」。实际生效与否取决于三件事：`module-config` 里有没有 `validator`、信任锚（trust anchor）文件在不在且是否可写、以及验证失败时策略是**放行**还是**拒绝**。三者任一缺位，应答就可能被原样透传给客户端，而配置文件看起来毫无问题。


## 信任锚不是一段静态配置

验证器判断一个应答是 Secure 还是 Bogus，需要从一个它无条件信任的公钥出发逐级验签。这个起点就是信任锚。对绝大多数部署来说，它只有一个：根区的 KSK，以一条 DS 或 DNSKEY 记录的形式存在。

关键不在于「有没有这条记录」，而在于**它是不是一个会随时间演化的状态**。根 KSK 会轮换，密钥会撤销，[RFC 5011] 定义的就是验证器如何在无人值守的情况下跟踪这套变化。因此 Unbound 把信任锚分成两类配置：

| 配置项 | 语义 | 是否自动滚动 | 适用场景 |
|---|---|---|---|
| `trust-anchor-file` | 静态可信密钥文件（DS / DNSKEY，zone file 格式） | 否 | 固定的内部锚、实验室里手动维护的锚 |
| `trust-anchor: "<RR>"` | 直接在配置里写一条 DS / DNSKEY | 否 | 单个测试锚 |
| `auto-trust-anchor-file` | 由 [RFC 5011] 探针跟踪并**回写**的锚文件 | 是 | 根锚，生产环境默认选择 |
| `trusted-keys-file` | BIND 9 风格 `trusted-keys { ... };` 格式 | 否 | 从 BIND 迁移时的兼容入口 |

`auto-trust-anchor-file` 的「auto」落在两处：一是 Unbound 定期（官方手册写的是每月数次）向锚所在区发 [RFC 5011] 探针，观察新密钥并更新状态；二是**它会写文件**——文件变化时要创建临时文件再原子替换。这带来三条容易被忽略的运行条件：① Unbound 运行用户（通常 `unbound`）对**锚文件本身**有写权限；② 对**锚文件所在目录**也有写权限（回写要创建临时文件）；③ 若启用 `chroot`，路径必须落在 chroot 内且目录同样可写（第 4 章讲过 chroot 会重写所有相对路径的解释基准）。只要有一条不满足，实例仍能启动、仍能解析，但锚再也不会更新——在根 KSK 轮换到来之前完全不可见，是典型的「平时全绿、某天全挂」。

## 用 `unbound-anchor` 初始化根锚

`unbound-anchor(8)` 是官方提供的根信任锚引导工具，用来在 Unbound 启动前创建或更新锚文件。它带有内置的初始根锚与根更新证书；文件不存在或为空时写入内置锚，随后尝试通过 DNS 做 [RFC 5011] 跟踪，跟踪失败时再按 [RFC 7958] 从 IANA 获取 `root-anchors.xml` 并用证书验签后更新。网络不通或证书过期时它**不改动原文件**，让解析器照常能起来——这个「宁可保持旧状态也不盲写」的行为是刻意设计。

推荐的初始化顺序（以 Debian/Ubuntu 常见路径为例，其他发行版路径见官方手册或打包说明）：

```bash
# 1. 建目录并给 Unbound 运行用户可写权限（文件 + 目录都要）
install -d -o unbound -g unbound -m 0755 /var/lib/unbound

# 2. 以 Unbound 运行用户身份生成 / 更新根锚
sudo -u unbound unbound-anchor -a /var/lib/unbound/root.key

# 3. 查看锚内容：应能看到状态标记（VALID / VALIDATING 等）
cat /var/lib/unbound/root.key

# 4. 确认属主与权限
stat -c '%U:%G %a %n' /var/lib/unbound /var/lib/unbound/root.key
```

**为什么这么设**：`sudo -u unbound` 不是形式主义——回写由 Unbound 进程自己完成，锚文件属主是 `root` 时只能在内存里跟踪滚动，重启就丢回旧锚。退出码也值得注意：官方手册写明**用证书更新成功或写入内置锚时退出 1**，无需更新、通过 [RFC 5011] 更新或出错时退出 0；不要写成 `unbound-anchor ... && systemctl start unbound`，第一次初始化反而会挡住启动。

**怎么验证**：`unbound-anchor -l | head -20` 打印内置锚与证书（不改动文件），用于核对初始锚来源；再跑 `sudo -u unbound test -w /var/lib/unbound/root.key && echo "anchor writable"` 与 `sudo -u unbound test -w /var/lib/unbound && echo "anchor dir writable"`，两条必须都输出，只测文件不测目录是常见漏项。

**失败模式 / 回滚点**：若出网受限拿不到 `root-anchors.xml`，工具通常静默退出并保留内置锚，用 `cat` 确认文件非空即可，不要误以为失败。目录可写而文件是 `root:root 0644` 时改属主后重启。回滚：停实例、移走 `root.key`、重跑 `unbound-anchor` 写回内置锚；这会让 [RFC 5011] 状态从零开始、**滚动观察期需重新累积**，不要在轮换窗口内轻易做。

## 完整验证器配置

下面是本章的基线配置。它假定这是一台**自己做完整递归**的解析器（第 4、6 章的角色），不是转发器。

```yaml
# /etc/unbound/unbound.conf.d/10-dnssec.conf
server:
    interface: 127.0.0.1@5353
    interface: 192.0.2.1@5353
    access-control: 127.0.0.0/8 allow
    access-control: 192.0.2.0/24 allow
    username: unbound
    chroot: ""
    do-ip4: yes
    do-ip6: no
    do-udp: yes
    do-tcp: yes
    edns-buffer-size: 1232
    max-udp-size: 1232

    # 模块链：validator 必须在 iterator 之前
    module-config: "validator iterator"

    # 根信任锚：由 RFC 5011 探针跟踪，路径必须可写且在 chroot 内
    auto-trust-anchor-file: "/var/lib/unbound/root.key"

    # 真验证（默认 no）：Bogus → SERVFAIL，Secure → 置 AD 位
    val-permissive-mode: no

    # 验证失败留痕：1 = 每个失败查询一行；2 = 附原因与来源服务器
    val-log-level: 1
    val-log-squelch: no

    # 有锚时要求 DNSSEC 数据存在，缺数据即判 Bogus（默认 yes）
    harden-dnssec-stripped: yes

    # 只依据 DNSSEC-secure 的 NXDOMAIN 做子树剪枝
    harden-below-nxdomain: yes

    # 与 DNSSEC 无关：只信任权威范围内的 glue
    harden-glue: yes

    # 附加段清理：只删除 secure 应答中签名不正确的附加数据
    val-clean-additional: yes

    # 对客户端 CD 位不放松：不接受「请别替我验证」的请求
    ignore-cd-flag: yes
```

**为什么这么设**：`module-config: "validator iterator"` 是验证器的开关本身，官方手册明确写「只写 `iterator` 就是非验证服务器」，且**顺序有意义**，绝大多数模块要排在链首。`auto-trust-anchor-file` 提供起点，没有它，即使 `validator` 在链里也退化成透传。`val-permissive-mode: no` 是拒绝策略；`val-log-level: 1` 让每次 Bogus 留一行日志，否则验证失败只表现为客户端看到 SERVFAIL，排查没有抓手。`edns-buffer-size: 1232` 沿用第 4 章结论：DNSSEC 应答比普通应答大，出向设 1472 会在 MTU 1280（WireGuard / IPv6 最小 MTU）与 PPPoE 1492 上分片，分片被丢后表现为「验证偶发超时」「大应答域名随机 SERVFAIL」；`max-udp-size` 只管入向，出向要靠 `edns-buffer-size`。其余 `harden-*` 与 `val-clean-additional`、`ignore-cd-flag` 的边界在 10.6 与 10.7 逐一拆开，这里先按基线全开。

**怎么验证**：

```bash
unbound-checkconf /etc/unbound/unbound.conf
for o in module-config val-permissive-mode harden-dnssec-stripped; do unbound-control -c /etc/unbound/unbound.conf get_option "$o"; done
```

第一条应无错误；循环依次输出 `validator iterator`、`no`、`yes`。`get_option` 读的是**生效值**（含 include 拼接与 `set_option` 运行时修改），比翻配置文件可靠；某些构建不带它，此时以 `stats_noreset` 与配置文件为准，不要凭「配了就生效」下结论。

**失败模式 / 回滚点**：最常见的启动失败是 `chroot` 开着但锚路径没搬进 chroot、或文件属主是 `root`；日志里出现锚文件读写失败，同时 `module-config` 仍有 `validator`——进入「有验证器、无锚」的透传状态，比完全不验证更迷惑。回滚：恢复 `chroot: ""` 与可写路径、改正属主后 restart（不是 reload，模块链与 interface 变更都需 restart）。

## 验证器的三态

同一份 `validator` 模块链，在不同配置下有三种截然不同的对外行为。这是本章最需要记牢的一张表。

| 状态 | 判定条件 | 坏签名域的应答 | 好签名域的应答 | 日志 | 是否提供保护 |
|---|---|---|---|---|---|
| **透传不验证** | 无信任锚（或 `module-config` 只写 `iterator`） | 返回答案，无 AD | 返回答案，无 AD | 无验证失败记录 | 否 |
| **permissive** | 有锚，`val-permissive-mode: yes` | 返回答案（Bogus 被标成 Indeterminate），不置 AD | 返回答案并置 AD | 按 `val-log-level` 记录 | 否，仅观测 |
| **真验证** | 有锚，`val-permissive-mode: no` | **SERVFAIL** | 返回答案并置 **AD** | 按 `val-log-level` 记录 | 是 |

三态里最危险的是第一种，因为它「什么错都不报」：客户端拿到答案、监控看到成功率 100%、日志干净，而伪造数据照样进缓存。第二种是官方手册明确写出的折中——安全检查照做，但 Bogus 结果**不向客户端扣留**，仍收到坏数据，只是记成 Indeterminate 并写日志；适合上线前的灰度观测，绝不能当最终状态。**AD 位只由真正完成验证且结果 Secure 的那台服务器设置**（第 3 章）：DO 位只表示「请把 DNSSEC 相关记录一起给我」，CD 位只表示「请别替我验证」，两者都不是服务器的开关。`ignore-cd-flag: yes` 让本机不接受客户端的「请别验证」，把策略留在自己手里；开放给所有客户端等于提供一条绕过验证的通道。

### 三态之间的切换方法

切换不需要改文件，可以用 `unbound-control set_option` 临时调整（**不写回配置文件、不清缓存**），适合验收时快速对照：

```bash
# 切到 permissive（仅用于观测，验收完必须切回）
unbound-control -c /etc/unbound/unbound.conf set_option val-permissive-mode: yes
unbound-control -c /etc/unbound/unbound.conf get_option val-permissive-mode

# 切回真验证
unbound-control -c /etc/unbound/unbound.conf set_option val-permissive-mode: no
unbound-control -c /etc/unbound/unbound.conf get_option val-permissive-mode
```

**为什么这么设**：`set_option` 能在「同一份缓存、同一台实例、同一时刻」下对比两种策略，排除缓存命中的干扰；但官方手册提醒并非所有选项都支持运行时修改，且新值不写回配置文件，临时切换后必须 `restart` 让盘上值重新生效。

**怎么验证**：`get_option` 读当前生效值；`unbound-control stats_noreset | grep num.rrset.bogus` 的计数随坏签名查询增长，说明验证器确实在做检查——与最终放行/拒绝是两件事，permissive 模式下它同样会涨。

**失败模式 / 回滚点**：忘记切回 `no` 是最大风险，务必把 `set_option val-permissive-mode: no` 作为验收脚本固定收尾并在巡检里用 `get_option` 断言。回滚：`systemctl restart unbound`。

## 验证器行为验收表

四类样本：**好签名**＝正常签署且信任链完整（Secure）；**坏签名**＝故意破坏签名或密钥不匹配（Bogus）；**无签名（Insecure）**＝区未签署且父区有可验证的「未签名」证明，正常返回；**被剥离签名**＝本应签名，应答里的 DNSSEC 相关记录被路径上的中间设备或攻击者删掉。教学环境不要去公网找坏签名域，用 `example.net` / `example.org` 下的四个子域作**本地测试域名占位符**，在实验用的权威服务器（第 21 章的 `cloud-01`，示例地址 `192.0.2.53`）上分别配成四类；也可用 `domain-insecure` 与本地 `local-zone` 模拟无签名分支。`@127.0.0.1 -p 5353` 是本章验证器实例：

```bash
# 统一前置：确认实例处于「真验证」态
unbound-control -c /etc/unbound/unbound.conf get_option module-config
unbound-control -c /etc/unbound/unbound.conf get_option val-permissive-mode
```

| 类别 | 测试域名（占位符） | `dig` 命令 | 真验证下的预期 | permissive 下的预期 | 无锚下的预期 |
|---|---|---|---|---|---|
| 好签名 | `signed.example.net` | `dig @127.0.0.1 -p 5353 signed.example.net A +dnssec` | `status: NOERROR`，`flags` 含 **`ad`**，返回 A | 同左（AD 照置） | `NOERROR`，**无 `ad`**，返回 A |
| 坏签名 | `bogus.example.net` | `dig @127.0.0.1 -p 5353 bogus.example.net A +dnssec` | **`status: SERVFAIL`**，无答案 | `NOERROR` 返回坏数据，**无 `ad`**，日志记 Bogus | `NOERROR` 返回坏数据，无 `ad`，日志无记录 |
| 无签名 | `unsigned.example.org` | `dig @127.0.0.1 -p 5353 unsigned.example.org A +dnssec` | `status: NOERROR`，返回 A，**无 `ad`** | 同左 | 同左（三者一致） |
| 被剥离签名 | `stripped.example.net`（本应签名） | `dig @127.0.0.1 -p 5353 stripped.example.net A +dnssec` | **`status: SERVFAIL`**（`harden-dnssec-stripped: yes` 判 Bogus） | `NOERROR` 返回答案，无 `ad`，日志记异常 | `NOERROR` 返回答案，无 `ad`，日志无记录 |

读表四个要点：① **无签名一类三态一致**——Insecure 不等于 Bogus，把「没有 `ad`」直接当故障是误判。② **只有坏签名与被剥离签名能区分三态**：好签名能区分「有没有做验证」（看 AD），区分不了「permissive 还是真验证」；四行缺一不可。③ `+dnssec` 只请求对端把 DNSSEC 相关记录一并给出，不是「请替我验证」；客户端不置 DO，验证器仍会自己向权威侧取所需记录。④ **AD 位要在本机这一跳看**：若前面有第 12 章的 DoH 前端或第 14 章的多级链，中间设备可能搬运或重写标志位，验收命令必须打在真正做验证的那台实例上。

配套的日志与计数器核对：

```bash
# 每次坏签名 / 剥离签名查询后，日志应新增一行
journalctl -u unbound --since '5 min ago' | grep -i 'validation failure\|bogus' | tail -20

# 验证器确实执行了检查：计数应随测试增长
unbound-control -c /etc/unbound/unbound.conf stats_noreset | grep -E 'num.valops|num.rrset.bogus|num.query.type.A'

# 清掉测试产生的缓存，避免下一轮验收命中旧结果
unbound-control -c /etc/unbound/unbound.conf flush_zone example.net
unbound-control -c /etc/unbound/unbound.conf flush_zone example.org
```

**为什么这么设**：`val-log-level: 1` 保证「每个失败的查询一行」，日志条数即可核对验收是否触发了验证路径；`num.valops` 与 `num.rrset.bogus` 把「检查是否执行」与「策略放行/拒绝」拆成两个独立观测量。`flush_zone` 是必要收尾：第 5 章讲过负缓存会放大故障，Bogus / SERVFAIL 同样可能被缓存，不刷会让下一轮测到上一轮的结论。

**怎么验证**：按「无锚 → permissive → 真验证」跑三遍（无锚态用一个不配 `auto-trust-anchor-file` 的独立实例、端口另开，不要在生产实例上删锚），逐格对照预期列。

**失败模式 / 回滚点**：好签名域始终不出现 `ad`，按顺序查 `module-config`、锚文件非空可读、查询是否命中了转发器。坏签名域返回答案，第一嫌疑是 `set_option` 改成 `yes` 忘了切回，第二是 `harden-dnssec-stripped: no`。回滚：恢复 10.3 基线并 restart，用 `get_option` 逐项断言。

## `harden-dnssec-stripped`：有锚时主动关掉就是制造漏洞

官方手册对 `harden-dnssec-stripped` 的定义是：**要求有信任锚的区必须带回 DNSSEC 数据，数据缺失就把该区判为 Bogus**。默认值 `yes`。若设为 `no`，当收不到 DNSSEC 数据、或 DNSKEY 数据验证不过时，该区会被判成 Insecure——手册的原话是「表现得就像没有信任锚」，并直接警告这会带来**禁用该区安全性的降级攻击风险**。

这条结论要连着本章标题读：在有锚的实例上把 `harden-dnssec-stripped: no` 写上，不是「略微放松」，而是**主动制造一个剥离签名降级漏洞**。路径上的中间设备（或攻击者）只要把 RRSIG / DNSKEY 从应答里删掉，验证器就从「拒绝」滑到「按未签名放行」，且客户端拿到答案、不报 SERVFAIL。验收表第四行会因此从 SERVFAIL 变成 NOERROR，这正是降级成功的证据。

```yaml
# 反面示例：有信任锚时不要这样写
server:
    auto-trust-anchor-file: "/var/lib/unbound/root.key"
    module-config: "validator iterator"
    val-permissive-mode: no
    harden-dnssec-stripped: no
```

**为什么不能这么设**：手册给这个选项留的合法理由只有一种——你确实身处会剥掉 DNSSEC 数据的侵入式防火墙之后，或某区在「签名 / 未签名 / 错误签名」间频繁抖动。这是**已知环境下的妥协**，代价是把「缺证据」当成「已证明未签名」（条文区别见第 16 章）；妥协要写进变更记录、限定作用域、设恢复期限，不能作为默认值留着。

**怎么验证**：`unbound-control -c /etc/unbound/unbound.conf get_option harden-dnssec-stripped` 读生效值，必须是 `yes`；`grep -rn 'harden-dnssec-stripped' /etc/unbound/unbound.conf.d/` 排查 include 覆盖——后加载的文件可能把前面写的 `yes` 盖掉，光看主配置文件会漏。

**失败模式 / 回滚点**：中间设备剥数据导致大面积 SERVFAIL 时，正确动作是**修链路**（换出向、改 MTU / EDNS 载荷、或另设一台能完整取到 DNSSEC 数据的验证器），而不是关开关。确需临时妥协时用 `domain-insecure: "区名"` 限定单个区，影响面比全局关小得多；恢复后立即撤销并 restart。

## 三个名字像、作用却完全不同的 `harden-*`

`harden-dnssec-stripped`、`harden-below-nxdomain`、`harden-glue` 常一起出现在模板里，容易被当成「一组 DNSSEC 加固」；实际上三者分属不同层面，只有一个与 DNSSEC 直接相关。

### `harden-below-nxdomain`：官方 NOTE 限定了生效范围

选项来自 [RFC 8020] 的 NXDOMAIN cut：某名字已知 NXDOMAIN，其下不必再问。但 DNSSEC 对空非终端（empty nonterminal）要求 NOERROR，而很老的权威软件会对空非终端错误返回 NXDOMAIN（反向解析里常见），故需收窄范围。官方手册在这里写了明确的 **NOTE**：**只有 DNSSEC-secure 的 NXDOMAIN 才会被采用，NSEC3 with opt-out 不足够**——老软件本来就没有 DNSSEC，限定在「已验证的 NXDOMAIN」就自动排除了它们。默认值 `yes`。

运维上最重要的推论：**它在没有验证器的部署里是空转**。无信任锚时验证器不产生 Secure 判定，剪枝没有依据；此时 `get_option harden-below-nxdomain` 仍返回 `yes`，但机制并未生效。把「参数存在」误判成「机制生效」是这一节要堵的坑；第 13 章从 QMIN 角度提过同一边界，这里补上验证器侧原因。

```bash
# 核对三件事，缺一不可
unbound-control -c /etc/unbound/unbound.conf get_option module-config
unbound-control -c /etc/unbound/unbound.conf get_option harden-below-nxdomain
test -s /var/lib/unbound/root.key && echo "anchor file non-empty" || echo "NO ANCHOR: nxdomain cut inert"
```

**为什么这么设**：三条命令分别对应「有验证器」「开关为 yes」「有可用锚」；只有前两条而第三条失败，剪枝照样不触发，这正是「无验证器时空转」的可观测证据。

**怎么验证**：`dig @127.0.0.1 -p 5353 nonexistent.example.org A` 返回 `status: NXDOMAIN` 只说明名字不存在，**不说明剪枝生效**。要确认剪枝，需对比同一父名下第二次查询是否不再产生出向报文（`tcpdump -ni any 'udp port 53'`），或查 `stats_noreset` 里与 aggressive 使用相关的计数（具体计数名见官方手册）。

**失败模式 / 回滚点**：把它当成「QMIN 的强力版」是常见误读——前者管「已知 NXDOMAIN 之下不必再问」，后者管「向每一级发多少标签」，二者独立、互不为前置（第 13 章）。老式反向解析区若因空非终端被误剪，可临时设 `no` 观察，代价仅是少量额外出向查询。

### `harden-glue`：与 DNSSEC 无关，任何模式都该开

官方手册的定义很简短：**只信任位于服务器权威范围之内的胶水记录（glue record）**，默认 `yes`。它针对应答附加段里夹带的、不属于该权威服务器所辖区域的 A/AAAA 记录——经典的缓存投毒面：伪造的域外 glue 一旦被收下并用于后续迭代，查询就被导向攻击者指定的地址。

这条与 DNSSEC **没有依赖关系**：域外 glue 是**非权威的附加数据**，本来就不在签名覆盖范围内；即使全域部署 DNSSEC、`val-permissive-mode: no`、所有 `harden-*` 全开，若 `harden-glue: no`，投毒路径依然存在。反过来，即使完全不配信任锚、做纯透传缓存或纯转发器，清理器（scrubber）也该把域外 glue 丢掉。结论：**`harden-glue: yes` 在任何模式下都该开**，它是缓存解析器的基本卫生，不是 DNSSEC 的子选项（第 2 章讲委派自举时已提过）。

```bash
unbound-control -c /etc/unbound/unbound.conf get_option harden-glue
```

**为什么这么设**：它是默认值，最实际的验证是防「被覆盖」——从旧模板拷来的配置可能显式写 `no`。

**怎么验证**：`get_option` 返回 `yes`；也可用 `unbound-control lookup` 查缓存里是否出现不该有的域外地址记录。第 14 章的链路图同样把 `harden-glue: yes` 放在转发器与递归验证器两侧，属跨角色一致项。

**失败模式 / 回滚点**：依赖「域外 glue 直接可用」的畸形委派会变慢或解析不出来；手册里的 `harden-unverified-glue`（默认 `no`）会尝试解析域外 glue 并在失败时回退原值，是这类场景的微调。优先修权威侧委派，不要靠关 `harden-glue` 兜底。

### `harden-referral-path`：默认关闭的实验选项

官方手册把它定义为「对引用路径做加固：为基础设施数据发起额外查询，在配了信任锚且相关区已签名时验证这些应答」，强制对引用路径上遇到的 NS 集合与名字服务器地址做 DNSSEC 验证。**默认是关闭的**，手册给出的理由有三条：给权威服务器增加负担、不是 RFC 标准、额外查询负载可能带来性能问题，并明确标为 **experimental option**；若启用，建议同时调大 `target-fetch-policy` 后面的数字以增加检查深度。

它和本章其他开关的差别值得写清楚：

| 选项 | 默认值 | 与 DNSSEC 的关系 | 成本 | 本章建议 |
|---|---|---|---|---|
| `harden-dnssec-stripped` | `yes` | 直接相关：决定缺 DNSSEC 数据时判 Bogus 还是 Insecure | 无额外查询 | 保持 `yes`，关掉即降级漏洞 |
| `harden-below-nxdomain` | `yes` | 依赖 Secure NXDOMAIN，无验证器时空转 | 减少查询 | 保持 `yes`，但别误判其生效范围 |
| `harden-glue` | `yes` | **无关**：处理非权威附加数据 | 少量解析 | 任何模式都开 |
| `harden-referral-path` | `no` | 部分相关：对引用路径基础设施数据做验证 | **显著增加出向查询** | 按基线保持 `no`，仅在受控实验里评估 |

**怎么验证**：第一条读生效值（期望 `no`），第二条观察额外查询负载，开启后可用同一条命令前后对比。

```bash
unbound-control -c /etc/unbound/unbound.conf get_option harden-referral-path
unbound-control -c /etc/unbound/unbound.conf stats_noreset | grep -E 'num.query|num.valops'
```

**失败模式 / 回滚点**：家庭网关或跨境链路上打开它，表现为解析变慢与出向查询数上升——引用路径每一级都多出一批基础设施查询，这不是 DNSSEC 验证本身慢，是选项的额外负载。回滚：设回 `no` 并 restart；第 14 章链路本来就窄，更不该默认打开实验选项。

### `val-clean-additional`：只清 secure 应答里的坏附加数据

官方手册的定义：指示验证器**从 secure 应答的附加段中删除签名不正确的数据**；对 Insecure / Bogus / Indeterminate / 未检查的应答不产生影响。默认 `no`，用途是保护依赖这台验证器做认证的客户端免受附加段坏数据影响。它容易被误读成「又一条 DNSSEC 加固」，实际边界很窄：只作用于**已判定 Secure 的消息**，且只清附加段中签名不正确的部分；不替代 `harden-glue`（管非权威 glue、与签名无关），也不改变 Bogus 的 SERVFAIL 策略。

`get_option val-clean-additional` 应返回 `yes`；构造一个附加段含坏数据的 secure 应答做对比，或观察下游客户端是否仍能通过独立查询拿到所需记录。

**为什么这么设**：本章基线把它设为 `yes`，与 `harden-glue: yes` 形成「非权威附加数据」与「已签名消息中的坏附加数据」两道互补清理。若下游有老客户端依赖附加段里某些未正确签名的记录，可能出现「主答案正常、附加信息变少」的观感，这是符合手册语义的预期行为，不是丢包。

**失败模式 / 回滚点**：设回 `no` 并 restart 即可，不影响信任链与 AD 位判定；它属于可选增强，不进 fail-closed 的关键路径。

## 信任锚失效 / 根 KSK 轮换的观测与恢复

锚一旦失效，症状不是「某个域解析不了」，而是**大面积、看似无规律的 SERVFAIL**——因为根是所有签名链的公共起点。轮换窗口内若本地锚没跟上新 KSK，验证器会把整个签名 DNS 判成 Bogus。第 16 章会讲 2018 年根 KSK 轮换的条文与坐标（KeyTag 等以 IANA 的 `root-anchors.xml` 与官方手册为准，本章不复述编号与日期），这里只给运维动作。

### 先分清三类「全挂」

| 现象 | 可能原因 | 快速判别 |
|---|---|---|
| 大面积 SERVFAIL，日志有 `validation failure` / `bogus` | 锚过期、锚被轮换、系统时钟漂移 | `date -R` 对时；`get_option` 核对锚路径；`unbound-anchor -l` 对照内置锚 |
| 大面积 SERVFAIL，日志有 `failed to prime trust anchor` | 实例被配成了转发器却带了锚 | `grep -rn forward-zone` + 第 5 章 |
| 部分域 SERVFAIL、大应答域名更明显 | EDNS 分片 / MTU，不是锚问题 | `dig +bufsize=1232` 对比；第 4 章 |

第二行是角色错配、第三行是传输问题，都不是锚失效；混在一起排查最常见的后果是去调上游超时、加第二条 `forward-addr`（第 5 章的竞速坑），问题越修越散。系统时钟要单独强调：RRSIG 生效/过期时间与 [RFC 5011] 观察期都按墙钟计算，网关断电后时钟回到出厂时间是「间歇性全量 SERVFAIL」最易忽略的成因；验收脚本应含 `timedatectl status` 或 `date -R` 断言，NTP 要在 Unbound 启动**之前**同步完。

### 观测命令

```bash
# 1. 时钟与锚文件状态
date -R
timedatectl status 2>/dev/null | grep -i 'synchronized\|system clock' || true
stat -c '%U:%G %a %y %n' /var/lib/unbound /var/lib/unbound/root.key
cat /var/lib/unbound/root.key

# 2. 验证器是否还在做检查、Bogus 是否在涨
unbound-control -c /etc/unbound/unbound.conf stats_noreset | grep -E 'num.valops|num.rrset.bogus'

# 3. 日志里的验证失败是不是集中爆发
journalctl -u unbound --since '1 hour ago' | grep -i 'validation failure\|bogus\|trust anchor' | tail -40

# 4. 用内置锚对照当前锚（unbound-anchor 不带 -v 时几乎不输出）
unbound-anchor -l | head -20
```

**为什么这么设**：`stat` 的 mtime 是判断「有没有成功回写过锚」最直接的证据——长期不变说明 [RFC 5011] 探针可能没在写（权限问题、长期离线、或实例从未真正跑过验证器）。`num.valops` 增长而 `num.rrset.bogus` 暴涨，说明验证器在工作但起点不对；两个都不动则更可能是模块链或角色问题。

### 恢复顺序

```bash
# 1. 先修时钟（NTP 未同步时不要动锚）
systemctl restart systemd-timesyncd    # 或用站点既有的 NTP 客户端
timedatectl status | grep -i synchronized

# 2. 修权限（文件 + 目录，属主为 Unbound 运行用户）
chown unbound:unbound /var/lib/unbound /var/lib/unbound/root.key
chmod 0644 /var/lib/unbound/root.key
sudo -u unbound test -w /var/lib/unbound/root.key && echo "anchor writable"

# 3. 让 unbound-anchor 引导 / 更新锚（退出 1 表示发生了更新，不是失败）
sudo -u unbound unbound-anchor -a /var/lib/unbound/root.key
cat /var/lib/unbound/root.key

# 4. 重启并清掉事故期间缓存的 Bogus 结果
systemctl restart unbound
unbound-control -c /etc/unbound/unbound.conf flush_bogus
unbound-control -c /etc/unbound/unbound.conf flush_negative

# 5. 复跑 10.5 验收表的好签名 / 坏签名两行
dig @127.0.0.1 -p 5353 signed.example.net A +dnssec | grep -E 'status:|flags:'
dig @127.0.0.1 -p 5353 bogus.example.net A +dnssec | grep -E 'status:|flags:'
```

**为什么这么设**：顺序不能乱，**先修时钟再动锚**——时钟不对时，新锚的观察期与签名有效期判断都不可靠。`unbound-anchor` 在 [RFC 5011] 跟踪失败时退回 [RFC 7958] 证书验签下载，网络不通则保持原文件不动，故可安全重跑。`flush_bogus` / `flush_negative` 清理事故期间缓存的坏结果与负缓存；第 5 章讲过负缓存按 SOA minimum 放大故障（常见 3600s），且链路上**每一个实例都要刷**，否则上游修好了下游仍返回缓存的失败结果。

**怎么验证**：第 5 步第一行应 `status: NOERROR` 且 `flags` 含 `ad`，第二行应为 `status: SERVFAIL`。若好签名域仍不带 AD，回 10.3 用 `get_option` 核对 `module-config`、锚路径与 `val-permissive-mode`；若日志仍有 `failed to prime trust anchor`，说明这台是转发器，应转第 5 章而不是继续修锚。

**失败模式 / 回滚点**：最糟的是「为了先恢复业务」直接把 `val-permissive-mode` 改成 `yes` 或把 `harden-dnssec-stripped` 改成 `no`——所有签名域瞬间恢复，但保护同时被抹掉，日志里多出大量 Indeterminate 记录，事后难与真正的锚恢复区分。业务确需临时可用性时，应把解析切到锚正常的实例或按第 6 章本地根区镜像形态调整，而不是在本机降验证等级；恢复后撤销全部临时改动并 restart。另一个隐蔽坑：长期关机或锚文件只读的实例会**错过 [RFC 5011] 观察期**（手册写探针每月数次、机器需经常在线），只在用时开机的实验网关不能假设锚会自动跟上，应在每次启用前跑一次 `unbound-anchor` 并把锚纳入配置管理（第 21 章会再用到）。

## 转发器不要配信任锚

第 5 章已给出反面配置与日志证据：信任锚需自己向根 priming DNSKEY，而 `forward-zone: name "."` 把根 DNSKEY 查询也交给上游，priming 无法完成，日志持续 `failed to prime trust anchor` 并伴随间歇性全量 SERVFAIL——**转发器与验证器二选一**，这里只做交叉引用。本章所有配置都假定实例自己走完整递归（第 4、6 章）；若拓扑是第 12 章的 DoH 前端 + 后端验证器或第 14 章的多级链路，确认 `dig` 验收打在**真正做验证的那一台**上，前端只搬运 DNS 报文，不能替后端验证。

## 加固项速查

| 检查项 | 期望 | 命令 / 依据 |
|---|---|---|
| 模块链 | `validator iterator` | `unbound-control get_option module-config` |
| 根锚文件 | 存在、非空、运行用户可写 | `test -s` + `sudo -u unbound test -w` |
| 锚所在目录 | 运行用户可写 | `sudo -u unbound test -w /var/lib/unbound` |
| 拒绝策略 | `val-permissive-mode: no` | `get_option val-permissive-mode` |
| 缺 DNSSEC 数据 | 判 Bogus | `get_option harden-dnssec-stripped` → `yes` |
| 剥签名降级面 | 已堵 | 10.5 验收表第四行 SERVFAIL |
| 域外 glue | 已清理（与 DNSSEC 无关） | `get_option harden-glue` → `yes` |
| NXDOMAIN 剪枝 | 开，且理解只对 secure 生效 | `get_option harden-below-nxdomain` + 锚就位 |
| 引用路径加固 | 保持默认 `no` | `get_option harden-referral-path` |
| 附加段清理 | `val-clean-additional: yes` | `get_option val-clean-additional` |
| 客户端 CD 位 | 不放松验证 | `get_option ignore-cd-flag` → `yes` |
| 验证失败可观测 | 日志有留痕 | `get_option val-log-level` ≥ 1 + `journalctl` |
| 时钟 | NTP 已同步 | `timedatectl status` / `date -R` |
| EDNS 出向 | 1232，避免 DNSSEC 大应答分片 | `get_option edns-buffer-size` |
| 角色 | 非转发器 | `grep -rn 'forward-zone'`、`grep -rn 'auto-trust-anchor-file'` |

本章收敛到一点：**验证器的安全性不由「开关数量」决定，由「是否 fail-closed」决定**。信任锚就位、坏签名返回 SERVFAIL、好签名带 AD、缺 DNSSEC 数据判 Bogus、域外 glue 一律清理——这五条同时成立，才算真正把「放行与拒绝」握在自己手里。

## KeyTrap 与 NSEC3：验证器自身的拒绝服务

2024 年 2 月披露的两个协议级漏洞，正好打在「验证器必须尽力验证」这个设计上：**CVE-2023-50387（KeyTrap）** 用同 key-tag 的大量 DNSKEY 与 RRSIG 组合，迫使验证器做平方级签名尝试；**CVE-2023-50868（NSEC3）** 用高迭代计数与多 NSEC3 RRSET 迫使大量哈希计算。单个精心构造的应答就能占满一个线程，所有线程都被占住即整体拒绝服务。Unbound ≤ 1.19.0 受影响，**1.19.1** 修复。

Unbound 的修法不是「关掉 DNSSEC」，而是给验证工作量设上限并让出 CPU：

| 内置限额（1.19.1 起） | 值 |
|---|---|
| 构建信任链时允许的 DNSKEY key 碰撞 | 4 |
| 每个 RRSET 的验证尝试 | 8 |
| 每次应答的验证尝试（超过则挂起） | 8 |
| 挂起前允许的 NSEC3 哈希计算 | 8 |
| 单次查询的挂起总上限（超过则报错） | 16 |

挂起（suspension）意味着验证器先去做别的工作、再按退避计时恢复，其它请求不会被一条恶意应答锁死；已完成的 RRSET 仍会进缓存。**关键运维结论**：遭受攻击时**不要关闭 DNSSEC 验证**来「缓解」—— 那等于主动降级，把缓存投毒的门重新打开；`serve-expired`（[RFC 8767]）若要兜底，应在独立线程返回过期数据，不要和验证抢同一个线程。

**怎么验证**：`unbound -V` 确认版本 ≥ 1.19.1；观察 CPU 是否长期单线程 100%、SERVFAIL 率与 bogus 计数；升级前后用同一批正常域名做基线对比。**回滚点**：升级失败时回退到上一版二进制，但那等于重新暴露在这两个 CVE 下，应视为临时状态。协议设计的根因（[RFC 4035] + [RFC 6840] 要求尝试所有 key/signature 组合）在第 16 章展开。

## 本章要点

- 信任锚是**会随时间演化的状态**，不是静态配置：`auto-trust-anchor-file` 按 [RFC 5011] 跟踪滚动并会回写文件，因此锚文件与所在目录都必须对 Unbound 运行用户可写，启用 `chroot` 时路径要落在 chroot 内；`unbound-anchor -a` 负责初始化，退出码 1 表示发生了更新而非失败。
- 验证器三态必须逐格验收：**无锚 = 透传不验证**（坏签名域也返回答案、日志干净）、`val-permissive-mode: yes` = **记日志但放行**（Bogus 标成 Indeterminate，好签名仍置 AD）、`val-permissive-mode: no` = **真验证**（坏签名 SERVFAIL、好签名带 AD）。无签名（Insecure）一类三态一致，不能用来区分策略。
- **有锚时设 `harden-dnssec-stripped: no` 等于主动制造剥离签名降级漏洞**：官方手册明确写它会让缺 DNSSEC 数据的区「表现得就像没有信任锚」，并警告降级攻击风险；中间设备删掉签名记录即可让验证器从拒绝滑向放行。
- 三个名字相近的开关分属不同层面：`harden-below-nxdomain` 依官方 man page 的 NOTE **只对 DNSSEC-secure 的 NXDOMAIN 生效**（NSEC3 opt-out 不够），无验证器时是空转，别把参数存在误判成机制生效；`harden-glue` **与 DNSSEC 无关**，域外夹带 glue 是缓存投毒面，任何模式都该开；`harden-referral-path` 是默认关闭的实验选项，会显著增加出向查询。`val-clean-additional` 只清 secure 应答附加段里签名不正确的数据。
- 信任锚失效 / 根 KSK 轮换表现为**大面积 SERVFAIL 且日志有 validation failure**，先分清「锚 / 时钟」「转发器角色错配」「EDNS 分片」三类成因；恢复顺序是修时钟 → 修权限 → `unbound-anchor` → restart → `flush_bogus` / `flush_negative`（链路上每个实例都要刷）→ 复跑验收表，切勿用 permissive 或关 `harden-dnssec-stripped` 来「先恢复业务」。
- 转发器上不要配信任锚（第 5 章已论证，角色二选一）；DNSSEC 的 RFC 族与设计原理在第 16 章，本章只讲配置与验证。加密传输（DoT / DoH / DoQ）保护传输隐私，DNSSEC 保护数据完整性与来源认证，二者不互相替代。

## 参考资料

- Unbound 官方手册，`unbound.conf(5)` 的 `auto-trust-anchor-file`、`module-config`、`val-permissive-mode`、`harden-dnssec-stripped`、`harden-below-nxdomain`、`harden-glue`、`harden-referral-path`、`val-clean-additional`、`ignore-cd-flag`：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- Unbound 官方手册，`unbound-anchor(8)` 的根信任锚引导、退出码与 [RFC 7958] 回退路径：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-anchor.html
- Unbound 官方手册，`unbound-control(8)` 的 `get_option`、`set_option`、`stats_noreset`、`flush_zone`、`flush_bogus`、`flush_negative`：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- Unbound 官方手册，Getting Started 配置示例中的 DNSSEC 信任锚段：https://unbound.docs.nlnetlabs.nl/en/latest/getting-started/configuration.html
- Unbound 官方手册，Home Resolver 用例（含 `auto-trust-anchor-file` 的完整配置形态）：https://unbound.docs.nlnetlabs.nl/en/latest/use-cases/home-resolver.html
- RFC Editor，[RFC 5011] *Automated Updates of DNS Security (DNSSEC) Trust Anchors*：https://www.rfc-editor.org/rfc/rfc5011.txt
- RFC Editor，[RFC 6840] *Clarifications and Implementation Notes for DNS Security*：https://www.rfc-editor.org/rfc/rfc6840.txt
- IANA，根区信任锚数据（`root-anchors.xml`，含 KeyTag 与有效期）：https://data.iana.org/root-anchors/root-anchors.xml
