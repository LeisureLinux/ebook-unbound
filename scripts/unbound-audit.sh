#!/usr/bin/env bash
# unbound-audit.sh — 检查一份 unbound.conf 是否违反《DNS的加密和隐私防护——从如何加固 Unbound 说起》的基线
#
# 用法:
#   scripts/unbound-audit.sh /etc/unbound/unbound.conf
#   scripts/unbound-audit.sh --strict /etc/unbound/unbound.conf   # 有 WARN 也返回非 0
#
# 这是静态检查，不连网络、不改配置。退出码: 0=通过(可有 WARN), 1=有 FAIL, 2=参数错误。
set -uo pipefail

STRICT=0
if [ "${1:-}" = "--strict" ]; then STRICT=1; shift; fi
CONF="${1:-}"
if [ -z "$CONF" ] || [ ! -r "$CONF" ]; then
  echo "用法: $0 [--strict] <unbound.conf>" >&2
  exit 2
fi

PASS=0; WARN=0; FAIL=0
say(){ printf '  [%-4s] %-46s %s\n' "$1" "$2" "$3"; }
ok(){ PASS=$((PASS+1)); say PASS "$1" "$2"; }
warn(){ WARN=$((WARN+1)); say WARN "$1" "$2"; }
fail(){ FAIL=$((FAIL+1)); say FAIL "$1" "$2"; }

# 去掉注释后的有效行（简单处理；unbound 只有 # 注释）
body(){ sed 's/[[:space:]]*#.*$//' "$CONF" | sed '/^[[:space:]]*$/d'; }
B="$(body)"
has(){ printf '%s\n' "$B" | grep -qE "$1"; }
val(){ printf '%s\n' "$B" | grep -E "$1" | tail -1 | sed -E 's/^[^:]*:[[:space:]]*([^[:space:]]+).*/\1/'; }
count(){ printf '%s\n' "$B" | grep -cE "$1"; }

echo "== unbound-audit: $CONF =="
echo "   规则来源: 本书第 4–13 章（加固基线）"
echo

# ── 1. 转发器与验证器互斥 ──
FZ=$(count '^[[:space:]]*forward-zone:')
TA=$(count '^[[:space:]]*auto-trust-anchor-file:')
if [ "$FZ" -gt 0 ] && [ "$TA" -gt 0 ]; then
  fail "forward-zone + auto-trust-anchor 互斥" \
       "转发器全量上游时无法 priming DNSKEY → 间歇性全量 SERVFAIL（见第 5 章）；二选一"
elif [ "$FZ" -gt 0 ]; then
  ok "转发器未配 trust anchor" "由上游负责验证"
else
  ok "无 forward-zone（递归/验证模式）" "trust anchor 可正常 priming"
fi

# ── 2. 单上游断言 ──
FA=$(count '^[[:space:]]*forward-addr:')
if [ "$FZ" -gt 0 ]; then
  if [ "$FA" -eq 1 ]; then ok "forward-addr 恰好 1 条" "unbound 多上游是并发竞速，不是主备"
  elif [ "$FA" -eq 0 ]; then fail "forward-zone 无 forward-addr" "转发器不会工作"
  else fail "forward-addr 有 $FA 条" "会竞速取最快，绕过既定上游与过滤表"
  fi
  if has '^[[:space:]]*forward-first:[[:space:]]*yes'; then
    fail "forward-first: yes" "上游失败会退回明文递归（静默降级），应 no"
  else ok "forward-first 未放行明文回退" "fail-closed"; fi
else
  ok "非转发器，跳过单上游检查" "—"
fi

# ── 3. EDNS / MTU ──
E=$(val '^[[:space:]]*edns-buffer-size:')
if [ -z "$E" ]; then warn "edns-buffer-size 未显式设置" "官方默认 1232，建议显式写明"
elif [ "$E" -le 1232 ]; then ok "edns-buffer-size=$E" "符合 DNS Flag Day 2020"
else fail "edns-buffer-size=$E" "MTU 1280(WG/IPv6) 与 PPPoE 1492 会分片丢包 → 等超时重试 TCP"; fi

