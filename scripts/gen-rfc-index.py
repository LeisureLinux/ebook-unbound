#!/usr/bin/env python3
"""gen-rfc-index.py — 从全书正文抽取 RFC 编号，生成附录 A1 的表格。

数据源：RFC Editor 官方 rfc-index.txt（构建时下载，失败时退回本地缓存
/var/tmp/rfc-index.txt 或 resources/rfc-index.txt）。
每章只引用编号与结论；名称/日期/状态/替代关系一律取自官方索引，不编造。

用法:
    python3 scripts/gen-rfc-index.py            # 生成 book/src/A1-rfc-index.md
    python3 scripts/gen-rfc-index.py --check    # 只报告与磁盘版本的差异
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "book" / "src"
OUT = SRC / "A1-rfc-index.md"
INDEX_URL = "https://www.rfc-editor.org/rfc-index.txt"
CACHES = [ROOT / "resources" / "rfc-index.txt", Path("/tmp/rfc-index.txt")]


def load_index() -> str:
    for c in CACHES:
        if c.exists() and c.stat().st_size > 1_000_000:
            return c.read_text(errors="replace")
    try:
        req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "Mozilla/5.0 axu-probe/1.0"})
        with urllib.request.urlopen(req, timeout=40) as r:
            t = r.read().decode("utf-8", "replace")
        (ROOT / "resources" / "rfc-index.txt").write_text(t, encoding="utf-8")
        return t
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  索引下载失败: {e}", file=sys.stderr)
        return ""


def parse(txt: str, ids: set[str]) -> dict[str, dict]:
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"(?m)^(\d{4}) ", txt)]
    out: dict[str, dict] = {}
    for i, (pos, n) in enumerate(starts):
        if n not in ids:
            continue
        end = starts[i + 1][0] if i + 1 < len(starts) else len(txt)
        b = re.sub(r"\s+", " ", txt[pos:end])
        nm = re.match(r"^\d{4}\s+(.*?)\.\s", b)
        date = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\.", b)
        status = re.search(r"\(Status:\s*([^)]+)\)", b)
        obs = re.search(r"\(Obsoleted by\s+([^)]+)\)", b)
        upd = re.search(r"\(Updated by\s+([^)]+)\)", b)
        out[n] = {
            "title": nm.group(1).replace("|", "／") if nm else "",
            "date": f"{date.group(1)[:3]} {date.group(2)}" if date else "",
            "status": status.group(1).strip() if status else ("NOT ISSUED" if "Not Issued" in b[:80] else ""),
            "obsoleted_by": ",".join(re.findall(r"RFC\s?\d+", obs.group(1))) if obs else "",
            "updated_by": ",".join(re.findall(r"RFC\s?\d+", upd.group(1))) if upd else "",
        }
    return out


def describe(title: str) -> str:
    t = title.lower()
    rules = [
        (("dnssec", "dns security", "nsec", "dnskey", "rrsig", "trust anchor", "dlv", "signing authority"), "DNSSEC / 信任链"),
        (("qname", "privacy", "dns over", "encrypted", "doh", "dot", "doq", "odoh", "cookie", "padding", "client subnet"), "查询隐私 / 加密传输"),
        (("multicast", "service discovery", "service registration", "dns-sd", "discovery proxy", "mdns", "llq", "designated resolver"), "服务发现 / 多播 / DDR"),
        (("ipv6", "neighbor", "slaac", "router", "source address", "unique local", "temporary", "prefix", "renumber"), "IPv6 / 地址与 RA"),
        (("dynamic update", "gss-tsig", "tsig", "tkey", "dhcp", "transaction signature"), "动态更新 / 事务认证"),
        (("dkim", "spf", "dmarc", "mta-sts", "tls-rpt", "dane", "tlsa", "ssh", "certificate", "caa", "acme", "mail", "smtp"), "邮件 / 证书 / 公钥发布"),
        (("special-use", "reserved", "home.arpa", ".local", ".onion", "special use"), "特殊用途域名"),
        (("serving stale", "negative caching", "ncache", "nxdomain", "minimal-sized", "aggressive", "edns", "extension mechanisms"), "缓存 / 应答语义 / EDNS"),
        (("domain name", "dns", "nameserver", "resolver", "zone", "wildcard", "clarification", "terminology", "internet hosts"), "DNS 基础 / 术语 / 实现"),
    ]
    for keys, label in rules:
        if any(k in t for k in keys):
            return label
    return "其他 / 网络基础设施"


def render(meta: dict[str, dict], wanted: set[str]) -> str:
    lines = [
        "# 附录 A1 RFC 清单", "",
        "> 数据来源：RFC Editor 官方 `rfc-index.txt`。状态取自索引的 `Status:` 字段；",
        "> 「有效」按是否被 `Obsoleted by` 替代判定，`Updated by` 表示有后续修订但仍有效。", "",
        "| RFC | 名称 | 发布日期 | 状态 | 有效 | 描述对象 |",
        "|---|---|---|---|---|---|",
    ]
    missing = sorted(wanted - set(meta), key=int)
    for n in sorted(meta, key=int):
        m = meta[n]
        valid = "已被替代" if m["obsoleted_by"] else ("未发行" if m["status"] == "NOT ISSUED" else "有效")
        st = m["status"] or "—"
        if m["updated_by"]:
            st += "（有修订）"
        lines.append(
            f"| [RFC {n}](https://www.rfc-editor.org/rfc/rfc{n}) | {m['title']} | "
            f"{m['date'] or '—'} | {st} | {valid} | {describe(m['title'])} |"
        )
    lines += ["", "> 本书正文只引用编号与结论；发布日期、状态与替代关系请以 RFC Editor 索引为准。"]
    if missing:
        lines += ["", f"> ⚠️ 以下编号未在索引中找到（正文里可能只是「按名称检索」的占位）：{', '.join(missing)}。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    wanted: set[str] = set()
    for p in SRC.glob("*.md"):
        if p.name.startswith("A1-"):
            continue
        wanted |= set(re.findall(r"RFC\s+(\d+)", p.read_text(encoding="utf-8")))
    txt = load_index()
    if not txt:
        print("  ❌ 无索引，无法生成 A1", file=sys.stderr)
        return 1
    meta = parse(txt, wanted)
    content = render(meta, wanted)
    if "--check" in sys.argv:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        d = "一致" if cur == content else "需重新生成"
        print(f"  正文引用 {len(wanted)} 个 RFC，索引命中 {len(meta)} 个 → {d}")
        return 0 if d == "一致" else 1
    OUT.write_text(content, encoding="utf-8")
    print(f"  ✅ A1 生成：正文引用 {len(wanted)} 个，索引命中 {len(meta)} 个，{len(content.splitlines())} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
