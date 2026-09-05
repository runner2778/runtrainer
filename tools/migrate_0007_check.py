"""执行 0007 迁移并检查结果（真实库）。备份：runtrainer.db.bak-0007"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database

config.ensure_dirs()
database.migrate()

import sqlite3
c = sqlite3.connect(config.DB_PATH)
c.row_factory = sqlite3.Row
print("user_version =", c.execute("PRAGMA user_version").fetchone()[0])
cols = [r[1] for r in c.execute("PRAGMA table_info(planned_workouts)")]
print("planned_workouts cols =", cols)
assert "slot" in cols and "segments_json" in cols
tcols = [r[1] for r in c.execute("PRAGMA table_info(training_plans)")]
print("training_plans new cols =", [x for x in ("double_days", "double_mode", "strength_days") if x in tcols])
idx = c.execute("PRAGMA index_list(planned_workouts)").fetchall()
print("indexes =", [(r["name"], r["unique"]) for r in idx])
n = c.execute("SELECT COUNT(*) FROM planned_workouts").fetchone()[0]
slots = c.execute("SELECT slot, COUNT(*) FROM planned_workouts GROUP BY slot").fetchall()
print("workout count =", n, "by slot =", [tuple(r) for r in slots])
plan = c.execute("SELECT id, status, double_days, double_mode, strength_days FROM training_plans ORDER BY id DESC LIMIT 1").fetchone()
print("latest plan =", dict(plan))
