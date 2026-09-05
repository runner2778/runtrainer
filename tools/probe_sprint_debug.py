"""冲刺课采样级识别逐步调试。"""
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
rows = sorted((s for s in d.samples if s.get("t_offset_s") is not None and s.get("speed_mps")),
              key=lambda s: s["t_offset_s"])
print("rows:", len(rows))
run_spds = [s["speed_mps"] for s in rows if s["speed_mps"] >= wa.SAMPLE_RUN_MIN_MPS]
baseline = sorted(run_spds)[len(run_spds) // 2]
print("baseline: %.3f 阈值: %.3f" % (baseline, baseline * wa.SAMPLE_WORK_FACTOR))
n = len(rows)
fast = [s["speed_mps"] >= baseline * wa.SAMPLE_WORK_FACTOR for s in rows]
runs = []
i = 0
while i < n:
    if fast[i]:
        j = i
        while (j + 1 < n and fast[j + 1]
               and rows[j + 1]["t_offset_s"] - rows[j]["t_offset_s"] <= wa.SAMPLE_MERGE_GAP_S):
            j += 1
        runs.append([i, j])
        i = j + 1
    else:
        i += 1
print("runs:", len(runs))
merged = []
for r in runs:
    if merged and rows[r[0]]["t_offset_s"] - rows[merged[-1][1]]["t_offset_s"] <= wa.SAMPLE_MERGE_GAP_S:
        merged[-1][1] = r[1]
    else:
        merged.append(list(r))
works = []
for lo, hi in merged:
    seg = wa._seg_from_rows(rows, lo, hi)
    dur = seg["duration_s"] or 0
    dist = seg["distance_m"] or 0
    ok = dur >= wa.SAMPLE_WORK_MIN_S and dist >= wa.SAMPLE_WORK_MIN_M
    if len(works) < 25 or ok:
        print(f"  窗口 t={rows[lo]['t_offset_s']:.0f}~{rows[hi]['t_offset_s']:.0f} "
              f"dur={dur:.0f} dist={dist:.0f} ok={ok}")
    if ok:
        works.append((lo, hi, seg))
print("works:", len(works))
if len(works) >= 2:
    for (a_lo, a_hi, _), (b_lo, b_hi, _) in zip(works, works[1:]):
        gap = rows[b_lo]["t_offset_s"] - rows[a_hi]["t_offset_s"]
        between = wa._seg_from_rows(rows, a_hi + 1, b_lo - 1)
        avg = between["distance_m"] / between["duration_s"] if between["duration_s"] else 0
        print(f"  间隔 gap={gap:.0f}s between_dur={between['duration_s']:.0f} "
              f"avg_spd={avg:.2f} 限={baseline * wa.SAMPLE_REST_FACTOR:.2f}")
