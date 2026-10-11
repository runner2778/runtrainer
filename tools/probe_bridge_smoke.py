"""新桥接接口的只读冒烟：确认方法存在、返回结构可用（不写真实库）。

写操作（add_manual_activity / sync_plan_paces / delete）走 pytest 临时库，
这里只碰 read-only 的 get_pace_sync_status 与 OCR 文本解析。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtrainer.db import database  # noqa: E402

database.migrate()

from runtrainer.api.bridge import Api  # noqa: E402

api = Api()

names = [n for n in dir(Api) if not n.startswith("_")]
new = ["add_manual_activity", "update_activity_fields", "delete_activity_record",
       "recognize_activity_screenshot", "get_pace_sync_status", "sync_plan_paces"]
print("桥接方法存在性：")
for n in new:
    print(f"  {'OK ' if n in names else '缺失'} {n}")

print("\n== get_pace_sync_status（只读）==")
r = api.get_pace_sync_status()
print("  ok=%s" % r.get("ok"))
if r.get("ok"):
    d = r["data"]
    for k in ("available", "plan_vdot", "target_vdot", "delta", "needs_sync",
              "stale_workouts", "total_workouts", "locked", "as_of"):
        print(f"    {k} = {d.get(k)}")
    if d.get("samples"):
        print("    样例:", json.dumps(d["samples"][:3], ensure_ascii=False))
else:
    print("  error:", r.get("error"))

print("\n== run_quick_assessment 的字段（不落库改动，仅确认结构）==")
import inspect  # noqa: E402
src = inspect.getsource(Api.run_quick_assessment)
print("  含 pace_sync 调用:", "sync_plan_paces" in src)

print("\n== 活动列表带新字段 ==")
r = api.list_activities(limit=3)
if r.get("ok"):
    for a in r["data"][:3]:
        print("  ", {k: a.get(k) for k in ("id", "name", "manual", "feel", "rpe", "note")})
else:
    print("  error:", r.get("error"))
