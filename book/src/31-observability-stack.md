# 可观测性栈：dnstap、Grafana、eBPF 与告警

一个 `unbound-control stats_noreset` 能告诉你「现在有多少 SERVFAIL」，却回答不了「哪个客户端把某个域刷出了几千个不同名字」，也回答不了「出向 UDP/53 是被 nftables 吃了，还是被上游丢了」。把这三种问题混在一处排查，结局往往是：指标看着正常，用户仍在报障，于是反复重启 Unbound 刷缓存，而真正的断点在别处。可观测性不是一个开关，是**三条互相独立、又能互相印证的链路**：`unbound-control` 按需统计、dnstap 查询流、以及操作系统侧的收发与归因。这里讲怎么接进指标与日志系统、怎么把数字变成能分级/降噪/演练的告警。


## 先划边界：采集点已有，缺的是栈

### 三层数据源与各自的盲区

| 层 | 采集点 | 能回答 | 回答不了 | 载体 |
|---|---|---|---|---|
| ① 应用内 | `unbound-control stats_noreset`、`log-servfail`、`get_option` | 解析成功率、命中率、递归耗时、DNSSEC bogus、SERVFAIL 原因、生效配置值 | 「谁问的」「问了什么名字」、单源速率、内核丢包 | 命令行 / exporter |
| ② 查询流 | dnstap（socket → consumer → 结构化日志） | 客户端地址、QNAME、QTYPE、应答码、上游交互、唯一 FQDN 数 | 报文在内核丢在哪、是否被 tproxy 改写、TCP 重传 | JSON / fstrm / Loki / Elasticsearch |
| ③ 内核与网络 | eBPF、`ss`、`conntrack`、nft counter | UDP/53 收发、丢包与重传、按 PID/UID 归因、TCP 回退、mark 路径 | QNAME 内容、缓存命中与否、DNSSEC 状态 | bpftrace / tcpdump / nft |

三层不是「越多越好」，而是**盲区互补**：① 是累积计数器，适合趋势与告警；② 是高基数明细，适合取证与隧道检测，但体积与隐私代价最大；③ 不碰 DNS 语义，却恰好能证明「包有没有离开这台机器」。只装一层的误判很有特征：只有 ① 时 `num.queries_timed_out` 上涨像上游挂了，实际可能是第 14 章 `output-tproxy` 把解析器自己的出向 53 打了 mark；只有 ② 时查询一条不少却没有出向消息，既可能是 `local-zone` 接管（第 24 章优先级），也可能是 consumer 断流只收到半边；只有 ③ 时看得出包在走，却分不清 NOERROR 与 SERVFAIL。所以验收标准不是「装了 Grafana」，而是**同一次故障注入，三条链路都能各自看到对应信号，且时间戳能对上**；第 7 章的 `extended-statistics` 让统计多出 QTYPE 等维度、`statistics-interval: 0` 关掉周期性打点改由控制通道按需取，本章沿用，不重贴配置块。

## 第一层：应用内统计与 SERVFAIL 日志

### `stats_noreset` 的读数纪律

`stats` 会重置计数（受 `statistics-cumulative` 控制），基线必须全程用 `stats_noreset`。集成进栈后多一条纪律：**只读一次，别让两个采集器同时读**。下面这段是可复制的快照骨架；若已装社区 exporter，就不必再跑第二套采集：

```bash
#!/bin/sh
# /usr/local/sbin/unbound-stats-snapshot.sh
set -eu
CTRL="unbound-control -c /etc/unbound/recursor.conf"
OUT=/var/lib/node_exporter/textfile_collector/unbound.prom
$CTRL stats_noreset | grep -E '^(total\.num\.(queries|cachehits|cachemiss)|num\.(queries|SERVFAIL|answer\.bogus|queries_timed_out|recursivereplies|expired|query\.opcode\.)|mem\.|recursion\.time\.)' > /tmp/ub-stats.$$
mv /tmp/ub-stats.$$ "$OUT.snapshot"
```

**为什么这么设**：`grep` 只留关键计数器，不把几十个 `num.query.type.*` 明细推进时序库；临时文件加 `mv` 是原子替换，采集器不会读到半截内容。`num.query.opcode.UPDATE` 这类**非 QUERY opcode 计数**（完整字段名见官方手册扩展统计章节）值得单独留意：递归解析器正常收不到 UPDATE，它持续非零通常意味着动态更新报文打错了地址，或第 25 章 DDNS 链路配错了目标。另外 `unbound-control` 的运行时 `local_zone` 增删（第 24 章）也可当作一条变更事件记进日志，便于把「过滤表更新」与命中率变化对上。  
**怎么验证**：`$CTRL stats_noreset | grep -E 'num.query.opcode|num.answer.bogus|num.queries_timed_out'` 应列出字段；两次执行之间做一次 `dig @192.0.2.53 example.org A`，`total.num.queries` 增量应为 1 而非被清零。  
**失败模式 / 回滚点**：控制通道未启用或证书权限不对时整条 ① 链路**静默失效且无告警**，因此后面必须有「采集器本身无数据」的告警。回滚：删掉脚本与定时项，回到人工按需执行，不要改成 `stats` 凑数。

### 关键指标清单

