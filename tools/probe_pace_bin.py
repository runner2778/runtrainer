"""探测 list_pace_bin_hr 真实数据形状（30s 配速梯度 × 时期对照）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database

config.ensure_dirs()
database.migrate()

from runtrainer.api.bridge import Api

r = Api().list_pace_bin_hr(120)
print("ok:", r["ok"], r.get("error"))
if not r["ok"]:
    sys.exit(1)
d = r["data"]
print("bins:", [(b["start_s"], b["end_s"]) for b in d["bins"]])
for p in d["periods"]:
    print(f"  {p['label']} ({p['start']}~{p['end']})")
    for i, h in enumerate(p["hr"]):
        if h is not None:
            print(f"    {d['bins'][i]['start_s']}s 档: HR={h} runs={p['runs'][i]} "
                  f"km={p['distance_km'][i]}")
