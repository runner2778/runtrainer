"""审计 2：垃圾心率行 + 边界分类 + max_hr 推断候选。只读。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.config import DATA_DIR  # noqa: E402

c = sqlite3.connect(Path(DATA_DIR) / "runtrainer.db")
c.row_factory = sqlite3.Row

print("=== 垃圾 max_hr（<120 或 max<avg，传感器异常） ===")
for r in c.execute("""SELECT date(start_ts,'unixepoch','localtime') d, name, avg_hr, max_hr,
        ROUND(distance_m/1000.0,1) km, ROUND(avg_pace_s_km) pace
      FROM activities WHERE source='garmin'
        AND (max_hr < 120 OR max_hr < avg_hr) ORDER BY start_ts"""):
    print(f"{r['d']}  {r['name'][:28]:30} avg={r['avg_hr']:>4} max={r['max_hr']:>4} {r['km']:>5}km pace={r['pace']}")

print("\n=== max_hr < 120 计数 ===")
print(c.execute("""SELECT COUNT(*) FROM activities WHERE source='garmin' AND max_hr < 120""").fetchone()[0])

print("\n=== 每活动采样峰值 top5 / p99 / p95 ===")
peaks = [r[0] for r in c.execute(
    """SELECT MAX(s.hr) FROM activities a JOIN activity_samples s ON s.activity_id=a.id
       WHERE a.source='garmin' GROUP BY a.id""")]
peaks.sort(reverse=True)
print("n =", len(peaks))
print("top10:", peaks[:10])
print("top5 mean:", round(sum(peaks[:5]) / 5, 1))
print("p99:", peaks[max(0, int(len(peaks) * 0.01))])
print("p95:", peaks[max(0, int(len(peaks) * 0.05))])
print("低峰值(<160) 条数:", sum(1 for p in peaks if p < 160))

print("\n=== avg_hr 异常（<80 或 >190） ===")
for r in c.execute("""SELECT date(start_ts,'unixepoch','localtime') d, name, avg_hr, max_hr,
        ROUND(distance_m/1000.0,1) km FROM activities WHERE source='garmin'
        AND (avg_hr < 80 OR avg_hr > 190) ORDER BY start_ts"""):
    print(f"{r['d']}  {r['name'][:28]:30} avg={r['avg_hr']:>4} max={r['max_hr']:>4} {r['km']}km")

print("\n=== 当前 hr_pct 落在分类边界 ±2%（max_hr=201，可能因 max_hr 修正改类） ===")
for r in c.execute("""SELECT date(start_ts,'unixepoch','localtime') d, name, avg_hr, max_hr,
        ROUND(avg_pace_s_km) pace, ROUND(distance_m/1000.0,1) km
      FROM activities WHERE source='garmin' AND avg_hr IS NOT NULL
        AND (avg_hr/201.0 BETWEEN 0.60 AND 0.64
          OR avg_hr/201.0 BETWEEN 0.70 AND 0.74
          OR avg_hr/201.0 BETWEEN 0.80 AND 0.84
          OR avg_hr/201.0 BETWEEN 0.90 AND 0.94)
      ORDER BY start_ts DESC LIMIT 60"""):
    pct = r['avg_hr'] / 201.0
    print(f"{r['d']}  {r['name'][:28]:30} avg={r['avg_hr']:>4} pct={pct:.2f} pace={r['pace']} {r['km']}km")
c.close()
