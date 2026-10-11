"""只读：当前目标 / 课表概况 / 各类「预估」字段一览。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import goal_repo, plan_repo  # noqa: E402

database.migrate()

print("== 当前目标 ==")
g = goal_repo.get_active_goal()
print(json.dumps(g, ensure_ascii=False, indent=2) if g else "无")

print("\n== 课表 ==")
p = plan_repo.get_active_plan()
if p:
    print(json.dumps({k: v for k, v in p.items() if k != "structure_json"},
                     ensure_ascii=False, indent=2)[:1500])
    ws = plan_repo.get_workouts(p["id"])
    print(f"课节 {len(ws)}")
    print("前 3 节:", json.dumps(ws[:3], ensure_ascii=False)[:800])
else:
    print("无")
