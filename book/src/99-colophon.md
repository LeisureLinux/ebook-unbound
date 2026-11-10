# 版本信息 {.unnumbered}

> 本页为电子书的自动生成元数据（`scripts/render_colophon.py` 在构建时按 git tag 覆盖本文件），
> 会出现在 PDF / ePub / HTML 三个格式的最后。

| 字段 | 值 |
|------|----|
| 当前版本 | v0.7.1 |
| 发布日期 | 2026 年 11 月 |
| 仓库 | https://github.com/LeisureLinux/ebook-unbound |
| 在线阅读 | https://leisurelinux.github.io/ebook-unbound/ |
| 许可 | CC BY-SA 4.0（正文）— © 2026 @LeisureLinux |

## 本次版本修订记录 (v0.7.1) {.unnumbered}

- 五篇扉页各配一幅科技插图，篇名独占一页；插图不带图注、不在图内重复篇名。
- 补正考证出处：FreeBSD 10.0-RELEASE 引入 Unbound 的发布公告、Kaminsky 缓存投毒
  （CERT VU#800113 / CVE-2008-1447）及投毒机理图解、ALPN [RFC 7301]、
  DNS Flag Day 2020、AXFR / IXFR、XFR-over-TLS 认证名、PMTU、priming 信任锚。
- 补概念：EDNS(0) 全称、wire format、RRset 举例、SVCB、cachedb 意义、
  EDNS 与 HTTPDNS 的关系、DDR 的 IPv4/IPv6 适用性、Firefox canary 域、
  AD 位被篡改的后果、Unbound 能否作为污染源的边界、NSD 配置详解（第 6 章）。
- 删去第 1 章 1.6 / 1.7 节、第 2 章 2.6.4 / 2.7 节，以及全书重复的
  「教学用示例」声明与自我指涉的边界说明。
- 代码与示例中的 `192.168.68.x` 统一改为 RFC 5737 文档地址 `192.0.2.x`。
- 新增 `scripts/verify-epub.py`（纯命令行结构校验）、
  `scripts/build-weread-epub.py`（微信读书专用版）、
  `scripts/add-pdf-cover.py`、`scripts/gen-part-images.py`。

## 历史版本 {.unnumbered}

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v0.7.1 | 2026-11-10 | 篇插图、考证与概念补正、去重复声明与自我指涉、NSD 配置详解 |
| v0.7.0 | 2026-11-09 | 书名与图书介绍重写、前置页与目录格式、PDF 封面、附录改名、微信读书专用版 |
| v0.6.0 | 2026-11-09 | 新增第 37 章 HTTPDNS；PDF 字体符号修复；章数同步到 37 |

---

> 本书使用 [Pandoc](https://pandoc.org/) + [XeLaTeX](https://tug.org/xetex/) 构建，
> 字体采用 [Noto Serif CJK SC](https://github.com/notofonts/noto-cjk)，
> 通过 GitHub Actions 自动构建并发布到 GitHub Pages。
> 正文以 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 发布，构建脚本为 MIT。
