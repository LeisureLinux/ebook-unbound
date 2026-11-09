#!/usr/bin/env bash
# build-local.sh — 本地构建 PDF / ePub / HTML（与 GitHub Actions 同一套命令）
# 依赖: pandoc, texlive-xetex + CJK 字体, librsvg2-bin, python3
set -uo pipefail
cd "$(dirname "$0")/.."
OUT=dist
mkdir -p "$OUT"

# 版本号：本地构建以 metadata.yml 的 ibooks.version 为准；
# CI 发版时会用 ${GITHUB_REF_NAME}（即 tag）覆盖，见 build.yml。
VERSION="v$(python3 -c "import yaml;print(yaml.safe_load(open('book/metadata.yml'))['ibooks']['version'])" 2>/dev/null || echo 0.7.0)"
echo "  版本: $VERSION"

# 正文：前言 + 37 章 + 附录 A/B + 版本信息
SRC=(book/src/00-preface.md)
for n in $(seq -w 1 37); do
  f=$(ls book/src/${n}-*.md 2>/dev/null | head -1)
  [ -n "$f" ] && SRC+=("$f") || echo "  ⚠️ 跳过缺失章节 ${n}-*.md（草稿尚未合并）"
done
for f in book/src/A-*.md book/src/B-*.md; do [ -f "$f" ] && SRC+=("$f"); done
f=$(ls book/src/99-*.md 2>/dev/null | head -1); [ -n "$f" ] && SRC+=("$f")
echo "  章节数: ${#SRC[@]}（全书 37 章 + 前言 + 附录 A/B + 版本信息）"
echo "  [0/5] 刷新附录 A（RFC 清单随正文自动更新）"
python3 scripts/gen-rfc-index.py || true

# 前置页 B/C/D：插在目录之前。
# --include-before-body 对三种格式都是「原样注入」，必须先转成对应格式：
#   PDF → LaTeX（H1 变 \chapter*，末尾加 \cleardoublepage 独占一页）
#   ePub / HTML → XHTML 片段（Pandoc 转出的 body 内片段）
# 直接给 Markdown 会导致 nav.xhtml / 每个章节文件里都被塞进整段原文。
FRONT=(book/src/front/01-about.md book/src/front/02-copyright.md book/src/front/03-author.md)
mkdir -p build/front
BEFORE_TEX=()
i=0
for f in "${FRONT[@]}"; do
  i=$((i+1))
  tex="build/front/0${i}.tex"
  xh="build/front/0${i}.xhtml"
  pandoc --to=latex --from=markdown+smart+footnotes+raw_attribute -V documentclass=book "$f" -o "$tex"
  printf '\n\\cleardoublepage\n' >> "$tex"
  BEFORE_TEX+=(--include-before-body="$tex")
done
# ePub / HTML 的前置页由 scripts/fix-front-matter.py 生成独立 XHTML 并插入
# spine / nav / ncx（Pandoc 的 --include-before-body 对 epub3 writer 会把
# 内容塞进 nav.xhtml 和每个章节文件，不能用）。

# 渲染封面（CJK 字体）
if [ -f book/cover.svg ]; then
  rsvg-convert -w 1200 -h 1600 book/cover.svg -o book/cover.png 2>/dev/null \
    && echo "  ✅ cover.png" || echo "  ⚠️ 封面渲染失败（继续）"
fi

echo "  [1/5] PDF (xelatex)"
pandoc --pdf-engine=xelatex \
  --metadata-file=book/metadata.yml \
  --include-in-header=book/theme/latex-header.tex \
  --toc --toc-depth=2 --number-sections \
  --from=markdown+smart+footnotes+raw_attribute \
  --resource-path=book/src \
  -V documentclass=book -V papersize=a4 \
  "${BEFORE_TEX[@]}" \
  "${SRC[@]}" -o "$OUT/unbound-dns-hardening.pdf" || echo "  ❌ PDF 失败"
python3 scripts/add-pdf-cover.py book/cover.png "$OUT/unbound-dns-hardening.pdf" || echo "  ⚠️ PDF 封面拼接失败"

echo "  [2/5] ePub"
pandoc --to=epub3 \
  --metadata-file=book/metadata.yml \
  --css=book/theme/epub.css \
  --toc --toc-depth=3 --number-sections \
  --from=markdown+smart+footnotes+raw_attribute \
  --resource-path=book/src \
  --epub-cover-image=book/cover.png \
  "${SRC[@]}" -o "$OUT/unbound-dns-hardening.epub" || echo "  ❌ ePub 失败"
python3 scripts/strip_landmarks.py 2>/dev/null || true

echo "  [3/5] HTML"
pandoc --to=html5 --standalone \
  --metadata-file=book/metadata.yml \
  --toc --toc-depth=3 --number-sections \
  --from=markdown+smart+footnotes+raw_attribute \
  --resource-path=book/src \
  --css=book/theme/html.css --self-contained \
  "${SRC[@]}" -o "$OUT/unbound-dns-hardening.html" || echo "  ❌ HTML 失败"

echo "  [4/5] 前置页 / 目录格式后处理（篇 / 第X章 / Y.n / 附录 / 版本号）"
python3 scripts/fix-front-matter.py \
  "$OUT/unbound-dns-hardening.epub" "$OUT/unbound-dns-hardening.html" "$VERSION" || true

echo "  [5/5] ePub 结构校验（纯命令行，不依赖 GUI）"
python3 scripts/verify-epub.py "$OUT/unbound-dns-hardening.epub" "$VERSION" || echo "  ⚠️ ePub 校验未通过"
echo "  产物:"; ls -lh "$OUT" 2>/dev/null | sed 's/^/    /' 
