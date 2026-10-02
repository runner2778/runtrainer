"""看真实库近 16 周 pace_bin_weekly 的候选档与默认档走势（段级口径）。只读。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.db.repos import activity_repo  # noqa: E402
from runtrainer.domain.workout_analysis import (  # noqa: E402
    PACE_TREND_EDGE_WEEKS, pace_bin_weekly, pace_hr_segments)

end = date.today()
# 与 bridge.list_pace_bin_weekly 同一取数窗口（多取 roll-1 周喂滚动窗）
start = end - timedelta(weeks=16 + PACE_TREND_EDGE_WEEKS - 1)
rows = activity_repo.list_structure_rows(start.isoformat(), end.isoformat())
segs = pace_hr_segments(rows)
out = pace_bin_weekly(segs, end, weeks=16)

print(f"活动 {len(rows)} 个 → 段级行 {len(segs)} 条")
print("bin_s:", out["bin_s"], " 周数:", len(out["weeks"]),
      " 滚动窗:", out["roll_weeks"], " 默认档:", out["default_start_s"])
print("周刻度:", [(w["label"], w["partial"]) for w in out["weeks"]])
print(f"\n下拉候选 {len(out['bins'])} 档:")
for b in out["bins"]:
    print(f"  {b['label']:>5}/km  段数={b['segs']:>3} 跑量={b['distance_km']:>6}km "
          f"首4周={b['early_hr']} 末4周={b['recent_hr']} Δ={b['drop']}")
    print(f"        hr     : {b['hr']}")
    print(f"        hr_roll: {b['hr_roll']}")
    print(f"        周段数 : {b['week_segs']}")
print()
for b in out["bins"][:2]:
    print(f"[{b['label']}] {b['note']}")
