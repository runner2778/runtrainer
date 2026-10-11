"""只读：审计某次活动的最快分段窗口——窗口距离/用时/配速/心率与停表占比。

用于核对「日常跑步最快分段」证据是否被停表/采样稀疏污染。
用法：python tools/probe_best_effort_window.py <activity_id> [max_hr]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402

database.migrate()

aid = int(sys.argv[1])
max_hr = float(sys.argv[2]) if len(sys.argv) > 2 else 201.0

samples = activity_repo.get_samples(aid) or []
act = None
for a in activity_repo.list_activities(limit=3000):
    if a.get("id") == aid:
        act = a
        break

print(f"活动 {aid} «{(act or {}).get('name')}» "
      f"距离={((act or {}).get('distance_m') or 0) / 1000:.2f}km "
      f"用时={(act or {}).get('duration_s')}s 平均配速={((act or {}).get('avg_pace_s_km') or 0) / 60:.2f}min/km")
print(f"样本点 n={len(samples)}  首={samples[0] if samples else None}")
print(f"末={samples[-1] if samples else None}")

# 累计量：与窗口扫描同口径
tot_d, tot_t, stopped_t = 0.0, 0.0, 0.0
t_prev = None
for s in samples:
    t = float(s.get("t_offset_s") or 0)
    spd = float(s.get("speed_mps") or 0)
    dt = (t - t_prev) if t_prev is not None else 0
    if dt < 0:
        dt = 0
    if t_prev is not None:
        tot_d += max(spd, 0) * dt
        tot_t += dt
        if spd < 0.5:
            stopped_t += dt
    t_prev = t
print(f"采样积分距离={tot_d:.0f}m  采样时间跨度={tot_t:.0f}s  "
      f"其中「速度<0.5m/s」的时长={stopped_t:.0f}s ({stopped_t / max(tot_t, 1) * 100:.1f}%)")

wins = evm._best_effort_windows_for(samples, evm.BEFFORT_DISTS, max_hr)
print(f"\n窗口（≥{ab.SEG_MIN_HR_RATIO:.0%}HRmax 才有资格）:")
for label, (pace_s_m, hr_avg) in sorted(wins.items()):
    std = dict((lbl, m) for m, lbl in evm.BEFFORT_DISTS)[label]
    proj_s = pace_s_m * std
    print(f"  {label:>4}: 窗口配速={pace_s_m * 1000 / 60:.2f}min/km "
          f"投影{std / 1000:.0f}K用时={proj_s:.0f}s "
          f"心率={hr_avg if hr_avg is None else round(hr_avg)} "
          f"({0 if not hr_avg else hr_avg / max_hr:.2f}) "
          f"→ VDOT {evm.vdot_for_pace_proj(std, pace_s_m)}")

print("\n同算法（ability）对照:")
w2 = ab._best_effort_windows(samples, max_hr, evm.BEFFORT_DISTS)
for label, v in sorted((w2 or {}).items()):
    print(f"  {label}: {v}")