| 指标 | 层 | 判读要点 |
|---|---|---|
| qps（`total.num.queries` 速率） | ① | 与「单源 qps」分开；总量平稳不代表无单源突增 |
| 缓存命中率（`cachehits / (cachehits + cachemiss)`） | ① | 骤降常是冷缓存、刚刷过缓存、或查询换成随机子域 |
| SERVFAIL 数 / 率 | ① | **用比率不用绝对值**，业务量翻倍时绝对值天然翻倍 |
| NXDOMAIN 比 | ② | 与第 29 章 DGA / 扫描特征联动 |
| 上游 RTT / 超时数（`recursion.time.avg`、`num.queries_timed_out`、`num.recursivereplies`） | ① | 超时涨而递归应答不涨，基本可断定出向链路或上游（第 7 章） |
| 递归查询数（`total.num.cachemiss`） | ① | 与 qps 比值异常抬升说明缓存失效或被绕过 |
| `unwanted` 计数 | ① | 被拒 / 不想要的查询，字段名见官方手册对应章节；呼应第 9 章 ACL |
| 单源 qps | ② | 必须按客户端地址聚合，① 层做不到 |
| 唯一 FQDN 数（按二级域、按源） | ② | 第 29 章认定的最通用隧道检测量，与编码 / QTYPE / 长度无关 |
| `ratelimit` 命中数 | ① | 官方标 experimental，阈值按 `stats_noreset` 基线调（第 29 章） |
| DNSSEC bogus 数（`num.answer.bogus`） | ① | 突增多半是信任锚 / 上游验证链问题，不是「网络慢」 |
| dnstap 帧速率 / 日志卷可用空间 | ② / 主机 | 帧速率归零即断流；30 天保留期写死后磁盘是最常见的静默故障点 |

### 日志分级与 NTP 这个前提

`verbosity` 平时保持 1，排障临时调到 2–3，排完立刻调回；`log-servfail` 独立于 verbosity、以 error 级别输出，是性价比最高的一行，把 verbosity 拉到 4 换来的多半是无关调试行和打满的磁盘。下面只读 / 临时改生效值，配置块在第 7 章，不重复：

```bash
CTRL="unbound-control -c /etc/unbound/recursor.conf"
$CTRL get_option verbosity                 # 先读再改，才知道回滚到哪
$CTRL get_option log-servfail
$CTRL verbosity 3                          # 排障窗口：专用命令临时提高详细度
timedatectl show -p NTPSynchronized --value # 日志关联的前提
chronyc tracking 2>/dev/null | head -5 || true
```

**为什么这么设**：日志关联的前提是**所有链路共用同一个可信时钟**。dnstap 帧、journald、`nft` 计数、Prometheus 抓取时间只要差几秒，「注入 → 指标抬升 → 日志出现」的因果链就会拼错，甚至得出「告警早于故障」的荒谬结论。NTP 时间同步（协议规范见 RFC Editor 官方资料）不是可选项；`timedatectl` 只确认本机已同步，跨主机还要确认是否同源。`verbosity` 是 `unbound-control` 的专用命令，与 `set_option` 不同；`log-servfail` 不在 `set_option` 支持的选项清单内，应固化在配置文件（第 7 章）。  
**怎么验证**：`timedatectl show -p NTPSynchronized --value` 应回 `yes`；`gw-01` 与 `cloud-01` 上 `date -u` 一致到秒级；`journalctl -u unbound --since '2 min ago'` 与 dnstap JSON 时间字段能对上。  
**失败模式 / 回滚点**：`verbosity` 命令与 `set_option` 都是运行时临时值、reload 即失效，别当成已固化配置写进演练报告；`chronyc` 未安装时第二行被 `|| true` 吞掉，此时要用别的手段确认同步。回滚：`$CTRL verbosity 1`，配置文件里保留 `log-servfail: yes`。

一条 SERVFAIL 的原因要落到类别：上游超时、DNSSEC 验证失败（bogus）、根区 / 转发区无法建立、缓存里的旧坏条目。`log-servfail` 给「这一条为什么失败」，`num.answer.bogus` 给「这类失败有多少」；告警挂后者速率才稳定，**定性仍要靠前者**，否则 bogus 告警响了还得从头翻日志。第 7 章「哪个实例 → 访问控制 → 日志原因 → 缓存旧条目 → nftables 出向豁免 → EDNS/MTU」的顺序在这里仍是入口。

## 第二层：dnstap 查询流的集成与隐私边界

### socket → consumer 的拓扑

集成侧要画清的是谁在监听，以及监听者挂了会怎样：

```text
[Unbound] --fstrm 帧--> /run/dnstap.sock (unix socket) --> [dnstap consumer]
                                                              |
        +-----------------------------------------------------+
        |                                                     |
  轻量：JSON Lines + jq 巡检                     重量：JSON -> Loki / Elasticsearch
        |                                                     |
        +--------------> [Alertmanager] <----------------------+
                                ^
        [Prometheus] <-- ① 层统计 / exporter --> [Grafana]：指标 + 日志同一时间轴
```

**为什么这么设**：把 consumer 画成**独立进程**是关键。socket 是写入目标、不是队列；没有 consumer 监听时写入失败，查询流副本整段消失，而解析服务通常照常工作——这正是「静默失效」的典型形状。日志侧与指标侧最终汇到同一告警入口、同一条 Grafana 时间轴，才能做「先看到指标抬升，再下钻到 QNAME」。  
**怎么验证**：`ls -l /run/dnstap.sock` 看 socket，`ss -xlp | grep dnstap` 看谁在监听，consumer 帧计数（随实现而异）在 `dig @192.0.2.53 example.org A` 前后应递增——三者缺一都不算「链路上有数据」。  
**失败模式 / 回滚点**：consumer 崩溃 / 重启窗口丢帧；socket 权限过宽会把全量查询流暴露给本机任意用户。回滚：`dnstap-enable: no` 并 restart（配置在第 7 章），同时**保留到期删除任务**——关掉采集不会让历史文件消失。

