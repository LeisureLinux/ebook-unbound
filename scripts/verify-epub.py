#!/usr/bin/env python3
"""verify-epub.py — 纯命令行校验 ePub 结构（不依赖 GUI / Sigil / epubcheck）。

检查项：
  1. mimetype 必须是 zip 第一项且未压缩、内容正确
  2. META-INF/container.xml 指向的 OPF 存在
  3. OPF manifest 里所有 href 都能在 zip 内找到；spine 的 idref 都在 manifest 里
  4. nav.xhtml 与 toc.ncx 的每个 href / src 目标存在
  5. 目录格式：第X章 / 第N篇　XXX / 附录 A、附录 B / 版本信息不带章号
  6. 前置页 B/C/D 各自是独立文件，正文没有重复注入（「这是一本关于」只出现 1 次）
  7. title_page.xhtml 作者行与版本号

用法: python3 scripts/verify-epub.py <file.epub> [version]
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

OPF_NS = "{http://www.idpf.org/2007/opf}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"

problems: list[str] = []
info: list[str] = []


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: verify-epub.py <file.epub> [version]", file=sys.stderr)
        return 2
    epub = Path(sys.argv[1])
    version = sys.argv[2] if len(sys.argv) > 2 else ""
    if not epub.exists():
        print(f"  ❌ 文件不存在: {epub}")
        return 2

    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        first = z.infolist()[0]
        # 1. mimetype
        if first.filename != "mimetype":
            problems.append(f"mimetype 不是 zip 第一项（是 {first.filename}）")
        elif first.compress_type != zipfile.ZIP_STORED:
            problems.append("mimetype 不应压缩")
        elif z.read("mimetype").decode().strip() != "application/epub+zip":
            problems.append("mimetype 内容错误")
        else:
            info.append("mimetype: OK（未压缩、第一项）")

        # 2. container.xml → OPF
        try:
            root = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
            opf = opf_path.attrib["full-path"] if opf_path is not None else "EPUB/content.opf"
        except Exception as e:  # noqa: BLE001
            problems.append(f"container.xml 解析失败: {e}")
            opf = "EPUB/content.opf"
        if opf not in names:
            problems.append(f"OPF 不存在: {opf}")
            _report(epub)
            return 1
        info.append(f"OPF: {opf}")

        opf_text = z.read(opf).decode("utf-8", "replace")
        prefix = opf.rsplit("/", 1)[0] if "/" in opf else ""
        try:
            opf_root = ET.fromstring(opf_text)
        except Exception as e:  # noqa: BLE001
            problems.append(f"OPF XML 解析失败: {e}")
            _report(epub)
            return 1

        manifest = {
            it.attrib["id"]: it.attrib.get("href", "")
            for it in opf_root.findall(f".//{OPF_NS}manifest/{OPF_NS}item")
        }
        spine = [
            it.attrib.get("idref")
            for it in opf_root.findall(f".//{OPF_NS}spine/{OPF_NS}itemref")
        ]
        missing_href = [
            f"{i}:{h}" for i, h in manifest.items()
            if h and f"{prefix}/{h}" not in names and h not in names
        ]
        if missing_href:
            problems.append(f"manifest 里 {len(missing_href)} 个 href 找不到: {missing_href[:5]}")
        bad_idref = [i for i in spine if i not in manifest]
        if bad_idref:
            problems.append(f"spine 里 {len(bad_idref)} 个 idref 不在 manifest: {bad_idref[:5]}")
        info.append(f"manifest {len(manifest)} 项 / spine {len(spine)} 项，引用完整"
                    if not missing_href and not bad_idref
                    else f"manifest/spine 有问题")

        # 3. nav / ncx 链接目标
        nav_name = next((n for n in names if n.endswith("nav.xhtml")), None)
        ncx_name = next((n for n in names if n.endswith("toc.ncx")), None)
        nav = z.read(nav_name).decode("utf-8", "replace") if nav_name else ""
        ncx = z.read(ncx_name).decode("utf-8", "replace") if ncx_name else ""
        if not nav_name or not ncx_name:
            problems.append("缺 nav.xhtml 或 toc.ncx")

        epub_dir = prefix  # e.g. EPUB
        nav_links = re.findall(r'href="([^"#]+)', nav)
        missing_nav = []
        for h in nav_links:
            target = f"{epub_dir}/{h}" if not h.startswith("EPUB/") else h
            if target not in names and h not in names:
                missing_nav.append(h)
        if missing_nav:
            problems.append(f"nav 里 {len(missing_nav)} 个链接目标不存在: {missing_nav[:5]}")
        ncx_srcs = re.findall(r'<content src="([^"#]+)', ncx)
        missing_ncx = [s for s in ncx_srcs
                       if f"{epub_dir}/{s}" not in names and s not in names]
        if missing_ncx:
            problems.append(f"ncx 里 {len(missing_ncx)} 个 src 目标不存在: {missing_ncx[:5]}")
        info.append(f"nav {len(nav_links)} 链接 / ncx {len(ncx_srcs)} 条目，目标齐全"
                    if not missing_nav and not missing_ncx else "nav/ncx 目标缺失")

        # 4. 目录格式
        labels = [re.sub(r"<[^>]+>", "", m.group(1)).strip()
                  for m in re.finditer(r'<a href="text/ch\d+\.xhtml"[^>]*>(.*?)</a>', nav)]
        ch_labels = [x for x in labels if re.match(r"^第[一二三四五六七八九十]+章", x)]
        if len(ch_labels) != 37:
            problems.append(f"章级「第X章」条目 {len(ch_labels)} 个，应为 37")
        bad_ch = [x for x in ch_labels if re.match(r"^第\s", x) or re.search(r"章\s+\d", x)]
        if bad_ch:
            problems.append(f"章级标签格式异常: {bad_ch[:3]}")
        parts = [x for x in labels if re.match(r"^第[一二三四五]篇", x)]
        bad_part = [x for x in parts if "　" not in x]
        if bad_part:
            problems.append(f"篇名缺全角空格: {bad_part[:3]}")
        for name in ("附录 A", "附录 B", "版本信息"):
            if not any(x.startswith(name) for x in labels):
                problems.append(f"目录缺「{name}」")
        if any(re.search(r"第[一二三四五六七八九十]+章.*附录|第\s*3[89]\s*章|第\s*40\s*章", x)
               for x in labels):
            problems.append("附录/版本信息被误加章号")
        info.append(f"目录: {len(ch_labels)} 章 / {len(parts)} 篇 / 附录 A、B / 版本信息")

        # 5. 前置页不重复注入
        front_files = [n for n in names if "/front-" in n and n.endswith(".xhtml")]
        if len(front_files) != 3:
            problems.append(f"前置页文件 {len(front_files)} 个，应为 3: {front_files}")
        all_xhtml = [n for n in names if n.endswith(".xhtml") and not n.endswith("nav.xhtml")]
        repeat = []
        for n in all_xhtml:
            t = z.read(n).decode("utf-8", "replace")
            if t.count("这是一本关于「自己掌握解析链路」") > 1:
                repeat.append(n)
        if repeat:
            problems.append(f"「图书介绍」正文被重复注入: {repeat[:5]}")
        if "这是一本关于" in nav:
            problems.append("nav.xhtml 里混入了图书介绍正文（Markdown 原样注入）")
        info.append(f"前置页 {len(front_files)} 个独立文件，正文无重复注入")

        # 6. title_page
        tp = next((n for n in names if n.endswith("title_page.xhtml")), None)
        if tp:
            t = z.read(tp).decode("utf-8", "replace")
            if "B站频道，微信公众号： @LeisureLinux" not in t:
                problems.append("title_page 作者行不对")
            # title_page 里 @LeisureLinux 正常会出现 2 次：author 行 + rights 版权声明；
            # >3 次说明 publisher 行没被清掉（重复署名）。
            if t.count("@LeisureLinux") > 3 or "B站频道，微信公众号： @LeisureLinux" not in t:
                problems.append("title_page 署名行异常（重复或缺失）")
            if version and version.lstrip("vV") not in t:
                problems.append(f"title_page 缺版本号 {version}")
            info.append("title_page: 作者行 + 版本号 OK"
                        if not any("title_page" in p for p in problems) else "title_page 有问题")
        else:
            problems.append("缺 title_page.xhtml")

        # 7. 各 XHTML 可解析
        bad_xml = []
        for n in all_xhtml:
            t = z.read(n).decode("utf-8", "replace")
            # 去掉 DOCTYPE 后解析 body 内容
            try:
                ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", t))
            except Exception as e:  # noqa: BLE001
                bad_xml.append(f"{n}: {str(e)[:60]}")
        if bad_xml:
            problems.append(f"{len(bad_xml)} 个 XHTML 解析失败: {bad_xml[:3]}")
        else:
            info.append(f"{len(all_xhtml)} 个 XHTML 均可解析")

    return _report(epub)


def _report(epub: Path) -> int:
    print(f"  校验 {epub.name} ({epub.stat().st_size // 1024} KB)")
    for x in info:
        print(f"     ✅ {x}")
    if problems:
        print(f"  ❌ {len(problems)} 个问题：")
        for p in problems[:30]:
            print(f"     - {p}")
        return 1
    print("  ✅ ePub 结构校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
