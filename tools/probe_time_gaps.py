"""只读：某活动的「样本跨度 − 用时」多出来的时间都花在哪几个区间。

若集中在少数大 dt 且该处速度仍非零 → 是采样缺口/暂停，时间口径与 distance
不一致；若分散在速度≈0 的区间 → 是正常停表。
用法：python tools/probe_time_gaps.py <activity_id> [topN]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo  # noqa: E402

database.migrate()

aid = int(sys.argv[1])
topn = int(sys.argv[2]) if len(sys.argv) > 2 else 12

act = next((a for a in activity_repo.list_activities(limit=3000) if a.get("id") == aid), {})
s = activity_repo.get_samples(aid) or []
dur = act.get("duration_s") or 0
span = float(s[-1]["t_offset_s"]) - float(s[0]["t_offset_s"])
print(f"活动 {aid} «{act.get('name')}» 距离={act.get('distance_m')}m "
      f"用时={dur}s 样本跨度={span}s 差={span - dur:.0f}s")

gaps = []
t_prev = None
for x in s:
    t = float(x.get("t_offset_s") or 0)
    if t_prev is not None:
        gaps.append((t - t_prev, t_prev, t, float(x.get("speed_mps") or 0),
                     float(x.get("hr") or 0)))
    t_prev = t
gaps.sort(key=lambda g: -g[0])
print("\n最大的 dt 区间（dt, 起, 止, 该区间起点速度m/s, 心率）：")
tot_extra = 0.0
for dt, a0, b0, spd, hr in gaps[:topn]:
    d_est = spd * dt
    print(f"  dt={dt:6.1f}s  t={a0:6.0f}→{b0:6.0f}  speed={spd:5.2f}m/s "
          f"→ 该段距离≈{d_est:6.0f}m  心率={hr:.0f}")

print(f"\ndt 中位={sorted(g[0] for g in gaps)[len(gaps) // 2]:.1f}s  "
      f"dt>2s 的区间数={sum(1 for g in gaps if g[0] > 2)}/{len(gaps)}")
big = [g for g in gaps if g[0] > 2]
print(f"dt>2s 的区间合计 {sum(g[0] for g in big):.0f}s，"
      f"其中速度<0.5m/s 的合计 {sum(g[0] for g in big if g[3] < 0.5):.0f}s")
print(f"「多出来的时间」= {span - dur:.0f}s")
