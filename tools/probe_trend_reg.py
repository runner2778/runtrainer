"""趋势回归样本诊断：打印过滤后的 (pace, hr) 点与 Theil-Sen 结果。"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo
from runtrainer.domain import ability as ab
from runtrainer.utils import dates, jsonutil

config.ensure_dirs()
database.migrate()

start = (dates.today() - timedelta(days=90)).isoformat()
acts = activity_repo.list_activities(start, None, None, 500, 0)
acts = [{**a, "structure": jsonutil.loads(a.get("structure_json")) if a.get("structure_json") else []}
        for a in acts]

pts = [(a["avg_pace_s_km"], a["avg_hr"], a["name"]) for a in acts
       if a.get("avg_pace_s_km") and a.get("avg_hr")
       and a["avg_hr"] >= ab.TREND_MIN_HR
       and (a.get("duration_s") or 0) >= ab.TREND_MIN_DURATION_S
       and ab.TREND_PACE_MIN_S_KM <= a["avg_pace_s_km"] <= ab.TREND_PACE_MAX_S_KM
       and not any(s.get("type") == "work" for s in a["structure"])]
pts.sort(key=lambda p: p[0])
print(f"样本数: {len(pts)}  pace 范围: {pts[0][0]:.0f}–{pts[-1][0]:.0f} s/km")
print(f"hr 范围: {min(p[1] for p in pts)}–{max(p[1] for p in pts)}")
print("pace | hr | 名称")
for p, h, n in pts:
    print(f"  {p:6.1f} {h:5.0f}  {n[:24]}")

xs = [p for p, _, _ in pts]
ys = [h for _, h, _ in pts]
reg = ab._theil_sen(xs, ys)
if reg:
    slope, intercept = reg
    print(f"Theil-Sen: hr = {slope:.4f}*pace + {intercept:.1f}")
    for target_hr in (161, 169.84, 177):
        print(f"  {target_hr} bpm → pace {(target_hr - intercept) / slope:.0f} s/km")
    # 最小二乘对比
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    print(f"OLS: hr = {sxy / sxx:.4f}*pace + {my - (sxy / sxx) * mx:.1f}")