### 消息类别、全量与采样

dnstap 消息按观察点与方向展开：client、resolver、forwarder 等观察点各配 query / response 两个方向，第 7 章六项 `dnstap-log-*-messages` 全开覆盖的正是这三组观察点的两个方向。按**四类**口径使用：客户端查询、客户端应答、解析器（含转发器）出向查询、出向应答；完整消息类型与字段定义见官方 dnstap 资料。

| 策略 | 做法 | 代价 | 适用 |
|---|---|---|---|
| 全量 | 六项全开，全部落盘，30 天到期删除 | 磁盘与 I/O 最大；副本最完整 | 本书默认；隧道检测与取证必须全量 |
| 按方向采样 | 只留 client 侧，或按 `1/N` 抽帧 | 无法还原「问了哪个上游」 | 只关心客户端行为 |
| 按名字 / 源采样 | 排除健康探测域与白名单源 | **容易把异常一并排除** | 容量受限时的退让，需白名单审计 |

采样最危险的不是丢数据，而是**丢掉的恰好是异常**：隧道流量通常 qps 不高，`1/N` 抽样后唯一 FQDN 数被系统性低估，第 29 章的流量分析直接失效。宁可明确写死保留期 + 到期删除，也不在采集端悄悄采样。落盘格式常见 JSON / fstrm 两类，字段名与时间字段含义随 consumer 实现而异，见所选实现的官方文档。

### 查询日志本身就是隐私副本

这一条要在集成方案里**再次明确**，因为它比第 7 章的单机配置更容易破功：单机时日志只在本机磁盘，接进栈之后它会被推送、索引、复制到副本、被 Grafana 跨主机调阅、被备份系统再存一份。「抗元数据聚合」在自己门口破功，往往就发生在这一步。三条硬边界：

- **保留期跟着副本走**：Loki / Elasticsearch 的索引保留、快照、对象存储桶生命周期都要与第 7 章的 30 天一致；只清理源主机 `/var/log/dnstap` 而索引里还留一年，等于没设保留期。
- **访问范围按最小权限**：日志面板账号与指标面板账号分开；能查 QNAME 的人应比能看 qps 的人少一个数量级；`<TOKEN>` 类凭据独立、可轮换、有审计。
- **不无条件推第三方 SaaS**：把全量查询流推到外部托管日志服务，等于把「家里每个客户端问过什么」交给第三方长期保存。这是**数据主权决定**，要在方案评审里显式写下来。

dnstap 里带 QNAME、客户端地址、时间戳、应答码——没有报文正文，但已足够还原一个网络里所有终端的行为轨迹。它和 DNSSEC 完全无关：DNSSEC 是数据完整性 / 来源认证，DoT / DoH / DoQ 是传输加密 / 隐私，而 dnstap 是**你自己主动造的一份明文元数据副本**。三者不能互相替代，也不能用「我们开了 DoT」给日志留存辩护。

### 轻量方案：JSON 落文件加 `jq` 巡检

不是每个环境都需要 Loki。把 consumer 输出的 JSON Lines 按小时切文件，配一个巡检脚本就能覆盖第 29 章大部分检测量。字段名随 consumer 实现而变，下面是骨架：

```bash
#!/bin/sh
# /usr/local/sbin/dnstap-json-inspect.sh —— 字段名为示意，以所选 consumer 输出为准
set -eu
LOG=/var/log/dnstap/queries.jsonl
# ① 按「源 + 二级域」统计唯一 FQDN 数（隧道检测最通用的量）
jq -r 'select(.type == "CLIENT_QUERY") | [.client_ip, .qname] | @tsv' "$LOG" \
  | awk -F'\t' '{ n = split($2, p, "."); d = (n >= 2 ? p[n-1] "." p[n] : $2);
                  seen[$1 "\t" d "|" $2] = 1 }
                END { for (k in seen) { split(k, a, "|"); cnt[a[1]]++ }
                      for (c in cnt) print cnt[c], c }' | sort -rn | head -20
# ② payload 粗筛：超长单标签（QNAME 总长上限见 [RFC 1034] 相关条文）
jq -r 'select(.type == "CLIENT_QUERY") | .qname' "$LOG" \
  | awk -F'.' '{ for (i = 1; i <= NF; i++) if (length($i) >= 40) print length($i), $0 }' \
  | sort -rn | head -20
# ③ 断流自检：文件超过 10 分钟无新增即视为可疑
find "$LOG" -mmin +10 -print
```

**为什么这么设**：① 只依赖「名字去重」，与编码 / QTYPE / 长度无关，能覆盖未知隧道（第 29 章流量分析一支）；② 是 payload 分析一支的快速粗筛，**只产候选不做判定**；③ 把「没有数据」变成可检查状态，这是整套栈最容易被漏掉的告警。三支都不联网、不依赖外部服务。  
**怎么验证**：先 `dig @192.0.2.53 www.example.org A`，① 应输出 `1` 级计数；再注入一批 `label01.tunnel.example.net` … `label99.tunnel.example.net`，① 里该域计数应接近注入条数，② 把标签拉长到 40 字节以上应列出候选。  
**失败模式 / 回滚点**：JSON 字段名不是 dnstap 协议强制的，`select(.type == "CLIENT_QUERY")` 在别的 consumer 上可能是 `message.type` 或嵌套结构，上线前必须 `head -1 "$LOG" | jq .` 对一次字段；② 的 40 字节只是教学取值，CDN 与证书续期会产生合法长标签，直接当告警必然误报。回滚：脚本不改采集配置，停掉定时项即可。

