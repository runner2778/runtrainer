"""看冲刺训练课为何漏识别为匀速（结构/圈数据）。只读。"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.config import DATA_DIR  # noqa: E402
from runtrainer.utils import jsonutil  # noqa: E402

c = sqlite3.connect(Path(DATA_DIR) / "runtrainer.db")
c.row_factory = sqlite3.Row
rows = c.execute("""SELECT id, name, start_ts, duration_s, distance_m, structure_json, laps_json
                    FROM activities WHERE source='garmin' AND name LIKE '%冲刺%'""").fetchall()
for r in rows:
    from datetime import datetime
    d = datetime.fromtimestamp(r["start_ts"]).strftime("%Y-%m-%d")
    segs = jsonutil.loads(r["structure_json"]) or []
    laps = jsonutil.loads(r["laps_json"]) or []
    print(f"\n{d} {r['name']}  dur={r['duration_s']}s dist={r['distance_m']}m"
          f"  laps={len(laps)} segs={len(segs)}")
    for s in segs:
        print("   seg:", {k: (round(v, 1) if isinstance(v, float) else v)
                          for k, v in s.items()})
    for l in laps[:12]:
        print("   lap:", {k: (round(v, 1) if isinstance(v, float) else v)
                          for k, v in l.items() if k != "start_ts"})
c.close()
