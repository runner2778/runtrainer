"""探测真实活动课程分类分布与样本标签。"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database

config.ensure_dirs()
database.migrate()

from runtrainer.api.bridge import Api

rows = Api().list_activities(limit=800)["data"]
kinds = Counter(a["workout"]["kind"] for a in rows)
print("总数:", len(rows))
print("分类分布:", dict(kinds))
print()
# 有分段结构的样本
with_struct = [a for a in rows if a["workout"]["seg_kinds"] and any(
    k for k in a["workout"]["seg_kinds"] if k)]
print(f"有细分段的活动: {len(with_struct)}")
for a in with_struct[:12]:
    w = a["workout"]
    print(f"  {a['start_ts']} {a['name'][:16]:<16} -> {w['kind']:<10} {w['label']}")
print()
for a in rows[:8]:
    w = a["workout"]
    print(f"  {a['name'][:14]:<14} {w['label']} (hr_pct={w['hr_pct']})")