### 重量方案：Loki / Elasticsearch 与告警

当节点多到需要跨主机检索 QNAME（第 26 章 anycast 的 per-node 集中观测就在这一层落地），把 JSON 送进日志系统更合适。骨架仍是 `consumer → 结构化日志 → 查询语言聚合 → 告警规则`。

**为什么这么设**：明细与取证留在日志系统（保留期与 30 天对齐），趋势交给指标系统（`dnstap_unique_fqdn_per_domain`、`dnstap_frames_total` 这类低基数聚合量），告警只挂低基数指标与「无数据」状态、明细由人从告警链接下钻。把 QNAME 直接当 Prometheus label，等于把高基数问题搬进时序库。  
**怎么验证**：`dig @192.0.2.53 probe31.example.net A` 后应能按该 QNAME 检索到，且 `instance` / `node` 标签指回正确节点——同时验收第 26 章要求的 per-node 区分能力。  
**失败模式 / 回滚点**：索引保留期与源主机不一致、副本数放大存储、日志系统宕机导致 consumer 背压堵住 socket。回滚：consumer 保留「同时落本地文件」分支，切回轻量方案，而不是停采集。

## 第三层：内核与网络，不改 Unbound

### 先说明白：Unbound 不原生支持 eBPF

**Unbound 不原生支持 eBPF**，官方也没有「eBPF 导出器」这类配置项。这里的 eBPF 是**操作系统侧的观测能力**：在内核里挂探针观察 UDP/53 与 TCP/53 的收发、丢弃、重传与进程归因，Unbound 不用改配置、不用重编译、不用重启。代价在另一侧：需要内核支持与相应权限，探针名与可用挂载点随内核版本变化，具体以 bpftrace 与内核官方文档为准（见参考资料），本书不对任何发行版的内核版本作断言。

### bpftrace / tcpdump 教学命令

```bash
# ① 先列可用探针：名字随内核版本变化，别照抄后面那段
bpftrace -l 'kprobe:udp*' | head -20
bpftrace -l 'tracepoint:net:*' | head -20
# ② 按 PID / UID 归因统计 53 端口相关 UDP 收发（探针名请按 ① 的结果替换）
bpftrace -e '
  kprobe:udp_sendmsg /comm == "unbound"/ { @tx[pid, uid, comm] = count(); }
  interval:s:10 { print(@tx); clear(@tx); }
'
# ③ TCP 回退与重传：先看有没有 SYN 到 53，再落盘细看
tcpdump -ni any 'tcp port 53 and tcp[tcpflags] & tcp-syn != 0' -c 20
tcpdump -ni any -s 0 'port 53' -w /tmp/dns-53.pcap
```

**为什么这么设**：② 的价值是「按 PID/UID 归因」——第 14 章 `meta skuid unbound` 的 uid 豁免是否真生效，与其猜，不如直接看哪个进程在往 53 发包。③ 抓 TCP/53 的 SYN，是因为 EDNS 报文过大被分片丢弃后会重试 TCP（第 4 章 1472 字节那个坑），「UDP 出去没回来 + 紧跟着 TCP SYN」是一条很干净的证据链。抓包文件同样属于查询流副本，跑完就删。  
**怎么验证**：`dig @192.0.2.53 example.org A +short` 期间 ② 的 `@tx` 应出现 `unbound` 的 pid/uid；`dig @192.0.2.53 example.org A +bufsize=4096` 观察 ③ 是否出现 TCP/53 SYN；`bpftrace -l` 无输出说明内核或工具链不支持，此时不要硬套探针名。  
**失败模式 / 回滚点**：探针名不存在时 bpftrace 直接报错，不会静默给错数据；但 `tcpdump -w` 长时间挂着会写满磁盘。回滚：`Ctrl-C` 结束 bpftrace，`rm -f /tmp/dns-53.pcap`，不改 Unbound 任何配置。

### `ss` / `conntrack` / nft counter：先看 nftables

eBPF 需要内核支持，下面这组几乎到处都有，且是第 14、22 章那条经验——**先看 nftables 规则**——的直接延续：

```bash
# ① 本机谁在听 :53 / :5353（第 14 章 tproxy 目标端口）
ss -u -a -n -p | grep -E ':53\b|:5353\b|:5357\b' || true
# ② UDP/53 的 conntrack 条目（NodeLocal DNSCache 那类竞态的观察点，第 20 章）
conntrack -L -p udp --dport 53 2>/dev/null | head -20 || true
# ③ 只读看规则与计数，确认 mark / skuid 豁免那几行还在
nft list ruleset | grep -i -E 'counter|mark|dport 53|skuid' | head -30
nft list chain inet mangle output-tproxy
```

