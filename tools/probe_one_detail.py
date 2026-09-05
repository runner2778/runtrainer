"""单条活动详情解析验证：samples/字段/采样级结构。"""
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

acts = activity_repo.list_activities(limit=5)
eid = acts[0]["external_id"]

adapter = GarminConnectAdapter(*settings_service.get_garmin_credentials(),
                               is_cn=settings_service.is_garmin_cn())
adapter.login()
d = adapter.fetch_activity_detail(eid)
print("活动:", eid, d.name)
print("duration_s:", d.duration_s, "distance_m:", d.distance_m,
      "avg_pace:", d.avg_pace_s_km, "avg_hr:", d.avg_hr, "max_hr:", d.max_hr)
print("laps:", len(d.laps), "首圈:", d.laps[0] if d.laps else None)
print("samples:", len(d.samples), "首条:", d.samples[0] if d.samples else None,
      "末条:", d.samples[-1] if d.samples else None)
hrs = [s["hr"] for s in d.samples if s.get("hr")]
spds = [s["speed_mps"] for s in d.samples if s.get("speed_mps")]
print("有心率采样:", len(hrs), "有速度采样:", len(spds))
if hrs:
    print("采样均值心率:", round(sum(hrs) / len(hrs), 1))
if spds:
    print("采样均值配速:", round(1000 / (sum(spds) / len(spds)), 1))

segs = analyze_structure(d.laps, d.duration_s, d.distance_m, samples=d.samples)
print("结构:")
for s in segs:
    print("  ", s)
w = classify_workout(segs, d.duration_s, d.distance_m, d.avg_hr, d.max_hr)
print("分类:", w["kind"], "|", w["label"])
