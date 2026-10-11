"""只读：样本间隔 dt 的分布——为「暂停区间」判定挑阈值。

正常采样间隔应集中在一个常数附近；暂停会在时间轴上留下一段远大于该
常数的空档。这里统计每个活动内 dt 的中位与最大值倍数。
"""
from __future__ import annotations

import sys
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo  # noqa: E402

database.migrate()

acts = activity_repo.list_activities(limit=3000)
meds: list[float] = []
big_gaps: list[tuple[float, float, float]] = []
n_act = 0
for a in acts:
    if not a.get("has_samples"):
        continue
    s = activity_repo.get_samples(a["id"]) or []
    if len(s) < 10:
        continue
    dts, t_prev = [], None
    for x in s:
        t = float(x.get("t_offset_s") or 0)
        if t_prev is not None and t > t_prev:
            dts.append(t - t_prev)
        t_prev = t
    if len(dts) < 10:
        continue
    n_act += 1
    m = statistics.median(dts)
    meds.append(m)
    for dt in dts:
        if dt > 5 * m and dt > 20:
            big_gaps.append((a["id"], m, dt))

print(f"活动数={n_act}   各活动 dt 中位的分布：")
print(f"  中位 {statistics.median(meds):.1f}s  "
      f"p10 {sorted(meds)[len(meds) // 10]:.1f}  "
      f"p90 {sorted(meds)[len(meds) * 9 // 10]:.1f}  "
      f"最大 {max(meds):.1f}  最小 {min(meds):.1f}")

from collections import Counter  # noqa: E402
print("  dt 中位取值分布:", Counter(round(m) for m in meds).most_common(8))

print(f"\n「>5×中位 且 >20s」的大空档：{len(big_gaps)} 个，涉及 "
      f"{len({g[0] for g in big_gaps})} 个活动")
for aid, m, dt in sorted(big_gaps, key=lambda g: -g[2])[:12]:
    print(f"  id={aid:<5} 中位={m:.1f}s  空档={dt:.0f}s  = {dt / m:.1f}×中位")
