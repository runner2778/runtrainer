"""只读探针：当前生产口径的「成绩预估」到底保守到什么程度。

对比三件事：
  1. 生产入口（plan_service.ability_assessment / quick_assessment）给出的预测成绩
  2. 个人真实比赛成绩（近一年、按距离）
  3. 每距离的 PB（全库最快，含训练中的最快分段不算）
输出「预测 − 真实最好成绩」的差（正 = 预测比实际最好成绩慢 = 保守）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo, plan_repo, profile_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import vdot as vd  # noqa: E402
from runtrainer.services import plan_service  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

database.migrate()


def fmt(sec) -> str:
    if not sec:
        return "--"
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main() -> None:
    acts = activity_repo.list_activities(limit=3000)
    first = dates.ts_to_date(acts[-1]["start_ts"]).isoformat()
    last = dates.ts_to_date(acts[0]["start_ts"]).isoformat()
    print(f"活动 {len(acts)} 条  {first} → {last}")

    profile = profile_repo.get_profile() or {}
    est_max = ab.estimate_max_hr(profile.get("max_hr"), acts)
    print(f"档案最大心率 = {profile.get('max_hr')}  估算最大心率 = {est_max}")

    races = ab.detect_races(acts, est_max)
    print(f"\n== 检测到的比赛 {len(races)} 场 ==")
    for r in races:
        print(f"  {r['day']} {r['label']:>4} 实际 {fmt(r['duration_s'])}"
              f"  VDOT {r['actual_vdot']}  hr_ref={r['hr_ref']}  {r['name']}")

    print("\n== 生产口径（长窗口） ==")
    long_a = plan_service.ability_assessment()
    print("  vdot =", long_a.get("vdot"), " model =", long_a.get("model"),
          " window =", long_a.get("window_days"))
    print("  predictions =", json.dumps(long_a.get("predictions"), ensure_ascii=False))
    print("  intervals   =", json.dumps(long_a.get("prediction_intervals"), ensure_ascii=False))
    print("  year_bests  =", json.dumps(long_a.get("year_bests"), ensure_ascii=False)[:600])
    print("  personal_races =", json.dumps(long_a.get("personal_races"), ensure_ascii=False)[:800])

    print("\n== 生产口径（近三周快评） ==")
    quick = plan_service.quick_assessment()
    print("  vdot =", quick.get("vdot"), " window =", quick.get("window_days"))
    print("  predictions =", json.dumps(quick.get("predictions"), ensure_ascii=False))
    print("  intervals   =", json.dumps(quick.get("prediction_intervals"), ensure_ascii=False))
    print("  personal_races =", json.dumps(quick.get("personal_races"), ensure_ascii=False)[:800])

    plan = plan_repo.get_active_plan()
    print("\n== 课表 ==")
    print("  plan vdot =", (plan or {}).get("vdot"), " 生成于", (plan or {}).get("generated_at"))

    print("\n== 证据明细（长窗口） ==")
    for e in (long_a.get("evidence") or []):
        print(f"  - {e.get('source')} :: {str(e.get('detail'))[:90]}")


if __name__ == "__main__":
    main()
