#!/usr/bin/env python3
r"""add-pdf-cover.py — 把封面 PNG 作为独立第 1 页拼到 PDF 产物前面。

背景：Pandoc 的 book 类模板在 begin{document} 后立刻发 frontmatter，
双面排版下无论用 \AtBeginDocument 还是 patch \maketitle，第一页都会变成
空白 roman 页，封面图被挤到第 2 页。改为构建后处理最稳。

用法: python3 scripts/add-pdf-cover.py <cover.png> <book.pdf>
"""
from __future__ import annotations
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: add-pdf-cover.py <cover.png> <book.pdf>", file=sys.stderr)
        return 2
    png, pdf = Path(sys.argv[1]), Path(sys.argv[2])

    # A4 @ 150dpi = 1240 x 1754；封面按整页缩放（keepaspectratio 等价）
    im = Image.open(png).convert("RGB")
    page_w, page_h = 1240, 1754
    im.thumbnail((page_w, page_h), Image.LANCZOS)
    canvas = Image.new("RGB", (page_w, page_h), (255, 255, 255))
    canvas.paste(im, ((page_w - im.width) // 2, (page_h - im.height) // 2))
    buf = BytesIO()
    canvas.save(buf, format="PDF", resolution=150.0)
    buf.seek(0)
    cover = PdfReader(buf).pages[0]

    book = PdfReader(str(pdf))
    w = PdfWriter()
    w.add_page(cover)
    for pg in book.pages:
        w.add_page(pg)
    # 保留原 metadata
    if book.metadata:
        w.add_metadata({k: str(v) for k, v in book.metadata.items() if v is not None})
    tmp = pdf.with_suffix(".pdf.tmp")
    with tmp.open("wb") as f:
        w.write(f)
    tmp.replace(pdf)
    print(f"  PDF 封面已拼接：{len(book.pages)} → {len(w.pages)} 页")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
