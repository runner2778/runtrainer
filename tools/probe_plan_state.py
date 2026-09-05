"""诊断：完成记录是否引用已被清理的活动（悬空引用）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.database import get_conn

config.ensure_dirs()
database.migrate()
with get_conn() as conn:
    dangling = conn.execute(
        "SELECT COUNT(*) FROM planned_workouts w LEFT JOIN activities a ON a.id = w.completed_activity_id "
        "WHERE w.completed_activity_id IS NOT NULL AND a.id IS NULL").fetchone()[0]
    total_done = conn.execute("SELECT COUNT(*) FROM planned_workouts WHERE status='completed'").fetchone()[0]
    print(f"completed workouts: {total_done} | dangling refs: {dangling}")
    rows = conn.execute(
        "SELECT w.plan_id, w.date, w.status, w.completed_activity_id FROM planned_workouts w "
        "WHERE w.status='completed' OR w.completed_activity_id IS NOT NULL ORDER BY w.date LIMIT 20").fetchall()
    for r in rows:
        print(dict(r))
    n = conn.execute("SELECT COUNT(*) FROM planned_workouts").fetchone()[0]
    print(f"total workouts: {n}")
