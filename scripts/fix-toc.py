#!/usr/bin/env python3
"""fix-toc.py — 把 Pandoc 生成的 ePub nav / HTML TOC 改成本书要求的三层格式：

    第X篇 该篇标题 XXX
    第Y章 该章描述 YYY
    Y.n 该小节描述 nnnn

背景：正文里原有 304 处手写的 `## 1.1` / `### 1.1.1` 前缀，叠加 Pandoc 的
--number-sections 后目录里小节号重复。现已统一剥掉手写前缀，由 Pandoc 编
`1 / 1.1 / 1.1.1`；本脚本把章级裸数字改成「第 N 章」，并把前言的 `0.x`
小节号去掉（前言不参与编号）。

用法:
    python3 scripts/fix-toc.py dist/unbound-dns-hardening.epub dist/unbound-dns-hardening.html
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

# 章级：<span class="toc-section-number">1</span> → 第 1 章
# 前言级：0.x → 去掉整个 span
# 小节级：1.1 / 1.1.1 → 原样保留
SPAN = re.compile(r'<span class="(?:toc-section-number|section-header-number)">([^<]*)</span>')


def fix_span(text: str) -> tuple[str, int]:
    n = 0

    def repl(m: re.Match) -> str:
        nonlocal n
        v = m.group(1).strip()
        if v.startswith("0."):            # 前言小节，不显示编号
            return ""
        if "." in v:                      # 小节 1.1 / 1.1.1 原样
            return m.group(0)
        if v.isdigit():
            n += 1
            return f'<span class="toc-section-number">第 {v} 章</span>'
        return m.group(0)

    return SPAN.sub(repl, text), n


def fix_html_toc(path: Path) -> int:
    t = path.read_text(encoding="utf-8")
    nt, n = fix_span(t)
    path.write_text(nt, encoding="utf-8")
    return n


def fix_epub(path: Path) -> int:
    """Pandoc 的 ePub nav.xhtml 不带编号；按 nav 的顶层 <li> 顺序补「第 N 章」。"""
    with zipfile.ZipFile(path, "r") as z:
        names = z.namelist()
        target = next((n for n in names if n.endswith("nav.xhtml")), None)
        if not target:
            print(f"  ⚠️  {path.name} 里没有 nav.xhtml")
            return 0
        data = {n: z.read(n) for n in names}
    nav = data[target].decode("utf-8")
    # 只改 nav 的最外层 <ol class="toc"> 下的顶层 <li>：它们是「前言 / 篇 / 章 / 附录」
    # 用计数器按顶层 li 的 <a> 文本判断：纯标题（无编号）且属于章文件 → 补章号。
    # 简化且稳健的做法：按 chNNN.xhtml 的序号推导章号，跳过篇标题与前言。
    ch = 0
    out = []
    for m in re.finditer(r'(<a href="text/ch\d+\.xhtml"[^>]*>)([^<]*)(</a>)', nav):
        pass  # 占位：下面用 sub 一次性处理
    def repl(m: re.Match) -> str:
        nonlocal ch
        head, label, tail = m.group(1), m.group(2), m.group(3)
        if label.startswith("第") or label in ("前言", "版本信息") or "篇" in label or "附录" in label:
            return m.group(0)
        if not label.strip():              # 章标题为空时 Pandoc 放在 span 里，这里跳过
            return m.group(0)
        ch += 1
        return f'{head}第 {ch} 章 {label}{tail}'
    new = re.sub(r'(<a href="text/ch\d+\.xhtml"[^>]*>)([^<]*)(</a>)', repl, nav)
    # 若 nav 本身已有 span 编号，也跑一遍 fix_span
    new, n2 = fix_span(new)
    n = ch + n2
    data[target] = new.encode("utf-8")
    tmp = path.with_suffix(".epub.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            z.writestr(name, data[name])
    tmp.replace(path)
    return n


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: fix-toc.py <file.epub|file.html> ...", file=sys.stderr)
        return 2
    total = 0
    for a in sys.argv[1:]:
        p = Path(a)
        if not p.exists():
            print(f"  ⚠️  不存在: {p}")
            continue
        if p.suffix == ".epub":
            n = fix_epub(p)
            print(f"  {p.name}: nav.xhtml 补章号 {n} 处")
        else:
            n = fix_html_toc(p)
            print(f"  {p.name}: TOC 改写 {n} 处章号")
        total += n
    print(f"  合计 {total} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
