"""全库分类审计：max_hr 现状、采样峰值分布、异常活动识别。

只读不写。输出三块：
1. profile.max_hr 与活动 max_hr 覆盖
2. 采样级最大心率分布（推断真实 max_hr 的候选）
3. 分类可疑活动（无结构但配速快、或 avg_hr/max_hr 比例异常）
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.config import DATA_DIR  # noqa: E402

db = Path(DATA_DIR) / "runtrainer.db"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row


def q(sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


print("=== 1. profile ===")
print(q("SELECT birth_year, max_hr, rest_hr, run_experience, vo2max FROM profile"))

print("\n=== 2. 活动 max_hr / avg_hr 覆盖 ===")
rows = q("""SELECT COUNT(*) n,
    SUM(max_hr IS NOT NULL) has_max,
    SUM(avg_hr IS NOT NULL) has_avg,
    SUM(has_samples=1) has_samples,
    SUM(structure_json IS NOT NULL) has_struct
    FROM activities WHERE source='garmin'""")
print(rows)

print("\n=== 3. 活动 max_hr 分布（非空） ===")
for r in q("""SELECT MAX(max_hr) mx, MIN(max_hr) mn, AVG(max_hr) av
              FROM activities WHERE source='garmin' AND max_hr IS NOT NULL"""):
    print(r)

print("\n=== 4. 活动 max_hr TOP 30（近期） ===")
for r in q("""SELECT date(start_ts,'unixepoch','localtime') d, name, max_hr, avg_hr,
                     ROUND(distance_m/1000.0,1) km
              FROM activities WHERE source='garmin' AND max_hr IS NOT NULL
              ORDER BY max_hr DESC LIMIT 30"""):
    print(f"{r['d']}  {r['name'][:28]:30} max={r['max_hr']:>3} avg={r['avg_hr']:>3} {r['km']}km")

print("\n=== 5. 采样级最大心率（每活动取采样峰值） TOP 30 ===")
for r in q("""SELECT date(a.start_ts,'unixepoch','localtime') d, a.name,
                     MAX(s.hr) smax, a.max_hr, a.avg_hr, a.has_samples
              FROM activities a JOIN activity_samples s ON s.activity_id=a.id
              WHERE a.source='garmin'
              GROUP BY a.id ORDER BY smax DESC LIMIT 30"""):
    print(f"{r['d']}  {r['name'][:28]:30} smax={r['smax']:>3} act_max={r['max_hr']} avg={r['avg_hr']:>3}")

print("\n=== 6. 采样峰值分布（分位数） ===")
for r in q("""SELECT COUNT(*) n,
    MAX(smax) mx,
    (SELECT smax FROM (SELECT MAX(s.hr) smax FROM activities a JOIN activity_samples s ON s.activity_id=a.id
      WHERE a.source='garmin' GROUP BY a.id ORDER BY smax DESC LIMIT 1 OFFSET (SELECT CAST(0.01*(SELECT COUNT(*) FROM (SELECT a.id FROM activities a JOIN activity_samples s ON s.activity_id=a.id WHERE a.source='garmin' GROUP BY a.id)) AS INT)))) p99,
    (SELECT smax FROM (SELECT MAX(s.hr) smax FROM activities a JOIN activity_samples s ON s.activity_id=a.id
      WHERE a.source='garmin' GROUP BY a.id ORDER BY smax DESC LIMIT 1 OFFSET (SELECT CAST(0.05*(SELECT COUNT(*) FROM (SELECT a.id FROM activities a JOIN activity_samples s ON s.activity_id=a.id WHERE a.source='garmin' GROUP BY a.id)) AS INT)))) p95
    FROM (SELECT MAX(s.hr) smax FROM activities a JOIN activity_samples s ON s.activity_id=a.id
      WHERE a.source='garmin' GROUP BY a.id)"""):
    print(r)

print("\n=== 7. 分类分布 ===")
for r in q("""SELECT json_extract(structure_json,'$.kind') kind, COUNT(*) n
              FROM activities WHERE structure_json IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""):
    print(r)

print("\n=== 8. 无结构但速度快的活动（可能漏识别间歇） ===")
for r in q("""SELECT date(start_ts,'unixepoch','localtime') d, name,
                     ROUND(avg_pace_s_km) pace, avg_hr, max_hr,
                     ROUND(distance_m/1000.0,1) km, has_samples
              FROM activities
              WHERE source='garmin' AND structure_json IS NULL
                AND avg_pace_s_km IS NOT NULL AND avg_pace_s_km < 330
              ORDER BY start_ts DESC LIMIT 40"""):
    print(f"{r['d']}  {r['name'][:28]:30} pace={r['pace']}s avg={r['avg_hr']} max={r['max_hr']} {r['km']}km samp={r['has_samples']}")

print("\n=== 9. 有结构但 avg_hr 极低（疑似误判 recovery/easy） ===")
for r in q("""SELECT date(start_ts,'unixepoch','localtime') d, name,
                     json_extract(structure_json,'$.kind') kind,
                     ROUND(avg_pace_s_km) pace, avg_hr, max_hr, ROUND(distance_m/1000.0,1) km
              FROM activities
              WHERE source='garmin' AND structure_json IS NOT NULL
                AND avg_hr IS NOT NULL AND avg_hr > 150
                AND json_extract(structure_json,'$.kind') IN ('recovery','easy')
              ORDER BY avg_hr DESC LIMIT 30"""):
    print(f"{r['d']}  {r['name'][:28]:30} {r['kind']:10} pace={r['pace']}s avg={r['avg_hr']} max={r['max_hr']} {r['km']}km")

conn.close()
