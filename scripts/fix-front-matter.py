#!/usr/bin/env python3
"""fix-front-matter.py — 处理 Pandoc 产物的前置页与目录格式。

做的事：
  1. ePub / HTML：清理 title_page.xhtml 里重复的 @LeisureLinux，
     把作者行改成「B站频道，微信公众号： @LeisureLinux」，日期后加版本号。
  2. PDF（通过 build-local.sh / CI 的 --include-before-body 注入的片段无关，
     本脚本处理 LaTeX 侧章号由 latex-header.tex 承担）。
  3. 目录格式（nav.xhtml / toc.ncx / HTML TOC）统一成：
       第N篇　XXX        （篇名前全角空格）
       第X章 XXX         （中文数字，无空格）
       X.n 小节          （原样）
       附录 A / 附录 B / 版本信息 —— 不带「第X章」
  4. 把前置页（图书介绍 / 版权信息 / 作者简介）插到目录里，排在前言之前。

用法:
  python3 scripts/fix-front-matter.py <epub> <html> <version>
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

# 中文数字（章号 1..40）
ZH = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
      "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
      "二十一", "二十二", "二十三", "二十四", "二十五", "二十六", "二十七", "二十八", "二十九", "三十",
      "三十一", "三十二", "三十三", "三十四", "三十五", "三十六", "三十七", "三十八", "三十九", "四十"]

FRONT_PAGES = [
    ("图书介绍", "text/front-about.xhtml"),
    ("版权信息", "text/front-copyright.xhtml"),
    ("作者简介", "text/front-author.xhtml"),
]


def zh(n: int) -> str:
    return ZH[n] if 0 < n < len(ZH) else str(n)


def chapter_label(n: int) -> str:
    return f"第{ZH[n]}章" if 0 < n < len(ZH) else f"第{n}章"


# ──────────────────────────────────────────────────────────────
# 1. title_page.xhtml
# ──────────────────────────────────────────────────────────────
AUTHOR_LINE = "B站频道，微信公众号： @LeisureLinux"


def fix_title_page(xml: str, version: str) -> str:
    # Pandoc 会把 @LeisureLinux 渲染成 <span class="citation" ...>@LeisureLinux</span>，
    # 所以按「整行 <p class="author">...</p>」替换，而不是匹配纯文本。
    xml = re.sub(
        r'<p class="author">.*?</p>\s*<p class="publisher">.*?</p>',
        f'<p class="author">{AUTHOR_LINE}</p>',
        xml, count=1, flags=re.S,
    )
    xml = re.sub(
        r'<p class="author">.*?</p>',
        f'<p class="author">{AUTHOR_LINE}</p>',
        xml, count=1, flags=re.S,
    )
    # 日期后加版本号（metadata.yml 的 date 已带版本时不重复加）
    def add_ver(m: re.Match) -> str:
        cur = m.group(1).strip()
        # 去掉可能已叠加的旧版本号，只保留「年月 · 目标版本」
        cur = re.sub(r"\s*·\s*v?\d+\.\d+\.\d+", "", cur).strip()
        return f"<p class=\"date\">{cur} · {version}</p>"
    xml = re.sub(r'<p class="date">([^<]*)</p>', add_ver, xml)
    return xml


def build_chapter_map(files: list[str]) -> dict[str, tuple[int, str] | None]:
    """从 nav 顶层 li 顺序推导章号：跳过前言、篇标题、附录、版本信息。"""
    out: dict[str, tuple[int, str] | None] = {}
    n = 0
    for f in files:
        m = re.match(r"text/ch(\d+)\.xhtml", f)
        if not m:
            continue
        # 顺序由调用方传入（已按顶层 li 排好），这里只数「第 N 章」候选
        out[f] = None
    return out


def fix_nav(nav: str) -> str:
    """把 nav 顶层章标签改成「第X章」，附录/版本信息去掉章号，篇名加空格。"""
    # 收集顶层 li（不带 ol 子列表的 li 就是顶层；用非贪婪匹配到 </li>）
    # 更稳妥：逐个匹配 <li id="toc-li-N"><a href="text/chNNN.xhtml"...>...</a>，
    # 其后紧跟 </li> 或 <ol 的都是顶层。
    # 不依赖 <li id="toc-li-N">：不同 Pandoc 版本生成的 li 属性不同，
    # 只按「指向 text/chNNN.xhtml 的 <a>」匹配更稳。
    # 只匹配「不带 # 锚点」的 chNNN.xhtml 链接（顶层章）；小节链接都带 #。
    pattern = re.compile(
        r'(<a href="[^"#]*?ch\d+\.xhtml"[^>]*>)(.*?)(</a>)',
        re.S,
    )
    # 先按出现顺序给章编号：从「第一个没有编号的普通章标题」开始
    # 篇标题 / 前言 / 附录 / 版本信息 都不编号
    items = list(pattern.finditer(nav))
    n = 0
    replacements: list[tuple[int, int, str]] = []
    for m in items:
        head, label_html, tail = m.group(1), m.group(2), m.group(3)
        plain = re.sub(r"<[^>]+>", "", label_html).strip()
        # 先剥掉 Pandoc 的章级编号 span（第 1 章 / 裸数字 38），
        # 再剥掉正文里残留的裸章号「1 」（Pandoc 给 unnumbered 章也留了数字）
        body = re.sub(r'<span class="toc-section-number">[^<]*</span>\s*', "", label_html).strip()
        body_plain = re.sub(r"<[^>]+>", "", body).strip()
        body_plain = re.sub(r"^\d+\s+", "", body_plain).strip()

        if re.match(r"^第[一二三四五]篇", body_plain):
            # 篇：确保「第N篇」后有全角空格
            new_label = re.sub(r"^(第[一二三四五]篇)\s*", r"\1　", body_plain)
            replacements.append((m.start(), m.end(), head + new_label + tail))
            continue
        if body_plain in ("前言", "版本信息") or re.match(r"^附录 [AB]", body_plain):
            replacements.append((m.start(), m.end(), head + body_plain + tail))
            continue
        if body_plain.startswith("图书介绍") or body_plain.startswith("版权信息") or body_plain.startswith("作者简介"):
            replacements.append((m.start(), m.end(), head + body_plain + tail))
            continue
        # 普通章
        n += 1
        new_label = f"{chapter_label(n)} {body_plain}"
        replacements.append((m.start(), m.end(), head + new_label + tail))

    for s, e, rep in reversed(replacements):
        nav = nav[:s] + rep + nav[e:]
    if n != 37:
        print(f"  ⚠️ fix_nav: 只识别到 {n} 个章（应为 37），nav 结构可能与预期不同", file=sys.stderr)
    return nav


def fix_ncx(ncx: str) -> str:
    """NSX 顶层 navLabel：章号改中文，附录/版本信息去编号，篇名加空格。"""
    # 顶层 navPoint = <navPoint ...><navLabel><text>...</text>...</navPoint> 中
    # 不含嵌套 navPoint 的；用「<text> 后紧跟 </navLabel> 且下一行是 <content」判断不可靠，
    # 改为按已知模式：顶层标签形如 "N 标题" 或 "第N篇 ..." 或纯标题
    def repl(m: re.Match) -> str:
        label = m.group(1)
        # 小节（X.Y）不动
        if re.match(r"^\d+\.\d", label):
            return m.group(0)
        mm = re.match(r"^(\d+)\s+(.*)$", label)
        if mm:
            num, title = int(mm.group(1)), mm.group(2).strip()
            if num >= 38 or re.match(r"^附录 [AB]", title) or title == "版本信息":
                return f"<text>{title}</text>"
            return f"<text>{chapter_label(num)} {title}</text>"
        if re.match(r"^第[一二三四五]篇", label):
            return f"<text>{re.sub(r'^(第[一二三四五]篇)　?\s*', r'\1　', label)}</text>"
        return m.group(0)

    return re.sub(r"<text>([^<]*)</text>", repl, ncx)


def fix_html_toc(html: str) -> str:
    """HTML TOC 的章级 span：裸数字 1..37 → 第X章；38/39/40 去掉；小节保留 1.1。"""
    def repl(m: re.Match) -> str:
        cls, val, rest = m.group(1), m.group(2).strip(), m.group(3)
        if "." in val:                       # 小节 1.1 / 1.1.1 原样
            return m.group(0)
        if val.startswith("第 ") and val.endswith(" 章"):
            num = int(val[2:-2].strip())
            return f'<span class="{cls}">{chapter_label(num)}</span>{rest}'
        if val.isdigit():
            num = int(val)
            if 1 <= num <= 37:
                return f'<span class="{cls}">{chapter_label(num)}</span>{rest}'
            if 38 <= num <= 40:            # 附录 / 版本信息不带章号
                return rest
        return m.group(0)

    html = re.sub(
        r'<span class="(toc-section-number)">([^<]*)</span>([^<]*)',
        repl, html,
    )
    # 封面后一页的作者行（HTML 的 title-block-header）与 ePub 一致
    html = re.sub(
        r'(<header id="title-block-header">.*?)<p class="author">.*?</p>',
        lambda m: m.group(1) + f'<p class="author">{AUTHOR_LINE}</p>',
        html, count=1, flags=re.S,
    )
    # 篇名：第一篇认识 → 第一篇　认识（正文标题已是全角，这里兜底 TOC 链接文本）
    html = re.sub(
        r'(第[一二三四五]篇)(认识|按角色|加固|RFC|工程实践)',
        r'\1　\2', html,
    )
    return html


# ──────────────────────────────────────────────────────────────
# 3. 把前置页插入 ePub（spine + nav + toc.ncx）
# ──────────────────────────────────────────────────────────────
FRONT_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN" xml:lang="zh-CN">
<head><meta charset="utf-8" /><title>{title}</title>
<link rel="stylesheet" type="text/css" href="../styles/stylesheet1.css" /></head>
<body epub:type="frontmatter"><section class="frontmatter">
<h1 class="unnumbered">{title}</h1>
{body}
</section></body></html>
"""


