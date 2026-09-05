"""能力预估证据链调试：缺分量的原因 + 间歇课结构实况。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo, health_repo, profile_repo
from runtrainer.utils import jsonutil

config.ensure_dirs()
database.migrate()

acts = activity_repo.list_activities(limit=5000)
print(f"活动总数: {len(acts)}")
print(f"  有 avg_hr: {sum(1 for a in acts if a.get('avg_hr'))}")
print(f"  有 avg_pace: {sum(1 for a in acts if a.get('avg_pace_s_km'))}")
print(f"  has_samples=1: {sum(1 for a in acts if a.get('has_samples'))}")
print(f"  有 structure: {sum(1 for a in acts if a.get('structure_json'))}")

prof = profile_repo.get_profile() or {}
print(f"档案: max_hr={prof.get('max_hr')} rest_hr={prof.get('rest_hr')} vo2max={prof.get('vo2max')} birth={prof.get('birth_year')}")

health = health_repo.get_health("2026-01-01")
rhrs = [r.get("resting_hr") for r in health if r.get("resting_hr")]
print(f"健康行: {len(health)}, 有静息心率: {len(rhrs)}, 中位数: {sorted(rhrs)[len(rhrs)//2] if rhrs else None}")

# 含 work 段的课实况
print("\n== 含 work 段的课 ==")
for a in acts:
    raw = a.get("structure_json")
    if not raw:
        continue
    segs = jsonutil.loads(raw) or []
    works = [s for s in segs if s.get("type") == "work"]
    rests = [s for s in segs if s.get("type") == "rest"]
    if not works:
        continue
    print(f"- {a['start_ts']} {a['name']} dist={a['distance_m']}m dur={a['duration_s']}s "
          f"avg_pace={a.get('avg_pace_s_km')} avg_hr={a.get('avg_hr')}")
    for s in segs:
        print(f"    {s.get('type'):6s} dist={s.get('distance_m')}m pace={s.get('pace_s_km')} "
              f"elapsed={s.get('elapsed_s')} hr={s.get('avg_hr')}")
