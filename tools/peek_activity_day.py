"""只读：检查给定日期的活动，定位最快分段档的离群来源。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo, profile_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

database.migrate()


def fmt_pace(p) -> str:
    if not p:
        return "—"
    m, s = divmod(int(round(p)), 60)
    return f"{m}:{s:02d}/km"


def main() -> None:
    days = sys.argv[1:] or ["2026-04-25", "2026-06-10"]
    acts = activity_repo.list_activities(limit=3000)
    prof = profile_repo.get_profile() or {}
    est_max = ab.estimate_max_hr(prof.get("max_hr"), acts)
    print(f"est_max_hr = {est_max}\n")
    for d in days:
        print(f"== {d} ==")
        for a in acts:
            if dates.ts_to_date(a["start_ts"]).isoformat() != d:
                continue
            print(f"  id={a.get('id')} «{a.get('name')}» type={a.get('type')} "
                  f"dist={a.get('distance_m')} dur={a.get('duration_s')} "
                  f"pace={fmt_pace(a.get('avg_pace_s_km'))} hr={a.get('avg_hr')} "
                  f"gain={a.get('elevation_gain_m')} cad={a.get('avg_cadence')}")
            if est_max and a.get("avg_hr"):
                print(f"    hr_ratio = {a['avg_hr'] / est_max:.3f}"
                      f"   跑步机={evm.is_treadmill(a)}")
            print(f"    env_adj_pace = {fmt_pace(evm.env_adjusted_pace(a, a.get('avg_pace_s_km')))}")


main()
