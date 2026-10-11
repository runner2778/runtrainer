"""只读：审计「样本时间跨度 vs 活动用时」——最快分段窗口的用时口径。

若样本 t_offset_s 的跨度系统性大于 duration_s（含停表/暂停），窗口配速
t/d 就会被拉慢，最快分段 VDOT 被低估，且越长的窗口越严重。
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

acts = activity_repo.list_activities(limit=3000)
prof_hr = 201.0

rows = []
for a in acts:
    if not a.get("has_samples") or not a.get("distance_m"):
        continue
    dur = a.get("duration_s") or 0
    if dur <= 0:
        continue
    s = activity_repo.get_samples(a["id"]) or []
    if len(s) < 2:
        continue
    span = float(s[-1].get("t_offset_s") or 0) - float(s[0].get("t_offset_s") or 0)
    if span <= 0:
        continue
    tot_d, t_prev = 0.0, None
    for x in s:
        t = float(x.get("t_offset_s") or 0)
        spd = float(x.get("speed_mps") or 0)
        if t_prev is not None:
            tot_d += max(spd, 0) * max(t - t_prev, 0)
        t_prev = t
    rows.append({"id": a["id"], "day": a.get("start_ts"), "name": a.get("name"),
                 "dist": a["distance_m"], "dur": dur, "span": span,
                 "ratio": span / dur, "int_d": tot_d,
                 "d_ratio": tot_d / a["distance_m"]})

rows.sort(key=lambda r: -r["ratio"])
print(f"有样本的活动 {len(rows)} 个；样本跨度/用时 的分布：")
import statistics  # noqa: E402
ratios = [r["ratio"] for r in rows]
print(f"  中位 {statistics.median(ratios):.4f}  均值 {statistics.mean(ratios):.4f}  "
      f"最大 {max(ratios):.4f}  最小 {min(ratios):.4f}")
over = [r for r in rows if r["ratio"] > 1.02]
print(f"  跨度超过用时 2% 的活动：{len(over)} 个")
print(f"\n积分距离/记录距离 中位 {statistics.median([r['d_ratio'] for r in rows]):.4f}")

print("\n跨度/用时 最大的 15 个：")
for r in rows[:15]:
    print(f"  id={r['id']:<5} 距离={r['dist'] / 1000:6.2f}km 用时={r['dur']:7.0f}s "
          f"样本跨度={r['span']:7.0f}s 比={r['ratio']:.3f} "
          f"积分距离比={r['d_ratio']:.3f} «{r['name']}»")

print("\n对 best_effort 的影响（抽查跨度比 >1.02 且距离 ≥14km 的活动）：")
for r in [x for x in over if x["dist"] >= 14000][:10]:
    s = activity_repo.get_samples(r["id"])
    wins = evm._best_effort_windows_for(s, evm.BEFFORT_DISTS, prof_hr)
    parts = []
    for label, (psm, hr) in sorted(wins.items()):
        std = dict((l, m) for m, l in evm.BEFFORT_DISTS)[label]
        parts.append(f"{label}:{psm * 1000 / 60:.2f}/{evm.vdot_for_pace_proj(std, psm)}")
    # 用 duration 口径重算（把窗口用时按 ratio 缩回移动时间）
    print(f"  id={r['id']} «{r['name']}» 比={r['ratio']:.3f}  " + "  ".join(parts))
