#!/usr/bin/env python3
"""check-redaction.py — 公开 CC 电子书的脱敏红线自检。

扫描 book/src/*.md，若出现真实环境的公网 IP / 域名 / token / 主机名，
返回非 0。与《写手共享规范》§1 一致。用法：

    python3 scripts/check-redaction.py
    python3 scripts/check-redaction.py --verbose
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "book" / "src"

# (标签, 正则, 说明)
RULES: list[tuple[str, str, str]] = [
    ("真实域名", r"(?i)\bfreelamp\.com\b", "业务域名"),
    ("真实 DDNS", r"(?i)\b(static-qrfan\w*|doorknob\w*|cool-feather-\w*|diluted-oat8-\w*)\b", "DDNS / 站点名"),
    ("真实 IPv4", r"\b(43\.135\.161\.\d+|124\.222\.231\.\d+|117\.185\.174\.\d+|223\.5\.5\.5|119\.29\.29\.29)\b", "公网 IP"),
    ("真实 v6 前缀", r"(?i)\b(2409:8a1e|240d:c000|2402:4e00|2404:6800)\b", "运营商 / 云 v6 前缀"),
    ("真实主机名", r"(?i)\b(wpad-02|wpad\.lan|mail2|mail3|happydl\.lan)\b", "内部主机名"),
    ("token", r"\b[0-9a-f]{40,64}\b", "疑似 API token / webhook key"),
    ("代理端口遗留", r"(?i)\bhttps?://wpad(?:\.lan|-02)[:\d]*\b", "旧代理地址"),
]

# 允许出现的文档地址段（RFC 5737 / RFC 3849）
ALLOWED = re.compile(r"\b(192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+|2001:db8:[0-9a-f:]*)\b", re.I)


def main() -> int:
    verbose = "--verbose" in sys.argv
    files = sorted(SRC.glob("*.md"))
    if not files:
        print("  ⚠️  book/src/ 里没有 md", file=sys.stderr)
        return 2

    hits: list[tuple[str, int, str, str]] = []
    for f in files:
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            # 去掉明确标注为文档段的地址后，再匹配红线
            cleaned = ALLOWED.sub("DOCADDR", line)
            for label, pattern, desc in RULES:
                m = re.search(pattern, cleaned)
                if m:
                    hits.append((f.name, lineno, label, m.group(0)[:60]))

    print(f"  扫描 {len(files)} 个章节文件 · {len(RULES)} 条红线规则")
    if hits:
        print(f"  ❌ 发现 {len(hits)} 处疑似真实信息：")
        for name, ln, label, frag in hits[:60]:
            print(f"     {name}:{ln}  [{label}]  {frag}")
        return 1

    print("  ✅ 脱敏检查通过：未发现真实公网 IP / 域名 / token / 主机名")
    if verbose:
        for f in files:
            n = len(f.read_text(encoding="utf-8").splitlines())
            print(f"     {f.name:52s} {n:5d} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
