"""按当前智能判断时期重建 active 计划（一次性工具）。

背景：旧计划是机械分期（11 周全周期，当前仍处于基础期），与近期实际
训练分布（强度课 ≥3/周、跑量维持 → 最终强度期）不符。用向导同款
start_phase="auto" 参数重建，沿用原目标与计划参数。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import goal_repo, plan_repo
from runtrainer.domain.phase_estimator import PHASE_NAMES
from runtrainer.services import plan_service

config.ensure_dirs()
database.migrate()

plan = plan_repo.get_active_plan()
if not plan:
    print("无 active 计划")
    sys.exit(1)
goal = goal_repo.get_goal(plan["goal_id"])
params = {
    "goal": {"distance_m": goal["distance_m"], "race_date": goal["race_date"],
             "target_seconds": goal.get("target_seconds"), "vdot": None,
             "name": goal.get("name")},
    "plan": {"start_date": plan["start_date"], "weeks": plan["total_weeks"],
             "base_weekly_km": plan["base_weekly_km"], "run_days": plan["run_days"],
             "long_run_weekday": plan["long_run_weekday"], "start_phase": "auto"},
}
payload = plan_service.create_goal_and_plan(params)
sp = payload["start_phase"]
print(f"重建完成: start_phase={sp}（{PHASE_NAMES.get(sp)}） "
      f"phase_weeks={payload['phase_weeks']} vdot={payload['vdot']} "
      f"课数={len(payload['workouts'])}")
for w in payload["warnings"]:
    print("  警告:", w)
