"""一次性修正传感器垃圾行：心率/配速不可信的活动清掉对应字段。

标准：max_hr < 120 且距离 < 1km（腕式传感器脱落/误录，心率不是跑步心率）；
或 0 距离但配速荒谬（>20:00/km）。活动记录与结构保留，仅清字段，
前端对这些活动显示「—」，分类回落为「匀速跑（缺心率数据）」。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.config import DATA_DIR  # noqa: E402

conn = sqlite3.connect(Path(DATA_DIR) / "runtrainer.db")
conn.row_factory = sqlite3.Row

# 1) 心率垃圾
bad_hr = [dict(r) for r in conn.execute(
    """SELECT id, date(start_ts,'unixepoch','localtime') d, name, avg_hr, max_hr,
              ROUND(avg_pace_s_km) pace, ROUND(distance_m/1000.0,1) km
       FROM activities WHERE source='garmin' AND max_hr IS NOT NULL
         AND max_hr < 120 AND distance_m < 1000""")]
# 2) 配速垃圾
bad_pace = [dict(r) for r in conn.execute(
    """SELECT id, date(start_ts,'unixepoch','localtime') d, name, avg_hr, max_hr,
              ROUND(avg_pace_s_km) pace, ROUND(distance_m/1000.0,1) km
       FROM activities WHERE source='garmin' AND avg_pace_s_km IS NOT NULL
         AND (avg_pace_s_km > 1200 OR (distance_m <= 0 AND avg_pace_s_km < 150))""")]

print("将清理（心率字段置 NULL）:")
for r in bad_hr:
    print(f"  {r['d']}  {r['name'][:28]:30} avg={r['avg_hr']} max={r['max_hr']} {r['km']}km pace={r['pace']}")
print("将清理（配速字段置 NULL）:")
for r in bad_pace:
    print(f"  {r['d']}  {r['name'][:28]:30} avg={r['avg_hr']} max={r['max_hr']} {r['km']}km pace={r['pace']}")

ids_hr = {r["id"] for r in bad_hr}
ids_pace = {r["id"] for r in bad_pace}
if ids_hr:
    conn.execute(f"UPDATE activities SET avg_hr=NULL, max_hr=NULL WHERE id IN ({','.join('?' * len(ids_hr))})",
                 tuple(ids_hr))
    print(f"\n心率字段清理 {len(ids_hr)} 条")
if ids_pace:
    conn.execute(f"UPDATE activities SET avg_pace_s_km=NULL WHERE id IN ({','.join('?' * len(ids_pace))})",
                 tuple(ids_pace))
    print(f"配速字段清理 {len(ids_pace)} 条")
conn.commit()
conn.close()
print("完成")
