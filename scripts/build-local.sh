#!/usr/bin/env bash
# build-local.sh — 本地构建 PDF / ePub / HTML（与 GitHub Actions 同一套命令）
# 依赖: pandoc, texlive-xetex + CJK 字体, librsvg2-bin, python3
set -uo pipefail
cd "$(dirname "$0")/.."
OUT=dist
mkdir -p "$OUT"

SRC=(book/src/00-preface.md)
for n in $(seq -w 1 24); do
  f=$(ls book/src/${n}-*.md 2>/dev/null | head -1)
  [ -n "$f" ] && SRC+=("$f") || echo "  ⚠️ 跳过缺失章节 ${n}-*.md（草稿尚未合并）"
done
f=$(ls book/src/99-*.md 2>/dev/null | head -1); [ -n "$f" ] && SRC+=("$f")
echo "  章节数: ${#SRC[@]}（全书 24 章 + 前言 + 版本信息）"

# 渲染封面（CJK 字体）
if [ -f book/cover.svg ]; then
  rsvg-convert -w 1200 -h 1600 book/cover.svg -o book/cover.png 2>/dev/null \
    && echo "  ✅ cover.png" || echo "  ⚠️ 封面渲染失败（继续）"
fi

echo "  [1/3] PDF (xelatex)"
pandoc --pdf-engine=xelatex \
  --metadata-file=book/metadata.yml \
  --include-in-header=book/theme/latex-header.tex \
  --toc --toc-depth=2 --number-sections \
  --from=markdown+smart+footnotes+raw_attribute \
  --resource-path=book/src \
  -V documentclass=book -V papersize=a4 \
  "${SRC[@]}" -o "$OUT/unbound-dns-hardening.pdf" || echo "  ❌ PDF 失败"

echo "  [2/3] ePub"
pandoc --to=epub3 \
  --metadata-file=book/metadata.yml \
  --css=book/theme/epub.css \
  --toc --toc-depth=3 --number-sections \
  --from=markdown+smart+footnotes+raw_attribute \
  --resource-path=book/src \
  --epub-cover-image=book/cover.png \
  "${SRC[@]}" -o "$OUT/unbound-dns-hardening.epub" || echo "  ❌ ePub 失败"
python3 scripts/strip_landmarks.py 2>/dev/null || true

echo "  [3/3] HTML"
pandoc --to=html5 --standalone \
  --metadata-file=book/metadata.yml \
  --toc --toc-depth=3 --number-sections \
  --from=markdown+smart+footnotes+raw_attribute \
  --resource-path=book/src \
  --css=book/theme/html.css --self-contained \
  "${SRC[@]}" -o "$OUT/unbound-dns-hardening.html" || echo "  ❌ HTML 失败"

echo "  产物:"; ls -lh "$OUT" 2>/dev/null | sed 's/^/    /'
