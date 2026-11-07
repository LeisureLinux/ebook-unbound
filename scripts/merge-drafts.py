#!/usr/bin/env python3
"""merge-drafts.py — 把 resources/drafts/{A..F} 的章节合并进 book/src/。

做的事：
  1. 按文件名收集 01..22 章，校验 22 章齐全、文件名无重复；
  2. 统一标题：把 `# 第 N 章 标题` 改成 `# 标题`（Pandoc --number-sections 会再编号）；
  3. 校验每章有「本章要点」与「参考资料」；
  4. 校验最多到 ###（不出现 ####）；
  5. 写入 book/src/ 后调用 check-redaction.py 做脱敏自检。

用法:
    python3 scripts/merge-drafts.py            # 合并
    python3 scripts/merge-drafts.py --dry-run  # 只报告，不写文件
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAFTS = ROOT / "resources" / "drafts"
SRC = ROOT / "book" / "src"
LANES = "ABCDEF"

EXPECTED = {
    "01": "history", "02": "dns-landscape", "03": "three-roles",
    "04": "caching-resolver", "05": "forwarder", "06": "recursive-root-mirror",
    "07": "multi-instance-observability", "08": "threat-model-nist",
    "09": "minimal-exposure", "10": "dnssec", "11": "dot", "12": "doh",
    "13": "query-privacy", "14": "encrypted-transport-routing",
    "15": "rfc-dns-basics", "16": "rfc-dnssec", "17": "rfc-privacy",
    "18": "rfc-ipv6-implementations", "19": "ipv6-deployment",
    "20": "kubernetes-dns", "21": "lab-topology", "22": "failure-cases",
}


def main() -> int:
    dry = "--dry-run" in sys.argv
    found: dict[str, Path] = {}
    dupes: list[str] = []
    # 正常分派落在 drafts/{A..F}/；超时 lane 的补写落在 drafts2/
    roots = [DRAFTS / lane for lane in LANES] + [ROOT / "resources" / "drafts2"]
    for d in roots:
        for f in sorted(d.glob("*.md")):
            m = re.match(r"^(\d{2})-", f.name)
            if not m:
                print(f"  ⚠️  忽略不合命名文件: {f.relative_to(ROOT)}")
                continue
            n = m.group(1)
            if n in found:
                dupes.append(f"{n}: {found[n].name} / {f.name}")
            found[n] = f

    print(f"  找到 {len(found)}/22 章")
    missing = [n for n in EXPECTED if n not in found]
    if missing:
        print(f"  ❌ 缺章节: {', '.join(missing)}")
    if dupes:
        print("  ❌ 重名章节:")
        for d in dupes:
            print(f"     {d}")
    if missing or dupes:
        return 1

    problems: list[str] = []
    for n, f in sorted(found.items()):
        text = f.read_text(encoding="utf-8")
        lines = text.splitlines()
        # 标题统一（只改第一行）
        first = lines[0] if lines else ""
        m = re.match(r"^#\s+第\s*\d+\s*章\s+(.+)$", first)
        if m:
            lines[0] = "# " + m.group(1).strip()
            text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
            print(f"  [title] {n}: 去掉章号前缀 → {lines[0]}")
        elif not first.startswith("# "):
            problems.append(f"{n}: 第一行为「{first[:40]}」，不是 # 标题")

        if "## 本章要点" not in text:
            problems.append(f"{n}: 缺「## 本章要点」")
        if "## 参考资料" not in text:
            problems.append(f"{n}: 缺「## 参考资料」")
        if re.search(r"^####\s", text, re.M):
            problems.append(f"{n}: 出现 ####（参考仓库踩过编号错乱的坑）")
        n_lines = len(lines)
        if n_lines < 150:
            problems.append(f"{n}: 只有 {n_lines} 行，偏短（目标 250-500）")
        print(f"  [{n}] {n_lines:4d} 行  {f.relative_to(ROOT)}")

    if problems:
        print("  ⚠️  格式问题:")
        for p in problems:
            print(f"     {p}")

    if dry:
        print("  (--dry-run，未写文件)")
        return 0

    for n, f in sorted(found.items()):
        text = f.read_text(encoding="utf-8")
        lines = text.splitlines()
        m = re.match(r"^#\s+第\s*\d+\s*章\s+(.+)$", lines[0])
        if m:
            lines[0] = "# " + m.group(1).strip()
            text = "\n".join(lines) + "\n"
        # 用规范文件名落盘（按 OUTLINE 的 slug）
        target = SRC / f"{n}-{EXPECTED[n]}.md"
        target.write_text(text, encoding="utf-8")
    print(f"  ✅ 已写入 {len(found)} 章到 book/src/")

    print("  ── 脱敏自检 ──")
    rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "check-redaction.py")], cwd=ROOT).returncode
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
