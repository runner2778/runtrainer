"""诊断：一键评估（21 天 + QUICK_WEIGHTS）在真实库上为什么没出间歇分量。

只读，不落库。
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402

database.migrate()

from runtrainer.db.repos import activity_repo, profile_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain.workout_analysis import estimate_max_hr  # noqa: E402
from runtrainer.services import plan_service  # noqa: E402
from runtrainer.utils import dates, jsonutil  # noqa: E402

today = dates.today()
prof = profile_repo.get_profile() or {}
for w in (7, 21, 28, 30, 60, 180):
    lo = (today - timedelta(days=w)).isoformat()
    rows = [_ for _ in activity_repo.list_activities(lo, limit=3000)]
    seg_acts = 0
    nodefc = []
    for r in rows:
        segs = jsonutil.loads(r.get("structure_json")) or []
        if any(s.get("type") in ("work", "rest") for s in segs):
            seg_acts += 1
        if not r.get("structure_json"):
            nodefc.append(r.get("id"))
    print("窗口 %3d 天: 活动 %4d，含 work/rest 分段 %d 条，无 structure %d 条"
          % (w, len(rows), seg_acts, len(nodefc)))

r = [_ for _ in activity_repo.list_activities((today - timedelta(days=21)).isoformat(),
                                              limit=3000)]
max_hr = prof.get("max_hr") or estimate_max_hr(prof.get("birth_year"))
print("max_hr:", max_hr, "rest_hr:", prof.get("rest_hr"))
iv = ab.interval_ability(plan_service._decode_activity and
                         [a for a in r], max_hr, today) if False else None

# 用与 ability_assessment 相同的方式解码（含 structure 与 date）
acts = []
for a in r:
    a = dict(a)
    raw = a.get("structure_json")
    a["structure"] = jsonutil.loads(raw) if raw else []
    if a.get("start_ts"):
        a["date"] = dates.ts_to_date(a["start_ts"]).isoformat()
    acts.append(a)
print("21 天活动:", len(acts), "其中带 structure 的:", sum(1 for a in acts if a["structure"]))
iv = ab.interval_ability(acts, max_hr, today)
print("interval_ability:", iv)
for a in acts:
    if any(s.get("type") in ("work", "rest") for s in a["structure"]):
        print("  候选:", a["date"], a["name"], a["distance_m"], a["avg_hr"],
              [(s.get("type"), s.get("pace_s_km"), s.get("avg_hr")) for s in a["structure"]])

print("---- 分量在场情况（21 天 + 年度 PB）----")
print("threshold(trend):", ab.threshold_pace_from_trend(acts, max_hr, today))
print("cruise:", ab.cruise_ability(acts, max_hr, today))
print("hrr:", ab.hrr_ability(acts, max_hr, prof.get("rest_hr"), today))
print("quality:", ab.quality_workout_evidence(acts, max_hr, today))
print("race:", ab.best_recent_race(acts, max_hr))
yb = ab.distance_bests(acts, max_hr=max_hr)
print("21 天内的 distance_bests:", [(b["distance"], b["best_seconds"], b["date"],
                                     b["source"], b.get("vdot")) for b in yb])
year_bests = ab.distance_bests(
    plan_service.ability_assessment()["acts_year"], max_hr=max_hr)
print("年度 distance_bests:", [(b["distance"], b["best_seconds"], b["date"],
                                b["source"], b.get("vdot")) for b in year_bests])
print("efforts(21 天):", ab._recent_efforts_evidence(year_bests, today, 21))