def insert_front_html(htmlp: Path, front_md: list[tuple[str, Path]]) -> None:
    """HTML：把 B/C/D 插到 TOC 之前，作为独立 section。"""
    import subprocess
    parts = []
    for i, (title, path) in enumerate(front_md, 1):
        body_md = re.sub(r"^#\s+.*?(?:\s*\{[^}]*\})?\s*\n", "",
                         path.read_text(encoding="utf-8"), count=1)
        r = subprocess.run(["pandoc", "--from=markdown", "--to=html5"],
                           input=body_md, capture_output=True, text=True)
        body = r.stdout.strip() if r.returncode == 0 else ""
        parts.append(
            f'<section class="frontmatter" id="front-{i}">\n'
            f'<h1 class="unnumbered">{title}</h1>\n{body}\n</section>\n'
        )
    h = htmlp.read_text(encoding="utf-8")
    h = re.sub(r'(<nav id="TOC"[^>]*>)', "".join(parts) + r"\1", h, count=1)
    htmlp.write_text(h, encoding="utf-8")
    print(f"  HTML 插入前置页 {len(parts)} 个")


def insert_front_epub(epub: Path, front_md: list[tuple[str, Path]]) -> None:
    import html as html_mod
    import subprocess
    with zipfile.ZipFile(epub, "r") as z:
        names = z.namelist()
        data = {n: z.read(n) for n in names}

    # Markdown → 简单 HTML（前置页只用标题/段落/表格/链接/列表/引用）
    def md_to_html(md: str) -> str:
        r = subprocess.run(
            ["pandoc", "--from=markdown", "--to=html5"],
            input=md, capture_output=True, text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else f"<p>{html_mod.escape(md)}</p>"

    new_items: list[tuple[str, str, bytes]] = []
    for i, (title, path) in enumerate(front_md, 1):
        fname = f"text/front-{['about','copyright','author'][i-1]}.xhtml"
        body_md = path.read_text(encoding="utf-8")
        # 去掉第一行 H1（我们单独渲染）
        body_md = re.sub(r"^#\s+.*?(?:\s*\{[^}]*\})?\s*\n", "", body_md, count=1)
        xhtml = FRONT_XHTML.format(title=html_mod.escape(title), body=md_to_html(body_md))
        new_items.append((fname, title, xhtml.encode("utf-8")))

    # content.opf：manifest 加 item，spine 在 nav 之后、ch001 之前插入
    opf = data["EPUB/content.opf"].decode("utf-8")
    ids = []
    for fname, _, _ in new_items:
        iid = fname.split("/")[-1].replace(".xhtml", "").replace("-", "_")
        ids.append(iid)
        opf = opf.replace(
            "</manifest>",
            f'    <item id="{iid}" href="{fname}" media-type="application/xhtml+xml" />\n  </manifest>',
        )
    spine_items = "".join(f' <itemref idref="{i}" />' for i in ids)
    # F 目录（nav）排在 B/C/D 之后、G 前言之前
    opf = re.sub(
        r'(<itemref idref="title_page_xhtml"[^>]*/>)',
        r"\1" + spine_items, opf, count=1,
    )
    opf = re.sub(r'\s*<itemref idref="nav"\s*/>', "", opf, count=1)
    opf = opf.replace(
        f' <itemref idref="{ids[-1]}" />',
        f' <itemref idref="{ids[-1]}" /> <itemref idref="nav" />',
        1,
    )
    data["EPUB/content.opf"] = opf.encode("utf-8")

    # nav.xhtml：在「前言」顶层 li 之前插入三个顶层 li
    nav = data["EPUB/nav.xhtml"].decode("utf-8")
    front_lis = "".join(
        f'<li><a href="{fname}">{html_mod.escape(title)}</a></li>'
        for fname, title, _ in new_items
    )
    # 在第一个「前言」顶层 li 之前插入；不依赖 li id
    m = re.search(r'(<li[^>]*>\s*<a href="[^"]*ch001\.xhtml)', nav)
    if m:
        nav = nav[:m.start()] + front_lis + nav[m.start():]
    else:
        # 兜底：插到第一个顶层 <li> 之前
        m2 = re.search(r'<li[^>]*>', nav)
        if m2:
            nav = nav[:m2.start()] + front_lis + nav[m2.start():]
    data["EPUB/nav.xhtml"] = nav.encode("utf-8")

    # toc.ncx：在第一个「前言」navPoint 之前插入
    ncx = data["EPUB/toc.ncx"].decode("utf-8")
    front_np = ""
    for k, (fname, title, _) in enumerate(new_items, 1):
        front_np += (
            f'<navPoint id="front-{k}"><navLabel><text>{html_mod.escape(title)}</text></navLabel>'
            f'<content src="{fname}" /></navPoint>\n'
        )
    ncx = re.sub(r'(<navPoint id="navPoint-1">)', front_np + r"\1", ncx, count=1)
    data["EPUB/toc.ncx"] = ncx.encode("utf-8")

    for fname, _, content in new_items:
        data[f"EPUB/{fname}"] = content

    tmp = epub.with_suffix(".epub.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        # EPUB OCF 要求 mimetype 为 zip 第一项且 STORED（不压缩）
        if "mimetype" in data:
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, data["mimetype"])
        for n in names:
            if n == "mimetype":
                continue
            z.writestr(n, data[n])
        for fname, _, content in new_items:
            z.writestr(f"EPUB/{fname}", content)
    tmp.replace(epub)
    print(f"  ePub 插入前置页 {len(new_items)} 个")


# ──────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────
def main() -> int:
    if len(sys.argv) < 4:
        print("用法: fix-front-matter.py <epub> <html> <version>", file=sys.stderr)
        return 2
    epub, htmlp, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    front_dir = Path("book/src/front")
    front_md = [
        ("图书介绍", front_dir / "01-about.md"),
        ("版权信息", front_dir / "02-copyright.md"),
        ("作者简介", front_dir / "03-author.md"),
    ]

    with zipfile.ZipFile(epub, "r") as z:
        names = z.namelist()
        data = {n: z.read(n) for n in names}

    # 1. title page
    tp = "EPUB/text/title_page.xhtml"
    if tp in data:
        data[tp] = fix_title_page(data[tp].decode("utf-8"), version).encode("utf-8")
        print(f"  title_page.xhtml: 作者行 + 版本号 {version}")

    # 2. 目录格式（nav / ncx）
    data["EPUB/nav.xhtml"] = fix_nav(data["EPUB/nav.xhtml"].decode("utf-8")).encode("utf-8")
    data["EPUB/toc.ncx"] = fix_ncx(data["EPUB/toc.ncx"].decode("utf-8")).encode("utf-8")
    print("  ePub nav.xhtml / toc.ncx: 目录格式已统一")

    tmp = epub.with_suffix(".epub.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        if "mimetype" in data:
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, data["mimetype"])
        for n in names:
            if n == "mimetype":
                continue
            z.writestr(n, data[n])
    tmp.replace(epub)

    # 3. 插入前置页（在格式修正之后，避免被覆盖）
    insert_front_epub(epub, front_md)

    # 4. HTML TOC + 前置页 B/C/D（build-local.sh 已不再用 include-before-body）
    h = htmlp.read_text(encoding="utf-8")
    htmlp.write_text(fix_html_toc(h), encoding="utf-8")
    insert_front_html(htmlp, front_md)
    print(f"  {htmlp.name}: TOC 格式 + 前置页已处理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
