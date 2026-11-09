#!/usr/bin/env python3
"""build-weread-epub.py — 生成「微信读书专用版」ePub（方向 A）。

从常规 ePub 派生，只做两处针对性处理，其余文件原样保留：

  1. 代码块：微信读书的排版引擎忽略 `white-space: pre-wrap` / `overflow-wrap`，
     也不提供横向滚动，长行会被截断。做法（含一处明确取舍）：
       - 剥掉 Pandoc 的语法高亮 span / 行号锚点（微信读书本身也不着色），
         只保留纯文本；
       - 按固定宽度（默认 64 字符）在**纯文本层**插入硬换行，
         不依赖任何 CSS；
       - 缩进用不换行空格保留，避免被折叠；
       - **只在空白或「后跟空格的标点」处断行**；找不到安全断点的整行原样
         保留（如 216 字符的长正则、URL、路径），宁可让极少数行溢出，也不
         把 shell 命令撕碎——技术书里的代码必须保持可复制；
       - ASCII 示意图靠列对齐表达结构，整块跳过折行；
       - 专用 CSS 把 pre 字号压到 0.72em，帮助窄屏容纳这些不可断行。
  2. 目录：微信读书把 nav.xhtml 里的 <ol>/<li> 当正文列表重新套自己的
     项目符号，忽略 `list-style: none`。做法：把 nav 里的 ol/li 全部换成
     <div>，缩进层级用 class 表达；toc.ncx 保持原样（它才是目录树来源）。

用法:
  python3 scripts/build-weread-epub.py <input.epub> <output.epub> [--width 64]

参考:
  - 微信读书团队《微信读书排版引擎自动化测试方案》(2016)：排版引擎自行解析
    EPUB/TXT，中间数据为 NSAttributedString，不是标准 WebView。
  - qiaomu-epub-book-generator / weread-clipper：面向微信读书的生成器都用
    极简 HTML、少依赖 CSS。
  - Yu's Cyber Desk 实测：pre-wrap 会破坏代码缩进；pre + 横向滚动才稳，
    但 EPUB 阅读器没有横向滚动，故改为硬换行。
"""
from __future__ import annotations

import html as html_mod
import re
import sys
import zipfile
from pathlib import Path

WIDTH = 56
NBSP = "\u00a0"


def strip_highlight(inner: str) -> str:
    """把 <pre> 内部还原成纯文本：去掉行号锚点、语法高亮 span、<code> 包装。"""
    # 去掉行号/锚点 <a ...></a>
    s = re.sub(r"<a\b[^>]*>\s*</a>", "", inner)
    # 去掉所有标签，但保留换行结构（span 之间本身有 \n）
    s = re.sub(r"<[^>]+>", "", s)
    return html_mod.unescape(s)


def is_ascii_diagram(text: str) -> bool:
    """判断是不是靠列对齐的 ASCII 示意图（这类不折行）。"""
    lines = text.split("\n")
    if len(lines) < 4:
        return False
    aligned = sum(
        1 for l in lines
        if re.search(r"[|+\-]{3,}", l) or re.match(r"^\s*[v^]\s*$", l)
        or re.search(r"\[[^]]+\]\s*-{2,}", l)
    )
    return aligned >= 3


