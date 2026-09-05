"""数据库修复状态检查：字段恢复、回填进度、同步游标。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo, plan_repo, sync_repo

config.ensure_dirs()
database.migrate()

acts = activity_repo.list_activities(limit=10000)
real = [a for a in acts if a["source"] == "garmin" and not str(a["external_id"]).startswith("mock_")]
print(f"总活动={len(acts)} 真实={len(real)}")
print(f"  distance 非空={sum(1 for a in real if a['distance_m'])}")
print(f"  avg_hr 非空={sum(1 for a in real if a['avg_hr'])}")
print(f"  has_samples=1={sum(1 for a in real if a['has_samples'])}")
print(f"  structure_json 非空={sum(1 for a in real if a['structure_json'])}")
st = sync_repo.get_sync_state("garmin")
print(f"同步状态: last_sync_ts={st['last_sync_ts']} error={st['last_error']!r}")
print(f"  meta={st['meta_json']}")
plan = plan_repo.get_active_plan()
print(f"活动计划: {json.dumps({k: plan.get(k) for k in ('id','vdot','race_date','status','generated_at')} if plan else None, ensure_ascii=False)}")
