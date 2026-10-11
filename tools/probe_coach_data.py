"""教练升级前真实库数据勘察（只读）：什么数据支撑什么能力。

回答：运动员个人响应模型（心率反应/疲劳耐受/后程能力/训练响应/睡眠敏感度）
在真实库上有没有足够的样本；健康数据覆盖如何；课表质量课完成形态。
"""
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo, health_repo, kv_repo, plan_repo, profile_repo  # noqa: E402
from runtrainer.domain import workout_analysis as wa  # noqa: E402
from runtrainer.utils import dates, jsonutil  # noqa: E402

database.migrate()
today = date.today()

print("=" * 60)
print("1) 活动总量与时间跨度")
acts = activity_repo.list_activities((today - timedelta(days=730)).isoformat(), limit=5000)
print("近 2 年活动:", len(acts))
if acts:
    days = sorted(dates.ts_to_date(a["start_ts"]) for a in acts if a.get("start_ts"))
    print("跨度:", days[0], "→", days[-1], "(", (days[-1] - days[0]).days, "天 )")
    sports = Counter(a.get("sport") for a in acts)
    print("运动类型:", dict(sports))

print("=" * 60)
print("2) 心率/配速/结构/采样覆盖")
with_hr = [a for a in acts if a.get("avg_hr")]
with_pace = [a for a in acts if a.get("avg_pace_s_km")]
with_struct = [a for a in acts if a.get("structure_json")]
with_samples = [a for a in acts if a.get("has_samples")]
print(f"有 avg_hr: {len(with_hr)}/{len(acts)}")
print(f"有 avg_pace: {len(with_pace)}/{len(acts)}")
print(f"有 structure_json: {len(with_struct)}/{len(acts)}")
print(f"有采样曲线: {len(with_samples)}/{len(acts)}")
run = [a for a in acts if a.get("distance_m")]
run_hr = [a for a in run if a.get("avg_hr")]
print(f"跑步(有距离): {len(run)}，其中带心率 {len(run_hr)}")

print("=" * 60)
print("3) 结构分段形态（后程能力/间歇执行质量的原料）")
segs_by_act = Counter()
work_seg_n = []
for a in with_struct:
    try:
        segs = jsonutil.loads(a["structure_json"]) or []
    except Exception:
        segs = []
    kinds = tuple(sorted({s.get("type") for s in segs}))
    segs_by_act[kinds] += 1
    works = [s for s in segs if s.get("type") == "work"]
    if works:
        work_seg_n.append(len(works))
for k, v in segs_by_act.most_common(10):
    print(f"  段类型组合 {k}: {v} 次")
if work_seg_n:
    print("  含 work 段的活动数:", len(work_seg_n), "组数分布:", Counter(work_seg_n).most_common(8))

print("=" * 60)
print("4) 健康数据覆盖（睡眠/HRV/静息心率）")
hrows = health_repo.get_health((today - timedelta(days=180)).isoformat(), today.isoformat())
print("近 180 天健康记录:", len(hrows), "天")
sleep_n = sum(1 for h in hrows if h.get("sleep_duration_s"))
hrv_n = sum(1 for h in hrows if h.get("hrv_avg_ms"))
rhr_n = sum(1 for h in hrows if h.get("resting_hr"))
score_n = sum(1 for h in hrows if h.get("sleep_score"))
print(f"  有睡眠时长: {sleep_n}，有 HRV: {hrv_n}，有静息心率: {rhr_n}，有睡眠评分: {score_n}")
if hrv_n:
    vals = [h["hrv_avg_ms"] for h in hrows if h.get("hrv_avg_ms")]
    print(f"  HRV 范围 {min(vals)}–{max(vals)}ms，均值 {sum(vals)/len(vals):.0f}")

print("=" * 60)
print("5) 个人比赛误差历史（第十九批 personal_history）")
raw = kv_repo.get_app_state("personal_history")
hist = jsonutil.loads(raw) if raw else []
print("条数:", len(hist))
for e in hist:
    print("  ", e.get("day"), e.get("label"), "actual", e.get("actual_vdot"),
          "pred", e.get("pred_vdot"))

print("=" * 60)
print("6) 近 16 周长距离/匀速跑的分段配速（后程能力样本）")
long_run = [a for a in with_struct
            if (a.get("distance_m") or 0) >= 12000 and a.get("avg_hr")]
print("≥12km 且有结构的活动:", len(long_run))
for a in sorted(long_run, key=lambda x: x["start_ts"])[-6:]:
    segs = jsonutil.loads(a["structure_json"]) or []
    day = dates.ts_to_date(a["start_ts"])
    if len(segs) <= 1:
        print(f"  {day} {a.get('distance_m')/1000:.1f}km 单段 配速{wa._overall(a.get('duration_s'), a.get('distance_m'), segs).get('pace_s_km')}")
        continue
    paces = [f"{s.get('pace_s_km')}" for s in segs if s.get("type") != "rest"]
    print(f"  {day} {a.get('distance_m')/1000:.1f}km {len(segs)}段 配速: " + " → ".join(paces))

print("=" * 60)
print("7) 同配速心率散点的个人一致性（心率反应模型可行性）")
# 用 4:50–5:40 常用配速窗 + 近 180 天，看同配速心率标准差
from runtrainer.domain.workout_analysis import _valid_pace_hr
rows = [a for a in acts if _valid_pace_hr(a)]
recent = [a for a in rows if dates.ts_to_date(a["start_ts"]) >= today - timedelta(days=90)]
print("近 90 天有效配速-心率行:", len(recent))
bins = defaultdict(list)
for a in recent:
    p = a["avg_pace_s_km"]
    if 280 <= p <= 340:
        bins[int(p // 10) * 10].append(a["avg_hr"])
for b in sorted(bins):
    hrs = bins[b]
    if len(hrs) < 3:
        continue
    mean = sum(hrs) / len(hrs)
    std = (sum((h - mean) ** 2 for h in hrs) / len(hrs)) ** 0.5
    m, s = divmod(b, 60)
    print(f"  配速 {m}:{s:02d}/km n={len(hrs)} 心率均值 {mean:.0f} σ={std:.1f}")

print("=" * 60)
print("8) 课表质量课执行形态（近 8 周）")
plan = plan_repo.get_active_plan()
if plan:
    rows = plan_repo.get_workouts(plan["id"], (today - timedelta(days=56)).isoformat(), today.isoformat())
    rows = [w for w in rows if plan["start_date"] <= w["date"] <= plan["race_date"]]
    quality = [w for w in rows if w.get("kind") in ("T", "T1", "I", "R", "TUNEUP", "M")]
    print("近 8 周计划课:", len(rows), "其中质量课:", len(quality))
    for w in quality[-8:]:
        print(f"  {w['date']} {w['kind']} {w['title']} {w.get('distance_km')}km 状态{w['status']}")

print("=" * 60)
print("9) 档案")
prof = profile_repo.get_profile() or {}
print(prof)
