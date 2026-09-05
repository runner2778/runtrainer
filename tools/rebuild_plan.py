"""强制重建 active 计划（绕过 VDOT 阈值）：修复旧段落 duration_min=null 的课表。

沿用原目标/比赛日/周数/跑量参数，保留过去日期的完成状态与关联活动。
可选参数：double_days(0-2) double_mode(threshold|easy|auto) strength_days(0-2)
pro(0|1 职业双练模式)，未提供时沿用现计划的值。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import goal_repo, plan_repo
from runtrainer.domain.plan_engine import pro_extra_km
from runtrainer.services import plan_service
from runtrainer.utils import dates

config.ensure_dirs()
database.migrate()

plan = plan_repo.get_active_plan()
if not plan:
    print("无 active 计划")
    sys.exit(1)
goal = goal_repo.get_goal(plan["goal_id"])
argv = sys.argv[1:]
# 现计划若已是职业模式，存储的 base_weekly_km 含展示上浮量，须按旧 VDOT 回退
raw_base = float(plan["base_weekly_km"] or 0)
if plan.get("pro_mode"):
    raw_base = max(1.0, raw_base - pro_extra_km(float(plan["vdot"] or 0)))
params = {
    "goal": {
        "distance_m": goal["distance_m"], "race_date": goal["race_date"],
        "target_seconds": goal.get("target_seconds"), "vdot": None,  # 强制自动重选
        "name": goal.get("name"),
    },
    "plan": {
        "start_date": plan["start_date"], "weeks": plan["total_weeks"],
        "base_weekly_km": raw_base, "run_days": plan["run_days"],
        "long_run_weekday": plan["long_run_weekday"],
        "double_days": int(argv[0]) if argv else (plan.get("double_days") or 0),
        "double_mode": argv[1] if len(argv) > 1 else (plan.get("double_mode") or "auto"),
        "strength_days": int(argv[2]) if len(argv) > 2 else (plan.get("strength_days") or 0),
        "pro_mode": int(argv[3]) if len(argv) > 3 else (plan.get("pro_mode") or 0),
    },
}
spec, warns, source = plan_service._build_spec(params)
res = plan_service.generate_plan(spec)
_, new_plan = plan_service._persist_plan(spec, res, source, goal)
print(f"新计划 id={new_plan['id']} vdot={new_plan['vdot']} source={source}")

# 保留过去日期的完成状态与关联活动（按 date+slot 匹配，一天两练各记各的）
old_ws = {(w["date"], w.get("slot") or 1): w for w in plan_repo.get_workouts(plan["id"])}
today = dates.today().isoformat()
carried = 0
for w in plan_repo.get_workouts(new_plan["id"]):
    ow = old_ws.get((w["date"], w.get("slot") or 1))
    if ow and ow["date"] < today and (ow["status"] != "planned" or ow.get("completed_activity_id")):
        plan_repo.set_workout_status(w["id"], ow["status"], ow.get("completed_activity_id"))
        carried += 1
print(f"carried={carried} warnings={warns}")