def hard_wrap(text: str, width: int) -> str:
    """按显示宽度折行，但**只在安全的空白/标点后断**。

    找不到安全断点就保留整行不折（宁可让个别超长行溢出，也不把 shell 命令、
    正则、URL 从中间撕碎——技术书里的代码必须保持可复制）。
    行首缩进转成不换行空格，避免被折叠。
    """
    out: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"^[ \t]+", line)
        indent = m.group(0).replace("\t", "    ") if m else ""
        body = line[len(indent):]
        prefix = NBSP * len(indent)
        avail = width - len(indent)
        if len(body) <= avail:
            out.append(prefix + body)
            continue
        # 逐段切：只在「空白或标点之后」断，且断点必须落在 avail 的前 60%~100%
        rest = body
        first = True
        while rest:
            take = width - (len(indent) if first else 2)
            if len(rest) <= take:
                out.append((prefix if first else NBSP * 2) + rest)
                break
            lo = max(int(take * 0.55), 12)
            cut = -1
            # 优先在空白后断；其次在标点后断
            for i in range(take, lo, -1):
                if rest[i - 1] in " \t":
                    cut = i
                    break
            if cut < 0:
                # 只在「后面跟空格」的逗号/分号/右括号处断；
                # 路径里的 / 和 .、正则里的 | 和 - 都不算断点（都在 token 内部）。
                for i in range(take, lo, -1):
                    if rest[i - 1] in ",;:)]}" and (i >= len(rest) or rest[i] in " \t"):
                        cut = i
                        break
            if cut < 0:
                # 没有安全断点：整行原样保留，不再折
                out.append((prefix if first else NBSP * 2) + rest)
                break
            out.append((prefix if first else NBSP * 2) + rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
            first = False
    return html_mod.escape("\n".join(out), quote=False)


def fix_pre(xhtml: str, width: int) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        count += 1
        open_tag = m.group(1)
        plain = strip_highlight(m.group(2))
        if is_ascii_diagram(plain):
            # ASCII 示意图靠列对齐表达结构，硬折行会把它拆坏；
            # 这类块本身行数多、单行不算特别长，保持原样。
            body = html_mod.escape(plain, quote=False)
        else:
            body = hard_wrap(plain, width)
        # 保留原 class（便于 CSS 仍能给个底色），但内部只留纯文本
        return f"{open_tag}<code>{body}</code></pre>"

    out = re.sub(r"(<pre\b[^>]*>)(.*?)</pre>", repl, xhtml, flags=re.S)
    return out, count


def fix_nav(nav: str) -> str:
    """把 nav.xhtml 的 ol/li 换成 div，去掉列表语义，避免微信读书套 bullet。"""
    # 只处理 <nav epub:type="toc"> 内部
    i = nav.find("<nav")
    j = nav.find("</nav>", i)
    if i < 0 or j < 0:
        return nav
    head, body, tail = nav[:i], nav[i:j + 6], nav[j + 6:]

    # 层级：用 ol 的嵌套深度给出缩进 class
    # 先把 <ol class="toc">/<ol> 与 </ol> 替换成带 depth 的 div 容器
    depth = 0
    parts = re.split(r"(<[^>]+>)", body)
    out: list[str] = []
    for tok in parts:
        if not tok.startswith("<"):
            out.append(tok)
            continue
        low = tok.lower()
        if re.match(r"<ol\b", low):
            depth += 1
            out.append(f'<div class="toc-level-{depth}">')
        elif low.startswith("</ol"):
            out.append("</div>")
            depth -= 1
        elif re.match(r"<li\b", low):
            out.append('<div class="toc-item">')
        elif low.startswith("</li"):
            out.append("</div>")
        else:
            out.append(tok)
    # 清掉 <nav> 上残留的 epub:type 无影响；保留 h1
    return head + "".join(out) + tail


def main() -> int:
    args = [a for a in sys.argv[1:]]
    width = WIDTH
    if "--width" in args:
        k = args.index("--width")
        width = int(args[k + 1])
        del args[k:k + 2]
    if len(args) != 2:
        print("用法: build-weread-epub.py <input.epub> <output.epub> [--width N]", file=sys.stderr)
        return 2
    src, dst = Path(args[0]), Path(args[1])
    if not src.exists():
        print(f"  ❌ 不存在: {src}")
        return 2

    with zipfile.ZipFile(src, "r") as zin:
        names = zin.namelist()
        data = {n: zin.read(n) for n in names}

    # 注入专用 CSS：pre 只依赖硬换行，不再要求任何折行属性；目录 div 无 bullet
    css = data.get("EPUB/styles/stylesheet1.css", b"").decode("utf-8", "replace")
    css += """

/* ===== 微信读书专用版（build-weread-epub.py 追加） =====
   代码块已在 HTML 层按 %d 字符硬换行，不再依赖 white-space / word-wrap。
   目录已由 ol/li 改为 div，不再依赖 list-style。 */
pre {
  white-space: pre !important;
  overflow-x: visible !important;
  word-wrap: normal !important;
  overflow-wrap: normal !important;
  font-size: 0.72em;  /* 微信读书窄屏：尽量容纳少数不可断的长命令 */
}
pre code { white-space: pre !important; }
.toc-item { margin: 0.25em 0; }
.toc-level-1 > .toc-item { font-weight: bold; }
.toc-level-2 > .toc-item { padding-left: 1.2em; font-weight: normal; }
.toc-level-3 > .toc-item { padding-left: 2.4em; font-size: 0.92em; }
.toc-level-4 > .toc-item { padding-left: 3.6em; font-size: 0.88em; }
nav[epub\\:type="toc"] ol, nav[epub:type="toc"] ol { list-style: none; }
""" % width
    data["EPUB/styles/stylesheet1.css"] = css.encode("utf-8")

    n_pre = 0
    n_files = 0
    for n in list(data):
        if not n.endswith(".xhtml"):
            continue
        t = data[n].decode("utf-8", "replace")
        if n.endswith("nav.xhtml"):
            t = fix_nav(t)
        elif "<pre" in t:
            t, c = fix_pre(t, width)
            n_pre += c
            n_files += 1
        data[n] = t.encode("utf-8")

    # 写回：mimetype 必须 STORED 且第一项
    tmp = dst.with_suffix(".epub.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        if "mimetype" in data:
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, data["mimetype"])
        for n in names:
            if n == "mimetype":
                continue
            z.writestr(n, data[n])
    tmp.replace(dst)
    print(f"  微信读书专用版: {dst.name} ({dst.stat().st_size // 1024} KB)")
    print(f"    处理 {n_files} 个文件 / {n_pre} 个代码块，硬换行宽度 {width}")
    print("    nav.xhtml: ol/li → div；toc.ncx / spine / 其余文件保持原样")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
