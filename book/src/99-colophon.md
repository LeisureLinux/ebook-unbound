# 版本信息 {.unnumbered}

> 本页为电子书的自动生成元数据（`scripts/render_colophon.py` 在构建时按 git tag 覆盖本文件），
> 会出现在 PDF / ePub / HTML 三个格式的最后。

| 字段 | 值 |
|------|----|
| 当前版本 | v0.7.0 |
| 发布日期 | 2026 年 11 月 |
| 仓库 | https://github.com/LeisureLinux/ebook-unbound |
| 在线阅读 | https://leisurelinux.github.io/ebook-unbound/ |
| 许可 | CC BY-SA 4.0（正文）— © 2026 @LeisureLinux |

## 本次版本修订记录 (v0.7.0) {.unnumbered}

- 书名改为《DNS的加密和隐私防护——从如何加固 Unbound 说起》，图书介绍按五篇结构、
  读者层次、安全加固必要性与 DNS 隐私防护动机重写。
- 补齐完整前置页：封面（PDF / ePub）、图书介绍、版权信息、作者简介；推荐语留空。
- ePub 封面后一页署名统一为「B站频道，微信公众号： @LeisureLinux」，日期后带版本号。
- 目录格式统一为「第X章 标题」「第N篇　标题」「1.1 小节」；附录改名 A / B，
  附录与版本信息不带章号。
- 新增 `scripts/verify-epub.py` 纯命令行结构校验、`scripts/add-pdf-cover.py` PDF 封面拼接。
- 修复 XeLaTeX 下圈码与数学符号的缺字警告；修复 ePub 前置页被重复注入正文的问题。

## 历史版本 {.unnumbered}

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v0.7.0 | 2026-11-09 | 书名与图书介绍重写、前置页与目录格式、PDF 封面、附录改名 |
| v0.6.0 | 2026-11-09 | 新增第 37 章 HTTPDNS；PDF 字体符号修复；章数同步到 37 |
| v0.5.0 | 2026-11-07 | 36 章定稿：故障排查工具箱、BIND/Unbound 命名、RFC6724 与 SSH DNS 归属 |

---

> 本书使用 [Pandoc](https://pandoc.org/) + [XeLaTeX](https://tug.org/xetex/) 构建，
> 字体采用 [Noto Serif CJK SC](https://github.com/notofonts/noto-cjk)，
> 通过 GitHub Actions 自动构建并发布到 GitHub Pages。
> 正文以 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 发布，构建脚本为 MIT。
