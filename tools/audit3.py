"""审计 3：Garmin 课程名信号 vs 分类矛盾。只读。"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.config import DATA_DIR  # noqa: E402
from runtrainer.domain.workout_analysis import classify_workout  # noqa: E402
from runtrainer.utils import jsonutil  # noqa: E402

c = sqlite3.connect(Path(DATA_DIR) / "runtrainer.db")
c.row_factory = sqlite3.Row

NAME_SIGNAL = {"乳酸阈值": "tempo", "阈值": "tempo", "冲刺": "interval",
               "间歇": "interval", "恢复": "recovery", "基础训练": "easy",
               "长距离": "easy"}

rows = c.execute("""SELECT id, name, start_ts, duration_s, distance_m, avg_pace_s_km,
                    avg_hr, max_hr, structure_json FROM activities WHERE source='garmin'""").fetchall()
print(f"总活动 {len(rows)}，含课程名信号的: ")
mismatch = []
for r in rows:
    sig = None
    for kw, kind in NAME_SIGNAL.items():
        if kw in (r["name"] or ""):
            sig = kind
            break
    if not sig:
        continue
    segs = jsonutil.loads(r["structure_json"]) or []
    w = classify_workout(segs, r["duration_s"], r["distance_m"], r["avg_hr"], 201, rest_hr=42)
    d = r["start_ts"]
    from datetime import datetime
    ds = datetime.fromtimestamp(d).strftime("%Y-%m-%d")
    if w["kind"] != sig:
        mismatch.append((ds, r["name"], w["kind"], w["label"], sig,
                         round(r["avg_pace_s_km"] or 0), r["avg_hr"], round((r["distance_m"] or 0) / 1000, 1)))

print(f"信号与分类矛盾 {len(mismatch)} 条:")
for ds, name, kind, label, sig, pace, hr, km in mismatch:
    print(f"  {ds}  {name[:26]:28} 信号={sig:9} 分类={kind:9} hr={hr} pace={pace}s {km}km")

c.close()
