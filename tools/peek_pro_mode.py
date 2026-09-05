"""临时探查：pro_mode 计划前几周形态。"""
from datetime import date
from runtrainer.domain.plan_engine import PlanSpec, generate_plan

s = PlanSpec(goal_distance_m=21097, race_date=date(2026, 12, 20), vdot=45,
             base_weekly_km=40, start_date=date(2026, 9, 28), run_days=5,
             long_run_weekday=6, target_seconds=6300, pro_mode=True)
res = generate_plan(s)
by_date = {}
for w in res.workouts:
    by_date.setdefault(w.date, []).append(w)
for wi in range(min(4, res.total_weeks)):
    days = sorted(d for d in by_date if any(w.week_index == wi for w in by_date[d]))
    print(f"== week {wi} reported={res.weekly_km[wi]}")
    for d in days:
        ws = by_date[d]
        print(" ", d, [(w.kind, w.slot, round(w.duration_min or 0), round(w.distance_km or 0, 1), w.title[:18]) for w in ws])
print("total weeks", res.total_weeks, "taper wks", res.phase_weeks["taper"])
# 最后两段：减量周与比赛周
for wi in (res.total_weeks - 2, res.total_weeks - 1):
    days = sorted(d for d in by_date if any(w.week_index == wi for w in by_date[d]))
    print(f"== week {wi} reported={res.weekly_km[wi]}")
    for d in days:
        ws = by_date[d]
        print(" ", d, [(w.kind, w.slot, round(w.duration_min or 0), round(w.distance_km or 0, 1), w.title[:18]) for w in ws])
