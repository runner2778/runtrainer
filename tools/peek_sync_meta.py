"""查同步断点与健康表日期范围。只读。"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.config import DATA_DIR  # noqa: E402

c = sqlite3.connect(Path(DATA_DIR) / "runtrainer.db")
c.row_factory = sqlite3.Row
row = c.execute("SELECT meta_json, last_error FROM sync_state WHERE source='garmin'").fetchone()
meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
print("meta keys:", sorted(meta.keys()))
for k in ("cursor_ts", "last_health_date", "last_stats"):
    print(f"  {k} =", json.dumps(meta.get(k), ensure_ascii=False)[:300])
print("last_error:", row["last_error"])

r = c.execute("SELECT MIN(date) mn, MAX(date) mx, COUNT(*) n FROM daily_health").fetchone()
print(f"\ndaily_health: {r['n']} 行, {r['mn']} ~ {r['mx']}")
r = c.execute("SELECT date, COUNT(*) n FROM daily_health GROUP BY date ORDER BY date DESC LIMIT 5").fetchall()
print("最近 5 天:", [(x["date"], x["n"]) for x in r])
c.close()
