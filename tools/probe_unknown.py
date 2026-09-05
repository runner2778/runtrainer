"""探测 unknown 分类构成：缺心率 vs 窗口外无结构。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo

config.ensure_dirs()
database.migrate()

rows = activity_repo.list_activities(limit=2000)
cutoff = int((datetime.now(timezone.utc) - timedelta(days=180)).timestamp())
no_hr = [a for a in rows if a["avg_hr"] is None]
print("total:", len(rows))
print("missing avg_hr:", len(no_hr), "of which before 180d:", sum(1 for a in no_hr if a["start_ts"] < cutoff))
print("before 180d:", sum(1 for a in rows if a["start_ts"] < cutoff))
print("before 180d with structure:", sum(1 for a in rows if a["start_ts"] < cutoff and a["structure_json"]))
print("before 180d with samples:", sum(1 for a in rows if a["start_ts"] < cutoff and a["has_samples"]))
