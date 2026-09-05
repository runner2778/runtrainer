"""计划段落形状分布：找出缺 distance/duration 的段落来源。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import plan_repo

config.ensure_dirs()
database.migrate()

plan = plan_repo.get_active_plan()
ws = plan_repo.get_workouts(plan["id"])
shapes = {}
for w in ws:
    segs = json.loads(w.get("segments_json") or "[]")
    key = json.dumps(segs, ensure_ascii=False)
    shapes[key] = shapes.get(key, 0) + 1
for k, n in sorted(shapes.items(), key=lambda kv: -kv[1])[:12]:
    print(n, k[:200])
print("--- 缺 distance/duration 的 continuous 段落所属课 ---")
for w in ws:
    segs = json.loads(w.get("segments_json") or "[]")
    for s in segs:
        if s.get("type") == "continuous" and not s.get("distance_km") and not s.get("duration_min"):
            print(w["date"], w["kind"], w["title"], "source=", w["source"],
                  "adj=", w["adjustment_id"], json.dumps(s, ensure_ascii=False))
            break