**为什么这么设**：① 确认「解析器出向豁免」有没有被规则改动顶掉；② 看 UDP/53 conntrack 是否堆积，是重传与超时的旁证；③ 是**只读**查看（不同于 `nft reset counters`），不会破坏基线。nft counter 能回答 ① ② 两层都回答不了的问题：那个包究竟有没有走到 `meta mark set` 那一行。  
**怎么验证**：先 `nft list ruleset | grep -i counter`，做一次 `dig @192.0.2.53 example.org A`，再读一次，对应规则 packets 应递增；若 `output-tproxy` 链尾计数递增而解析超时，优先怀疑第 14 章那类「自己的出向被自己劫持」。  
**失败模式 / 回滚点**：`nft reset counters` 会清零所有计数，排查前后不要随手执行，否则演练差分证据全没了；`conntrack` 表为空不代表没有流量（UDP 条目寿命短，或工具未安装被 `|| true` 吞掉）。恢复规则集用第 14 章备份文件整体 `nft -f`，不要逐条删。

## 集成骨架

### stats → Prometheus → Grafana

常见接法是社区 exporter（如 `unbound_exporter` 一类实现）把 `unbound-control stats` 转成 `/metrics`，Prometheus 抓取，Grafana 出面板；也可走上一节的 textfile 快照。下面 `prometheus.yml` 片段是可复制骨架：

```yaml
# /etc/prometheus/prometheus.yml 片段
global: { scrape_interval: 15s, evaluation_interval: 15s }
rule_files: [ /etc/prometheus/rules/unbound.yml ]
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['192.0.2.53:9093']
scrape_configs:
  - job_name: unbound-recursor
    static_configs:
      - targets: ['192.0.2.53:9167']
        labels: { role: recursor, node: gw-01 }
  - job_name: unbound-forwarder
    static_configs:
      - targets: ['203.0.113.10:9167']
        labels: { role: forwarder, node: cloud-01 }
  - job_name: node
    static_configs:
      - targets: ['192.0.2.53:9100', '203.0.113.10:9100']
```

**为什么这么设**：`role` 与 `node` 两个 label 是后面所有面板与告警的分组依据——第 7 章多实例、第 26 章 per-node 差异都靠它区分，缺了它两台节点的 SERVFAIL 会被混成一条曲线；`node` job 提供磁盘指标，没有它就写不出「磁盘将满」告警。exporter 的**具体指标名随实现而变**，本节与下一节表达式都是骨架，字段名以 `/metrics` 实际输出为准。  
**怎么验证**：`promtool check config /etc/prometheus/prometheus.yml` 无报错；`curl -s http://192.0.2.53:9167/metrics | head -20` 能看到计数器；Prometheus `/targets` 页两个 unbound job 为 UP。  
**失败模式 / 回滚点**：exporter 与控制通道绑在一起，`unbound-control` 连不上时它往往仍返回 200 但指标停滞——**UP 不等于有数据**，所以要有「序列无变化」类告警。回滚：注释掉对应 `scrape_configs` 条目并 reload；Prometheus 与 Alertmanager 凭据一律 `<TOKEN>`，不要写进可公开仓库。

### Grafana 面板清单

面板按「先看总量、再看比例、最后看可疑度」排，不要一屏塞满：

| 面板 | 表达式意图（骨架） | 判读要点 |
|---|---|---|
| qps（按 role / node 分组） | `rate(queries_total[5m])` | 总量与单源分开；切流后看节点间是否均衡（第 26 章） |
| SERVFAIL 率 | `rate(servfail_total[5m]) / clamp_min(rate(queries_total[5m]), 1)` | 用比率；`clamp_min` 避免低流量除零尖刺 |
| 缓存命中率 | `cachehits / (cachehits + cachemiss)` | 骤降关联冷缓存 / 刷缓存 / 随机子域 |
| 上游 RTT 与超时 | `recursion_time_avg`、`rate(timed_out_total[5m])` | 超时涨而递归应答不涨 → 出向链路（第 7、14 章） |
| 隧道可疑度 | `dnstap_unique_fqdn_per_domain` Top N + 长标签候选数 | 对应第 29 章流量 / payload 两支；必须带白名单过滤 |
| 采集链路健康 | `rate(dnstap_frames_total[5m])`、exporter 序列新鲜度 | 单独一行，避免「没告警」其实是「没数据」 |
| 日志卷余量 | node exporter `avail_bytes / size_bytes` | 30 天保留期的容量前瞻 |

面板放一个统一时间轴变量，指标面板与 dnstap 日志面板共用同一区间，否则「先看到曲线、再下钻 QNAME」会因来回改时间范围而做不下去。指标是等间隔采样、dnstap 是逐条事件、nft counter 是累计计数，三者天然不同步：以 NTP 同步后的 UTC 为唯一时基，注入动作记一条带 `date -u` 的锚点，指标看锚点后第一个抓取窗口，dnstap 看锚点前后几秒明细，nft counter 看注入前后**读数差**而非绝对值；多节点（第 26 章）还要确认 NTP 同源，否则 per-node 曲线会横向错位。

## 告警规则示例与误报治理

### 八条规则的总表

阈值全是**占位值**，必须按下一节 7 天基线替换，`for` 抑制单窗口抖动；第 8 条最容易被省掉却最值钱。

| # | 告警 | 来源 | 触发条件（骨架） | 分级 |
|---|---|---|---|---|
| 1 | SERVFAIL 率超阈 | ① | 比率连续 5 分钟 > 基线倍数 | critical |
| 2 | 单源 qps 突增 | ② | 某客户端 qps > 基线 N 倍，持续 5 分钟 | warning |
| 3 | 唯一 FQDN 数异常 | ② | 按域 / 按源唯一名字数超基线 | warning |
| 4 | 上游全超时 | ① + ③ | 超时涨、递归应答不涨，且 nft 出向计数有包 | critical |
| 5 | DNSSEC bogus 突增 | ① | `num.answer.bogus` 速率超基线 | critical |
| 6 | dnstap 断流 | ② | 帧计数 10 分钟无增量 / socket 无 consumer | warning |
| 7 | 磁盘将满 | ③ / 主机 | 日志卷可用空间 < 15% | warning |
| 8 | 采集器无数据 | ① | exporter 序列长时间不更新 | warning |

