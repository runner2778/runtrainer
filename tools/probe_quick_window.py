"""一键评估口径选型：窗口取多宽，间歇/速度能力才真的有样本？
只读不落库，对比 21/42/56/90/180 天 + QUICK_WEIGHTS 的 VDOT 与在场分量。
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
from runtrainer.utils import dates, jsonutil  # noqa: E402

today = dates.today()
prof = profile_repo.get_profile() or {}
max_hr = prof.get("max_hr") or estimate_max_hr(prof.get("birth_year"))
rest_hr = prof.get("rest_hr")

allacts = []
for a in activity_repo.list_activities((today - timedelta(days=364)).isoformat(), limit=3000):
    a = dict(a)
    raw = a.get("structure_json")
    a["structure"] = jsonutil.loads(raw) if raw else []
    if a.get("start_ts"):
        a["date"] = dates.ts_to_date(a["start_ts"]).isoformat()
    allacts.append(a)
print("全年活动:", len(allacts))
year_bests = ab.distance_bests(allacts, max_hr=max_hr)
print("年度 PB:", [(b["distance"], b["best_seconds"], b["date"], b.get("vdot"))
                 for b in year_bests])

# 计划完成度（与 ability_assessment 一致）
from runtrainer.domain import load_metrics  # noqa: E402
from runtrainer.db.repos import plan_repo  # noqa: E402
plan = plan_repo.get_active_plan()
plan_exec = None
if plan:
    rows = plan_repo.get_workouts(
        plan["id"], (today - timedelta(days=ab.QUALITY_WINDOW_DAYS)).isoformat(),
        today.isoformat())
    rows = [w for w in rows if plan["start_date"] <= w["date"] <= plan["race_date"]]
    run_day = {}
    for a in allacts:
        if a.get("date") and a.get("distance_m") and load_metrics.is_running(a.get("sport")):
            run_day[a["date"]] = run_day.get(a["date"], 0.0) + a["distance_m"] / 1000.0
    plan_exec = ab.quality_execution(rows, today=today,
                                     auto_done=load_metrics.workout_auto_done(rows, run_day))
print("plan_exec:", plan_exec)

print("\n%-8s %-6s %-6s %-6s %-6s %-6s | %s" % (
    "窗口", "活动", "VDOT", "间歇", "分段", "比赛", "在场分量(证据 source=vd)"))
for w in (21, 28, 42, 56, 90, 180):
    lo = (today - timedelta(days=w)).isoformat()
    acts = [a for a in allacts if a.get("date", "") >= lo]
    est = ab.compute_ability(acts, prof.get("vo2max"), prof.get("max_hr"),
                             rest_hr=rest_hr, as_of=today, plan_exec=plan_exec,
                             year_bests=year_bests, weight_profile=ab.QUICK_WEIGHTS,
                             effort_window_days=w)
    ev = est.get("evidence") or []
    n_iv = sum(1 for e in ev if e["source"] == "interval_ability")
    items = " ".join("%s=%s" % (e["source"].replace("_ability", "").replace("_pace", ""),
                                e.get("vdot")) for e in ev)
    print("%-8s %-6d %-6s %-6d %-6s %-6s | %s" % (
        "%d天" % w, len(acts), est.get("vdot"),
        1 if n_iv else 0,
        1 if any(e["source"] == "recent_efforts" for e in ev) else 0,
        1 if any(e["source"] == "recent_race" for e in ev) else 0, items))

print("\n年度 PB 参照：5K 54.9 / 10K 51.2 / 半马 53.5；长期口径 VDOT 54.2")
print("QUICK_WEIGHTS:", ab.QUICK_WEIGHTS)
