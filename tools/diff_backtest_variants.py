"""只读：把多份回测全量输出的「新」误差按场对齐，看每个候选改法动了哪几场。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LINE = re.compile(r"^\s+(\d{4}-\d{2}-\d{2})\s+(真\S+)\s+实际\s+(\S+)\s+VDOT\s+([\d.]+)"
                  r"\s+\|\s+旧\s+([+-]?\d+)s\s+\|\s+新\s+([+-]?\d+)s")

configs = sys.argv[1:] or ["base", "no_be", "label_fresh_only"]
tables: dict[str, dict[str, tuple[str, int, str]]] = {}
for c in configs:
    p = ROOT / "tools" / f"_bt_{c}.txt"
    rows = {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        m = LINE.match(ln)
        if m:
            key = f"{m.group(1)} {m.group(2)}"
            rows[key] = (m.group(4), int(m.group(6)), m.group(3))
    tables[c] = rows

keys = sorted(tables[configs[0]])
w = max(len(k) for k in keys) + 2
print(f"{'场次':<{w}}{'实际VDOT':>8} " + "".join(f"{c:>16}" for c in configs) + "   Δ(base→末)")
for k in keys:
    vdot = tables[configs[0]][k][0]
    cells = []
    for c in configs:
        e = tables[c].get(k)
        cells.append(f"{e[1]:>+16d}" if e else f"{'—':>16}")
    delta = tables[configs[-1]][k][1] - tables["base"][k][1]
    flag = "  <<<" if abs(delta) >= 20 else ""
    print(f"{k:<{w}}{vdot:>8} " + "".join(cells) + f"   {delta:>+7d}{flag}")
