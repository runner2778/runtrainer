"""只读：核对 VDOT ↔ 成绩 的往返一致性与两个函数的差异。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.domain import vdot as vd  # noqa: E402


def fmt(sec) -> str:
    h, r = divmod(int(round(sec)), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


print("v      " + "  ".join(f"{k:>10}" for k in vd.DISTANCES))
for v in (50.0, 52.4, 53.8, 55.0):
    row = [f"{fmt(vd.predict_time(d, v)):>10}" for d in vd.DISTANCES.values()]
    print(f"{v:<6} " + "  ".join(row))

print("\n== equivalent_times vs predict_time（同档） ==")
for v in (50.0, 52.4, 53.8):
    eq = vd.equivalent_times(v)
    print(f"  vdot {v}: equivalent_times={ {k: fmt(x) for k, x in eq.items()} }")
    print(f"           predict_time   ="
          f"{ {k: fmt(vd.predict_time(d, v)) for k, d in vd.DISTANCES.items()} }")

print("\n== 往返：estimate_vdot(predict_time(d, v)) 应等于 v ==")
for v in (50.0, 52.4, 53.8, 55.0):
    out = []
    for d in vd.DISTANCES.values():
        t = vd.predict_time(d, v)
        out.append(f"{vd.estimate_vdot(d, t):.2f}")
    print(f"  v={v:<5} → " + "  ".join(out))

print("\n== 反查：某个预测成绩对应多少 VDOT ==")
for label, t in (("5K", 1147.08), ("10K", 2393.86), ("半马", 5272.76), ("全马", 10996.0)):
    d = vd.DISTANCES[label]
    print(f"  quick {label} {fmt(t)} → VDOT {vd.estimate_vdot(d, t):.2f}")
for label, t in (("5K", 1097.08), ("10K", 2433.87), ("半马", 5048.08), ("全马", 10770.77)):
    d = vd.DISTANCES[label]
    print(f"  long  {label} {fmt(t)} → VDOT {vd.estimate_vdot(d, t):.2f}")