# ── 4. QMIN ──
Q=$(val '^[[:space:]]*qname-minimisation:')
QS=$(val '^[[:space:]]*qname-minimisation-strict:')
if [ "$Q" = "no" ]; then fail "qname-minimisation: no" "把完整 QNAME 送给每一跳；应 yes"
elif [ "$Q" = "yes" ] || [ -z "$Q" ]; then ok "QNAME minimisation 开启（或默认）" "少说话"; fi
if [ "$QS" = "yes" ]; then fail "qname-minimisation-strict: yes" \
  "违反 RFC 8020 的 CDN 区会让整站资源解析失败（第 13 章实测）；应 no"
else ok "QMIN strict 未开启" "允许失败时回退完整域名"; fi

# ── 5. DNSSEC 验证 ──
if [ "$TA" -gt 0 ]; then
  V=$(val '^[[:space:]]*val-permissive-mode:')
  if [ "$V" = "yes" ]; then fail "val-permissive-mode: yes" "坏签名只记日志仍放行，等于不验证"
  else ok "val-permissive-mode=no（或默认）" "真验证"; fi
  for k in harden-dnssec-stripped harden-below-nxdomain harden-glue; do
    v=$(val "^[[:space:]]*$k:")
    if [ "$v" = "no" ]; then
      if [ "$k" = "harden-glue" ]; then fail "$k: no" "与 DNSSEC 无关，域外 glue 是缓存投毒面，任何模式都该开"
      elif [ "$k" = "harden-dnssec-stripped" ]; then fail "$k: no" "有锚时会被剥离签名降级攻击"
      else warn "$k: no" "仅对 DNSSEC-secure NXDOMAIN 生效；无验证器时本就是空转"
      fi
    elif [ -z "$v" ]; then ok "$k 用默认 yes" "—"
    else ok "$k: yes" "—"; fi
  done
else
  ok "无 trust anchor，跳过 DNSSEC 检查" "转发器由上游验证；若本意是验证器请补 auto-trust-anchor-file"
fi

# ── 6. serve-expired 单位 ──
T=$(val '^[[:space:]]*serve-expired-client-timeout:')
if [ -n "$T" ] && [ "$T" -gt 10000 ] 2>/dev/null; then
  fail "serve-expired-client-timeout=$T" "单位是毫秒；>10000 会让客户端等 10 秒以上才回过期缓存"
else ok "serve-expired-client-timeout 合理" "${T:-默认 1800ms}"; fi

# ── 7. 暴露面 ──
if has '^[[:space:]]*interface:[[:space:]]*0\.0\.0\.0([@[:space:]]|$)'; then
  warn "interface 绑定 0.0.0.0" "确认这不是公网可达的开放解析器（第 9 章）"
else ok "interface 未全绑 0.0.0.0" "—"; fi
if has '^[[:space:]]*access-control:[[:space:]]*0\.0\.0\.0/0[[:space:]]+allow'; then
  fail "access-control 0.0.0.0/0 allow" "开放递归解析器，会被用于放大攻击"
else ok "无 0.0.0.0/0 allow" "—"; fi
for k in hide-identity hide-version; do
  if has "^[[:space:]]*$k:[[:space:]]*yes"; then ok "$k: yes" "—"
  else warn "$k 未开启" "会暴露版本信息"; fi
done

# ── 8. DDR 压制与 dnstap ──
if has '_dns\.resolver\.arpa.*always_nodata'; then ok "DDR 已压制" "客户端不会自动发现 DoH 绕过本地解析器"
else warn "未压制 _dns.resolver.arpa" "第 13 章：客户端可能自动升级加密 DNS 绕过本地策略"; fi
if has '^[[:space:]]*dnstap-enable:[[:space:]]*yes'; then
  warn "dnstap 全量查询日志开启" "确认 dashboard 的 --ttl 保留期（本书用 30 天）与访问范围"
fi

echo
printf '  合计: PASS=%d  WARN=%d  FAIL=%d\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
if [ "$STRICT" = 1 ] && [ "$WARN" -gt 0 ]; then exit 1; fi
exit 0
