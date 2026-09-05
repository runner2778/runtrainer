"""真实间歇课与匀速课的采样级结构识别对比。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo
from runtrainer.services import settings_service
from runtrainer.garmin.garminconnect_adapter import GarminConnectAdapter
from runtrainer.domain.workout_analysis import analyze_structure, classify_workout

config.ensure_dirs()
database.migrate()

adapter = GarminConnectAdapter(*settings_service.get_garmin_credentials(),
                               is_cn=settings_service.is_garmin_cn())
adapter.login()

targets = []
for a in activity_repo.list_activities(limit=5000):
    if a["name"] and "冲刺" in a["name"]:
        targets.append((a["external_id"], a["name"]))
        if len(targets) >= 2:
            break
targets.append((activity_repo.list_activities(limit=1)[0]["external_id"], "普通跑"))
for eid, name in targets:
    d = adapter.fetch_activity_detail(eid)
    print(f"\n=== {name} ({eid}) dur={d.duration_s}s dist={d.distance_m}m "
          f"hr={d.avg_hr} samples={len(d.samples)} ===")
    segs = analyze_structure(d.laps, d.duration_s, d.distance_m, samples=d.samples)
    for s in segs:
        print("  ", s["type"], "dist=", s.get("distance_m"), "dur=", s.get("duration_s"),
              "pace=", s.get("pace_s_km"), "hr=", s.get("avg_hr"))
    w = classify_workout(segs, d.duration_s, d.distance_m, d.avg_hr, d.max_hr)
    print("  →", w["kind"], "|", w["label"])
