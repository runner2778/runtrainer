"""趋势回归样本体检：structure 分布与疑似失真的快配速样本。"""
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo
from runtrainer.utils import dates, jsonutil

config.ensure_dirs()
database.migrate()

start = (dates.today() - timedelta(days=90)).isoformat()
acts = activity_repo.list_activities(start, None, None, 500, 0)
print(f"90 天内活动: {len(acts)}")

with_struct = 0
with_work = 0
rows = []
for a in acts:
    st = jsonutil.loads(a.get("structure_json")) if a.get("structure_json") else []
    if st:
        with_struct += 1
    if any(s.get("type") == "work" for s in st):
        with_work += 1
    rows.append((a["name"], a["avg_pace_s_km"], a["avg_hr"], len(st),
                 "W" if with_work and len(st) else "-"))
print(f"有 structure: {with_struct} / 含 work 段: {with_work}")
print("名称 | 配速 s/km | avg_hr | 分段数")
for r in sorted(rows, key=lambda x: (x[1] or 9999))[:40]:
    print(f"  {r[0][:20]:20s} {r[1]:>7} {r[2]:>7} {r[3]:>3} {r[4]}")
