# 版本信息 {.unnumbered}

> 本页为电子书的自动生成元数据（`scripts/render_colophon.py` 在构建时按 git tag 覆盖本文件），
> 会出现在 PDF / ePub / HTML 三个格式的最后。

| 字段 | 值 |
|------|----|
| 当前版本 | v0.7.3 |
| 发布日期 | 2026 年 11 月 |
| 仓库 | https://github.com/LeisureLinux/ebook-unbound |
| 在线阅读 | https://leisurelinux.github.io/ebook-unbound/ |
| 许可 | CC BY-SA 4.0（正文）— © 2026 @LeisureLinux |

## 本次版本修订记录 (v0.7.3) {.unnumbered}

- 第 17 章补正 ODoH [RFC 9230] 的标准地位：Independent Submission / Experimental，
  未经 IETF 背书；底层加密是 [RFC 9180] HPKE，不是 [RFC 9458] OHTTP。
  厘清时间线：ODoH（2022，Experimental）在前，OHTTP（2024，Proposed Standard）
  是后来的泛化，9458 的致谢写明设计基于 9230。
- 新增一条误读：ODoH 是公开 RFC 规范，开源项目只是实现。
- 术语表 ODoH 词条补齐状态；去掉第 5 章一处章节预告式自我指涉。

## 历史版本 {.unnumbered}

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v0.7.3 | 2026-11-16 | ODoH 标准地位与 OHTTP 时间线补正 |
| v0.7.2 | 2026-11-11 | 并发竞速的上游隐私代价、bootstrap DoH IP 与 DoH bypass 交叉引用 |
| v0.7.1 | 2026-11-10 | 篇插图、考证与概念补正、去重复声明与自我指涉、NSD 配置详解 |
| v0.7.0 | 2026-11-09 | 书名与图书介绍重写、前置页与目录格式、PDF 封面、附录改名、微信读书专用版 |
| v0.6.0 | 2026-11-09 | 新增第 37 章 HTTPDNS；PDF 字体符号修复；章数同步到 37 |

---

> 本书使用 [Pandoc](https://pandoc.org/) + [XeLaTeX](https://tug.org/xetex/) 构建，
> 字体采用 [Noto Serif CJK SC](https://github.com/notofonts/noto-cjk)，
> 通过 GitHub Actions 自动构建并发布到 GitHub Pages。
> 正文以 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 发布，构建脚本为 MIT。
