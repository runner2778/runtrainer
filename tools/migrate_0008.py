"""真实库：用 sqlite3 backup API 做一致备份（应用可能正开着），再执行迁移。"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database

config.ensure_dirs()
db_path = Path(os.environ["APPDATA"]) / "RunTrainer" / "runtrainer.db"
bak_path = Path(os.environ["APPDATA"]) / "RunTrainer" / "runtrainer.db.bak-0008"

src = sqlite3.connect(db_path)
dst = sqlite3.connect(bak_path)
with dst:
    src.backup(dst)
dst.close()
print("backup ok:", bak_path)

database.migrate()
conn = sqlite3.connect(db_path)
print("user_version =", conn.execute("PRAGMA user_version").fetchone()[0])
cols = [r[1] for r in conn.execute("PRAGMA table_info(training_plans)")]
print("pro_mode in training_plans:", "pro_mode" in cols)
conn.close()
