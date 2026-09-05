"""冲刺课速度分布：快段是否被采样捕捉。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo
from runtrainer.services import settings_service
from runtrainer.garmin.garminconnect_adapter import GarminConnectAdapter
from runtrainer.domain import workout_analysis as wa

config.ensure_dirs()
database.migrate()

adapter = GarminConnectAdapter(*settings_service.get_garmin_credentials(),
                               is_cn=settings_service.is_garmin_cn())
adapter.login()

eid = None
for a in activity_repo.list_activities(limit=5000):
    if a["name"] and "冲刺" in a["name"]:
        eid = a["external_id"]
        break
d = adapter.fetch_activity_detail(eid)
print("活动:", d.name, "dur=", d.duration_s, "dist=", d.distance_m)
rows = sorted((s for s in d.samples if s.get("t_offset_s") is not None and s.get("speed_mps")),
              key=lambda s: s["t_offset_s"])
spds = [s["speed_mps"] for s in rows]
print("采样数:", len(rows), "速度范围: %.2f ~ %.2f" % (min(spds), max(spds)))
run_spds = [v for v in spds if v >= wa.SAMPLE_RUN_MIN_MPS]
print("跑动采样(>=1.5):", len(run_spds), "基线(中位数):", sorted(run_spds)[len(run_spds) // 2] if run_spds else None)
hist = {}
for v in spds:
    b = int(v)
    hist[b] = hist.get(b, 0) + 1
for b in sorted(hist):
    print(f"  {b} m/s: {hist[b]}")
# 快于基线 12% 的游程
base = sorted(run_spds)[len(run_spds) // 2]
thr = base * wa.SAMPLE_WORK_FACTOR
print("快段阈值: %.2f m/s" % thr)
fast = [s for s in rows if s["speed_mps"] >= thr]
print("快采样数:", len(fast))
if fast:
    t0 = rows[0]["t_offset_s"]
    runs = []
    cur = [fast[0]]
    for a, b in zip(fast, fast[1:]):
        if b["t_offset_s"] - a["t_offset_s"] <= 15:
            cur.append(b)
        else:
            runs.append(cur)
            cur = [b]
    runs.append(cur)
    for r in runs:
        print(f"  快段: t={r[0]['t_offset_s'] - t0:.0f}~{r[-1]['t_offset_s'] - t0:.0f}s "
              f"持续={r[-1]['t_offset_s'] - r[0]['t_offset_s']:.0f}s "
              f"速度≈{sum(x['speed_mps'] for x in r) / len(r):.2f} m/s")