前七条全依赖「数据还在来」，采集链路自己断掉时仪表盘会显示一条安静的平线，很容易被误读成「系统很健康」。隧道类规则（#2、#3）刻意留在下面片段之外：它们的量由日志侧聚合，表达式随聚合实现而变，写成一段看似能跑的 PromQL 反而误导。

### Prometheus 规则片段

```yaml
# /etc/prometheus/rules/unbound.yml —— 阈值为占位，按 7 天基线替换
# 指标名随所选 exporter 而定，以 /metrics 实际输出为准
groups:
  - name: unbound-health
    rules:
      - alert: UnboundSERVFAILRateHigh
        expr: rate(unbound_servfail_total[5m]) / clamp_min(rate(unbound_queries_total[5m]), 1) > 0.05
        for: 5m
        labels: { severity: critical }
        annotations: { summary: "SERVFAIL 率连续 5 分钟超阈（先查 log-servfail 原因）" }
      - alert: UnboundDNSSECBogusSpike
        expr: rate(unbound_answer_bogus_total[5m]) > 1
        for: 5m
        labels: { severity: critical }
        annotations: { summary: "DNSSEC bogus 突增：查信任锚与上游验证链" }
  - name: dnstap-pipeline
    rules:
      - alert: DnstapNoFrames
        expr: increase(dnstap_frames_total[10m]) == 0
        for: 5m
        labels: { severity: warning }
        annotations: { summary: "dnstap 断流：检查 socket 与 consumer 进程" }
      - alert: DnstapDiskFilling
        expr: node_filesystem_avail_bytes{mountpoint="/var/log/dnstap"} / node_filesystem_size_bytes{mountpoint="/var/log/dnstap"} < 0.15
        for: 10m
        labels: { severity: warning }
        annotations: { summary: "日志卷将满：核对 30 天到期删除任务与索引保留期" }
```

**为什么这么设**：`SERVFAILRateHigh` 用比率，业务量翻倍不误报；`clamp_min(..., 1)` 防夜间低流量分母趋零把曲线打飞；critical 规则的 annotation 直接写明第一步查什么，把第 7 章排查顺序塞进告警本身；`DnstapNoFrames` 用 `increase == 0` 而非 `absent()`，因为「指标存在但停滞」正是 consumer 挂掉的典型形态。  
**怎么验证**：`promtool check rules /etc/prometheus/rules/unbound.yml` 无报错；`/alerts` 页能看到八条规则（其余在 Alertmanager 侧）处于 inactive；注入演练后对应规则应转 pending 再 firing。  
**失败模式 / 回滚点**：指标名与所选 exporter 不匹配时规则会**永久 inactive 且不报错**，这是最隐蔽的失败；阈值拍脑袋写死会在峰谷反复误报，最后被整体静音——告警被静音比没有告警更危险。回滚：从 `rule_files` 摘掉该文件并 reload，先恢复人工巡检。

### 先测 7 天基线

```bash
#!/bin/sh
# /usr/local/sbin/unbound-baseline.sh —— 每小时一次，连续跑 7 天
set -eu
CTRL="unbound-control -c /etc/unbound/recursor.conf"
echo -n "$(date -u +%Y-%m-%dT%H:%M:%SZ) " >> /var/log/unbound-baseline.tsv
$CTRL stats_noreset | grep -E '^(total\.num\.queries|num\.SERVFAIL|num\.answer\.bogus|num\.queries_timed_out|total\.num\.cachehits|total\.num\.cachemiss)' | tr '\n' ' ' >> /var/log/unbound-baseline.tsv
echo >> /var/log/unbound-baseline.tsv
```

**为什么这么设**：7 天覆盖完整周周期——工作日与周末的查询量、夜间批处理、证书续期与健康探测节奏都不同，只测一天会把周期性峰值当异常。文件是**累积快照**，看速率要取相邻两行之差；用 `stats_noreset` 才不会自己把计数读没。  
**怎么验证**：7 天后 `wc -l /var/log/unbound-baseline.tsv` 接近 168；画出 qps 与 SERVFAIL 率日曲线，取 p95 / p99 与最大周期峰值作阈值上界，而不是取均值。  
**失败模式 / 回滚点**：基线期若恰含真实故障或演练，阈值会被污染，须标注并剔除该窗口；基线含查询计数但不含 QNAME，仍按内部文档保管。回滚：停掉定时项，保留文件供复盘。

### 白名单：CDN、健康探测、证书续期

第 29 章 payload 分析（超长 QNAME、高熵标签、罕见 QTYPE）会撞上一批完全合法的流量，白名单要按**产生原因**分类，而不是按「看着像不像坏」临时加。

| 来源 | 特征 | 误报落在 | 处理 |
|---|---|---|---|
| CDN / CNAME 链 | 长 QNAME、多标签、一批新子域 | #2、#3、长标签粗筛 | 按已知域后缀与已知源白名单，且**定期复审** |
| 健康探测 / 监控 | 固定高频、周期性、名字可枚举 | #2 单源 qps | 按源 + 探测名字精确放行，不整段放行该源 |
| 证书续期（ACME DNS-01） | 突发 `_acme-challenge` TXT、标签较长 | #3、payload 粗筛 | 按名字形态与时间窗放行（第 30 章） |
| 授权的内部扫描 | 唯一 FQDN 数骤增 | #3 | 授权窗口内静音该源，窗口外照常告警，**留审计记录** |

