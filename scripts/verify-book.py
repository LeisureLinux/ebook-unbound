#!/usr/bin/env python3
"""verify-book.py — 《Unbound DNS 服务加固指南》全书验收。

检查：
  1. 24 章 + 前言 + 版本信息齐全，文件名与 OUTLINE 一致；
  2. 每章纯标题（无「第 N 章」前缀）、有「本章要点」与「参考资料」；
  3. 无 ####、代码围栏成对、无禁用术语；
  4. 脱敏红线（调用 check-redaction.py）；
  5. 三种构建产物存在且非空。

用法: python3 scripts/verify-book.py [--skip-artifacts]
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "book" / "src"
DIST = ROOT / "dist"

EXPECT = {
    "01": "history", "02": "dns-landscape", "03": "three-roles",
    "04": "caching-resolver", "05": "forwarder", "06": "recursive-root-mirror",
    "07": "multi-instance-observability", "08": "threat-model-nist",
    "09": "minimal-exposure", "10": "dnssec", "11": "dot", "12": "doh",
    "13": "query-privacy", "14": "encrypted-transport-routing",
    "15": "rfc-dns-basics", "16": "rfc-dnssec", "17": "rfc-privacy",
    "18": "rfc-ipv6-implementations", "19": "ipv6-deployment",
    "20": "kubernetes-dns", "21": "lab-topology", "22": "failure-cases", "23": "wpad", "24": "adhole",
}
BAD_TERMS = {"陈旧数据": "过期缓存应答", "粘合记录": "胶水记录",
             "授权服务器": "权威服务器", "Q名": "查询名"}
problems: list[str] = []
info: list[str] = []


def main() -> int:
    # 1. 完整性
    missing = []
    for n, slug in EXPECT.items():
        f = SRC / f"{n}-{slug}.md"
        if not f.exists():
            # 也接受任意 slug 的同号文件
            alt = list(SRC.glob(f"{n}-*.md"))
            if not alt:
                missing.append(f"{n}-{slug}.md")
    if not (SRC / "00-preface.md").exists():
        missing.append("00-preface.md")
    if not (SRC / "99-colophon.md").exists():
        missing.append("99-colophon.md")
    if missing:
        problems.append(f"缺文件: {', '.join(missing)}")

    chapters = [f for f in sorted(SRC.glob("*.md"))
               if re.match(r"^(?:0[1-9]|1\d|2[0-4])-", f.name)]
    info.append(f"章节文件 {len(chapters)}/24，加前言与版本信息共 {len(list(SRC.glob('*.md')))} 个 md")

    total_lines = 0
    for f in chapters:
        t = f.read_text(encoding="utf-8")
        lines = t.splitlines()
        total_lines += len(lines)
        if not lines or not lines[0].startswith("# "):
            problems.append(f"{f.name}: 首行不是 # 标题")
        elif re.match(r"^#\s+第\s*\d+\s*章", lines[0]):
            problems.append(f"{f.name}: 仍带「第 N 章」前缀")
        if "## 本章要点" not in t:
            problems.append(f"{f.name}: 缺「## 本章要点」")
        if "## 参考资料" not in t:
            problems.append(f"{f.name}: 缺「## 参考资料」")
        if re.search(r"^####\s", t, re.M):
            problems.append(f"{f.name}: 出现 ####")
        if t.count("```") % 2:
            problems.append(f"{f.name}: 代码围栏不成对")
        for k, v in BAD_TERMS.items():
            if k in t:
                problems.append(f"{f.name}: 禁用术语「{k}」→ 应用「{v}」")
        if len(lines) < 150:
            problems.append(f"{f.name}: 仅 {len(lines)} 行，偏短")
    info.append(f"正文字数规模: {total_lines} 行")

    # 2. 脱敏
    rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "check-redaction.py")],
                        cwd=ROOT, capture_output=True, text=True)
    print("  " + rc.stdout.strip().replace("\n", "\n  "))
    if rc.returncode:
        problems.append("脱敏检查未通过")

    # 3. 构建产物
    if "--skip-artifacts" not in sys.argv:
        for name in ("unbound-dns-hardening.pdf", "unbound-dns-hardening.epub",
                     "unbound-dns-hardening.html"):
            p = DIST / name
            if not p.exists() or p.stat().st_size < 10_000:
                problems.append(f"构建产物缺失或过小: {name}")
            else:
                info.append(f"{name}: {p.stat().st_size // 1024} KB")
    else:
        info.append("(--skip-artifacts，跳过产物检查)")

    print("\n  ── 验收摘要 ──")
    for x in info:
        print(f"     {x}")
    if problems:
        print(f"\n  ❌ 未通过，{len(problems)} 个问题：")
        for p in problems[:50]:
            print(f"     - {p}")
        return 1
    print("\n  ✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
