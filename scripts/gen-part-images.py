#!/usr/bin/env python3
"""生成 5 幅「篇」扉页插图，风格沿用封面（Unbound 官方色系）。

深靛蓝 #2d2e83 / 青 #1fc2d7 / 橙 #f26521，深底 + 折面 + 几何线条。
每幅对应一篇主题，输出 book/images/partN.svg，再由 rsvg-convert 转 PNG。
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "book" / "images"
OUT.mkdir(parents=True, exist_ok=True)

W, H = 900, 1200
PAL = dict(bg1="#0d0d27", bg2="#16173f", facet="#2d2e83",
           cyan="#1fc2d7", cyan2="#1289a8", orange="#f26521",
           light="#e2e8f0", dim="#94a3b8", green="#5eead4")

HEAD = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0.6" y2="1">
    <stop offset="0%" stop-color="{PAL['bg1']}"/><stop offset="60%" stop-color="{PAL['bg2']}"/>
    <stop offset="100%" stop-color="{PAL['bg1']}"/></linearGradient>
  <linearGradient id="facet" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0%" stop-color="#0d0d27"/><stop offset="50%" stop-color="{PAL['facet']}"/>
    <stop offset="100%" stop-color="#0d0d27"/></linearGradient>
  <radialGradient id="glow"><stop offset="0%" stop-color="{PAL['cyan']}" stop-opacity="0.18"/>
    <stop offset="100%" stop-color="{PAL['cyan']}" stop-opacity="0"/></radialGradient>
</defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/>
<circle cx="450" cy="430" r="330" fill="url(#glow)"/>
<g font-family="DejaVu Sans, Noto Sans CJK SC, sans-serif">
  <text x="60" y="90" fill="{PAL['cyan']}" font-size="22" letter-spacing="6">DNS / UNBOUND</text>
  <line x1="60" y1="108" x2="840" y2="108" stroke="{PAL['cyan']}" stroke-width="1.5" opacity="0.45"/>
</g>
'''
FOOT = """
<line x1="330" y1="1100" x2="570" y2="1100" stroke="url(#facet)" stroke-width="2" opacity="0.6"/>
</svg>
"""


def part1():
    """第一篇：倒树命名空间 + 四种角色"""
    s = HEAD
    # 倒树：根 -> TLD -> 域 -> 主机
    s += f'<g fill="none" stroke="{PAL["cyan"]}" stroke-width="2.5" opacity="0.85">'
    nodes = [(450, 200), (300, 320), (600, 320), (220, 440), (380, 440), (540, 440), (700, 440)]
    edges = [(0, 1), (0, 2), (1, 3), (1, 4), (2, 5), (2, 6)]
    for a, b in edges:
        s += f'<line x1="{nodes[a][0]}" y1="{nodes[a][1]+18}" x2="{nodes[b][0]}" y2="{nodes[b][1]-18}"/>'
    s += '</g>'
    for i, (x, y) in enumerate(nodes):
        r = 20 if i == 0 else 14
        c = PAL['orange'] if i == 0 else PAL['cyan']
        s += f'<circle cx="{x}" cy="{y}" r="{r}" fill="{c}" opacity="0.9"/>'
        if i == 0:
            s += f'<circle cx="{x}" cy="{y}" r="30" fill="none" stroke="{PAL["orange"]}" stroke-width="1.5" opacity="0.5"/>'
    # 四种角色横排
    s += f'<g font-family="DejaVu Sans, Noto Sans CJK SC, sans-serif" font-size="19" fill="{PAL["light"]}" text-anchor="middle">'
    for i, lab in enumerate(["stub", "cache", "forward", "recursive"]):
        x = 140 + i * 205
        s += f'<rect x="{x-88}" y="610" width="176" height="66" rx="8" fill="{PAL["facet"]}" opacity="0.55"/>'
        s += f'<text x="{x}" y="650">{lab}</text>'
        if i < 3:
            s += f'<path d="M{x+90} 643 L{x+115} 643" stroke="{PAL["cyan"]}" stroke-width="2" fill="none"/>'
            s += f'<path d="M{x+112} 639 L{x+117} 643 L{x+112} 647" stroke="{PAL["cyan"]}" stroke-width="2" fill="none"/>'
    s += '</g>'
    # 折面徽记
    s += f'<path d="M450 780 L580 855 L580 1005 L450 1080 L320 1005 L320 855 Z" fill="url(#facet)" opacity="0.5"/>'
    s += f'<path d="M320 855 L450 930 L580 855" fill="none" stroke="{PAL["cyan"]}" stroke-width="2" opacity="0.6"/>'
    s += f'<circle cx="450" cy="930" r="9" fill="{PAL["orange"]}"/>'
    return s + FOOT


def part2():
    """第二篇：多实例 / 端口 / 配置块"""
    s = HEAD
    # 三个实例盒子 + 端口
    boxes = [(150, 240, "resolver", "5335"), (450, 240, "forwarder", "5357"), (150, 470, "mirror", "1053"), (450, 470, "auth", "5358")]
    s += f'<g font-family="DejaVu Sans Mono, monospace" font-size="20">'
    for x, y, name, port in boxes:
        s += f'<rect x="{x}" y="{y}" width="300" height="150" rx="10" fill="{PAL["facet"]}" opacity="0.5"/>'
        s += f'<rect x="{x}" y="{y}" width="300" height="38" rx="10" fill="{PAL["cyan2"]}" opacity="0.55"/>'
        s += f'<text x="{x+16}" y="{y+26}" fill="{PAL["light"]}" font-size="17">{name}</text>'
        s += f'<text x="{x+16}" y="{y+80}" fill="{PAL["green"]}" font-size="22">:{port}</text>'
        s += f'<line x1="{x+16}" y1="{y+100}" x2="{x+284}" y2="{y+100}" stroke="{PAL["cyan"]}" stroke-width="1.5" opacity="0.5" stroke-dasharray="8 6"/>'
        s += f'<text x="{x+16}" y="{y+128}" fill="{PAL["dim"]}" font-size="15">unbound.conf</text>'
    s += '</g>'
    # 连线汇聚到中央菱形
    s += f'<g fill="none" stroke="{PAL["cyan"]}" stroke-width="2" opacity="0.6">'
    for x, y, _, _ in boxes:
        s += f'<path d="M{x+150} {y+150} C{x+150} 720 450 720 450 760"/>'
    s += '</g>'
    s += f'<path d="M450 760 L560 825 L450 890 L340 825 Z" fill="url(#facet)"/>'
    s += f'<circle cx="450" cy="825" r="10" fill="{PAL["orange"]}"/>'
    return s + FOOT


def part3():
    """第三篇：加固 / 盾 / 信任边界"""
    s = HEAD
    # 盾形折面
    s += f'<path d="M450 170 L680 260 L680 600 C680 780 560 900 450 960 C340 900 220 780 220 600 L220 260 Z" fill="url(#facet)" opacity="0.75"/>'
    s += f'<path d="M450 170 L680 260 L680 600 C680 780 560 900 450 960 C340 900 220 780 220 600 L220 260 Z" fill="none" stroke="{PAL["cyan"]}" stroke-width="3"/>'
    s += f'<path d="M220 260 L450 350 L680 260" fill="none" stroke="{PAL["cyan"]}" stroke-width="2" opacity="0.6"/>'
    s += f'<path d="M450 350 L450 960" fill="none" stroke="{PAL["cyan"]}" stroke-width="1.5" opacity="0.35"/>'
    # 锁 / 校验链
    s += f'<g fill="none" stroke="{PAL["green"]}" stroke-width="4">'
    s += '<path d="M390 540 L390 500 C390 455 510 455 510 500 L510 540"/>'
    s += '</g>'
    s += f'<rect x="370" y="540" width="160" height="120" rx="14" fill="{PAL["cyan2"]}" opacity="0.75"/>'
    s += f'<circle cx="450" cy="592" r="14" fill="{PAL["bg1"]}"/>'
    s += f'<rect x="445" y="592" width="10" height="34" rx="5" fill="{PAL["bg1"]}"/>'
    # 三层边界标注
    s += f'<g font-family="DejaVu Sans, Noto Sans CJK SC, sans-serif" font-size="17" fill="{PAL["light"]}" text-anchor="middle">'
    for i, lab in enumerate(["DNSSEC", "DoT / DoH", "QMIN · DDR"]):
        y = 730 + i * 62
        s += f'<rect x="285" y="{y-28}" width="330" height="44" rx="6" fill="{PAL["facet"]}" opacity="0.6"/>'
        s += f'<text x="450" y="{y}">{lab}</text>'
    s += '</g>'
    s += f'<circle cx="450" cy="170" r="12" fill="{PAL["orange"]}"/>'
    return s + FOOT


def part4():
    """第四篇：RFC 文档堆栈"""
    s = HEAD
    s += f'<g font-family="DejaVu Sans Mono, monospace" font-size="19">'
    for i, (num, label, col) in enumerate([
        ("1034 / 1035", "DNS 基础", PAL['cyan']),
        ("4033–4035", "DNSSEC", PAL['green']),
        ("7858 / 8484", "加密传输", PAL['cyan2']),
        ("9156 / 9462", "隐私与发现", PAL['orange']),
    ]):
        y = 230 + i * 175
        off = i * 22
        s += f'<rect x="{150+off}" y="{y}" width="{600-off}" height="140" rx="8" fill="{PAL["facet"]}" opacity="{0.35+i*0.12}"/>'
        s += f'<rect x="{150+off}" y="{y}" width="8" height="140" fill="{col}"/>'
        s += f'<text x="{185+off}" y="{y+52}" fill="{col}" font-size="24">RFC {num}</text>'
        s += f'<text x="{185+off}" y="{y+96}" fill="{PAL["light"]}" font-size="20">{label}</text>'
        s += f'<line x1="{185+off}" y1="{y+112}" x2="{700}" y2="{y+112}" stroke="{PAL["dim"]}" stroke-width="1" opacity="0.4" stroke-dasharray="6 8"/>'
    s += '</g>'
    s += f'<path d="M450 940 L530 985 L450 1030 L370 985 Z" fill="none" stroke="{PAL["cyan"]}" stroke-width="2" opacity="0.6"/>'
    s += f'<circle cx="450" cy="985" r="7" fill="{PAL["orange"]}"/>'
    return s + FOOT


def part5():
    """第五篇：工程拓扑 / 网关 + 云 + IPv6 + 集群"""
    s = HEAD
    # 中央网关折面
    s += f'<path d="M450 220 L600 305 L600 475 L450 560 L300 475 L300 305 Z" fill="url(#facet)"/>'
    s += f'<path d="M300 305 L450 390 L600 305" fill="none" stroke="{PAL["cyan"]}" stroke-width="2"/>'
    s += f'<circle cx="450" cy="390" r="11" fill="{PAL["orange"]}"/>'
    s += f'<g font-family="DejaVu Sans, Noto Sans CJK SC, sans-serif" font-size="18" fill="{PAL["light"]}" text-anchor="middle">'
    s += '<text x="450" y="340" font-size="20">gw-01</text>'
    # 六个外围节点
    labs = ["IPv6 / RA", "Kubernetes", "anycast", "DHCP", "observability", "honeypot"]
    import math
    for i, lab in enumerate(labs):
        ang = math.pi / 3 * i - math.pi / 6
        x, y = 450 + 300 * math.cos(ang), 560 + 240 * math.sin(ang)
        s += f'<line x1="450" y1="560" x2="{x:.0f}" y2="{y:.0f}" stroke="{PAL["cyan"]}" stroke-width="2" opacity="0.45"/>'
        s += f'<rect x="{x-95:.0f}" y="{y-24:.0f}" width="190" height="48" rx="8" fill="{PAL["facet"]}" opacity="0.7"/>'
        s += f'<text x="{x:.0f}" y="{y+6:.0f}" fill="{PAL["light"]}" font-size="16">{lab}</text>'
        s += f'<circle cx="{x:.0f}" cy="{y-30:.0f}" r="5" fill="{PAL["green"]}"/>'
    s += '</g>'
    return s + FOOT


for i, fn in enumerate([part1, part2, part3, part4, part5], 1):
    (OUT / f"part{i}.svg").write_text(fn(), encoding="utf-8")
    print(f"wrote part{i}.svg")