三条纪律：白名单**尽量窄**（源 + 名字形态 + 时间窗）、**要有到期复审**、变更本身要有审计。把「某个 CDN 域」整段加进白名单，等于给第 29 章描述的隧道开了一条免检通道——攻击者只要把数据编码进这个域的子域标签即可。告警分级也在这时定：critical 只给「用户已受影响」的信号（SERVFAIL 率、bogus 突增、上游全超时），warning 给「可疑但需人工确认」的（隧道可疑度、单源突增），采集链路健康单独分组、走不同接收人；分级不清的后果是全部塞进同一个群，然后集体静音。

## 演练：三条链路都要看见

第 28 章给了故障域隔离的注入清单，这里只做**观测验收**——验证三条链路各自能不能产生可辨认信号。演练前备份配置，凭据用 `<TOKEN>` 且使用记录归档。

### 注入 1：关掉一个上游

```bash
date -u +%Y-%m-%dT%H:%M:%SZ | tee /tmp/drill-anchor.txt
# 教学实验里临时停用某一个上游（做法随部署形态而变，见第 5、14 章）
unbound-control -c /etc/unbound/recursor.conf list_forwards
for i in $(seq 1 12); do dig @192.0.2.53 example.org A +short >/dev/null; sleep 1; done
# ① 层：超时与 SERVFAIL
unbound-control -c /etc/unbound/recursor.conf stats_noreset | grep -E 'num.queries_timed_out|num.SERVFAIL|num.recursivereplies'
# ② 层：consumer 侧看出向消息有去无回（命令随实现而定）；③ 层：出向包与 mark
nft list ruleset | grep -i -E 'counter|mark' | head -20
```

**为什么这么设**：单上游失效是 fail-closed 场景（第 5 章 `forward-first: no`）。验收点不是「会不会 SERVFAIL」，而是**能否把失败定位到出向链路**：① 超时涨、② 出向查询有去无回、③ 看到包出门且 mark 正确，三者齐备才算定位成立。  
**怎么验证**：注入前后两组 `stats_noreset` 相减，`num.queries_timed_out` 应上升；`nft` 对应规则 packets 差分非零；dnstap 里应能检出应答变长或缺失的 resolver / forwarder 消息。  
**失败模式 / 回滚点**：多上游时「关一个」未必产生可观测变化（竞速取最快，第 4 章「单上游不是冗余」的含义），演练要在单上游实验实例做。恢复后负缓存可能继续作祟（第 7 章），按控制面 → 权威 → 递归 → 客户端逐层刷，且**链路上每个实例都要刷**。

### 注入 2：注一批高熵 QNAME

```bash
# 教学用高熵标签，域名均为示例域
for i in $(seq -w 1 80); do dig @192.0.2.53 "x${i}a9f3c7e1b.tunnel.example.net" A +short >/dev/null; done
/usr/local/sbin/dnstap-json-inspect.sh | head -20          # ② 层：唯一 FQDN 数
# ① 层：命中率是否下滑、递归数是否抬升
unbound-control -c /etc/unbound/recursor.conf stats_noreset | grep -E 'total.num.cachehits|total.num.cachemiss'
bpftrace -l 'kprobe:udp*' | head -5                        # ③ 层：按 PID 归因有发包
```

**为什么这么设**：80 个互不相同、标签高熵的子域不改变总 qps 多少，却把「按域唯一 FQDN 数」抬高——这正是第 29 章流量分析比 payload 分析更通用的原因。① 层只会有轻微命中率下滑；若 ① 毫无变化而告警全响在 ② 层，说明分层是对的。  
**怎么验证**：巡检脚本 ① 里 `tunnel.example.net` 计数应接近 80；表中 #3 应进入 pending/firing；「隧道可疑度」面板在同一 UTC 时间窗出现凸起。  
**失败模式 / 回滚点**：consumer 若做了采样，唯一 FQDN 数会明显小于 80——这正是反对采集端采样的实证；NXDOMAIN 会被负缓存按 SOA minimum 缓存（第 7 章），演练后刷缓存并把该域加进**演练白名单**而非永久白名单。

### 注入 3：刷掉 nft counter

```bash
# 记录基线（只读）
nft list ruleset | grep -i -E 'counter|packets' | head -20 | tee /tmp/nft-before.txt
nft reset counters                         # 注入：清零计数，会破坏基线，故意做
dig @192.0.2.53 www.example.org A +short >/dev/null   # 制造一次可观测流量
nft list ruleset | grep -i -E 'counter|packets' | head -20 | tee /tmp/nft-after.txt
diff -u /tmp/nft-before.txt /tmp/nft-after.txt || true
```

**为什么这么设**：这一条演练的是**观测本身被重置**。`nft reset counters` 是第 14 章明确警告过的动作，清零后所有基于「累计计数差分」的判断短暂失真；③ 层若没有独立帧计数或 eBPF 计数兜底，就会出现「明明有流量，计数器看起来没动」的假象。  
**怎么验证**：`diff` 应显示计数清零后重新累积；同时 ① 层 `total.num.queries` 仍递增、② 层仍能查到 `www.example.org`，证明另外两条链路不受 nft 计数重置影响。  
**失败模式 / 回滚点**：生产环境不要随意 `reset counters`；演练前 `nft list ruleset > /etc/nftables.conf.bak`（第 14 章），恢复用整体 `nft -f` 而非逐条删。临时文件 `/tmp/nft-*.txt`、`/tmp/drill-anchor.txt` 演练后删除。

### 验收矩阵

| 注入 | ① 应用内 | ② 查询流 | ③ 内核 / 网络 | 合格标准 |
|---|---|---|---|---|
| 关一个上游 | 超时数、SERVFAIL 抬升 | 出向消息有去无回 | 出向包与 mark 可见 | 能定位到出向链路，不是「重启再试」 |
| 高熵 QNAME | 命中率轻微下滑 | 唯一 FQDN 数 ≈ 80、长标签候选 | 按 PID 归因有发包 | 隧道可疑度告警可复现，白名单不误吞 |
| 刷 nft counter | 查询计数仍递增 | QNAME 明细仍在 | 计数清零后重新累积 | 另外两层不受影响，能识别「观测被重置」 |

三项都通过，才可以说这套栈「能观测」；只通过一项，说明装的是仪表盘，不是可观测性。演练记录连同 UTC 锚点、阈值调整依据、白名单变更归档，作为下一轮 7 天基线的输入。

## 与其他章的交叉引用

- 第 7 章：采集点、`stats_noreset` 纪律、`log-servfail`、dnstap 配置与 **30 天保留期**，本章沿用不重复；第 9 章：`access-control` / `ratelimit` 计数是 unwanted / 限速命中指标来源（官方标 experimental，阈值按基线调）。
- 第 14、22 章：出向收敛、tproxy mark、`meta skuid` 豁免；第 26 章：anycast 集中观测与 per-node 差异落在 `node` / `role` label 与跨主机日志检索。
- 第 28 章：故障域隔离的注入清单；第 29 章：唯一 FQDN 数、长 QNAME / 高熵标签、单源 qps 的定义与误报来源；第 30 章：`_acme-challenge` 等记录是证书续期突发查询的合法来源，白名单按名字形态写。

## 本章要点

- **三层互补，不靠一层包打天下**：① `stats_noreset` / `log-servfail` 给累积计数与 SERVFAIL 原因；② dnstap 给 QNAME、单源速率、唯一 FQDN 数；③ eBPF / `ss` / `conntrack` / nft counter 给「包有没有出门、被谁发的、mark 走到哪」。采集点见第 7 章。
- **两条硬边界**：**Unbound 不原生支持 eBPF**，eBPF 是操作系统侧能力、探针名随内核版本变化；**查询日志本身就是全量查询流副本**，接进 Loki / Elasticsearch 后保留期要跟着索引、副本、快照一起对齐 30 天，不把 dnstap 数据无条件推第三方 SaaS。
- 集成形态：`unbound_exporter` / stats → Prometheus → Grafana（qps / SERVFAIL 率 / 命中率 / 上游 RTT / 隧道可疑度 + 采集链路健康 + 日志卷余量），dnstap consumer → JSON → Loki / Elasticsearch → 告警，或轻量落文件 + `jq` 巡检；指标名随所选实现而变，只给骨架表达式。
- **告警靠基线与白名单治理**：先测 7 天基线再定阈值；八条规则覆盖 SERVFAIL 率连续 5 分钟超阈、单源 qps、唯一 FQDN 数、上游全超时、DNSSEC bogus 突增、dnstap 断流、磁盘将满、采集器无数据；CDN / 健康探测 / 证书续期会产生合法长 QNAME 与突发查询，白名单要窄、要复审、要有审计（呼应第 29 章）。
- **日志关联的前提是 NTP**：`verbosity` 平时 1、排障临时 2–3 再调回，`log-servfail` 常开；UTC 锚点 + 同源时钟，才能把指标采样、dnstap 事件、nft 计数差分拼成同一条因果链。
- **验收靠注入，不靠声明**：关一个上游、注一批高熵 QNAME、刷掉 nft counter，逐项确认三条链路都产生可辨认信号且互不受牵连；第 26 章 per-node 集中观测与第 28 章注入清单只作交叉引用。

## 参考资料

- Unbound 官方手册，Monitoring and Reporting（`extended-statistics`、`statistics-interval`、按需统计）：https://unbound.docs.nlnetlabs.nl/en/latest/topics/core/monitoring.html
- Unbound 官方手册，`unbound-control(8)` 的 `stats_noreset`、`get_option`、`set_option`、`verbosity`、`list_forwards`：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html
- Unbound 官方手册，`unbound.conf(5)`（`verbosity`、`log-servfail`、`dnstap` 各节；配置块见第 7 章）：https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html
- dnstap 官方站点（消息类型、帧格式与字段定义以此为准，本书「四类」为归纳）：https://dnstap.info/
- `unbound_exporter` 社区实现（stats → `/metrics` 的一种形态，指标名以实际输出为准）：https://github.com/kumina/unbound_exporter
- Prometheus 官方文档，Alerting rules（`for`、`expr`、规则文件校验）：https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/
- Grafana Loki 官方文档（结构化日志侧聚合、保留期与 LogQL）：https://grafana.com/docs/loki/latest/
- bpftrace 官方参考手册（探针列表、内置变量 `pid` / `uid` / `comm`、内核版本差异）：https://github.com/iovisor/bpftrace/blob/master/docs/reference_guide.md

