"""plan 30（职业双练）不变量检查。"""
import os
import sqlite3
from datetime import date, timedelta

c = sqlite3.connect(os.path.join(os.environ["APPDATA"], "RunTrainer", "runtrainer.db"))
c.row_factory = sqlite3.Row
ws = [dict(r) for r in c.execute("SELECT * FROM planned_workouts WHERE plan_id=30 ORDER BY date, slot")]
plan = dict(c.execute("SELECT * FROM training_plans WHERE id=30").fetchone())
pro = bool(plan.get("pro_mode"))
HARD = {"T", "I", "R", "M", "TUNEUP", "RACE"}

by_date = {}
for w in ws:
    by_date.setdefault(w["date"], []).append(w)

errs = []
# 职业模式：非减量/比赛周——原休息日（周一/周五）轻松跑单练，其余全部两练
per_date = {}
for w in ws:
    per_date.setdefault((w["date"], w["week_index"], w["phase"]), []).append(w)

for (d, wi, ph), arr in sorted(per_date.items()):
    if ph == "taper" or d >= sorted(by_date)[-1]:
        if len(arr) > 1:
            errs.append(f"{d} 减量/比赛周出现两练")
        continue
    if len(arr) == 1:
        if arr[0]["kind"] != "E" or "原休息日" not in arr[0]["title"]:
            errs.append(f"{d} 单练但非原休息日轻松跑 {arr[0]['kind']}")
        if date.fromisoformat(d).weekday() not in (0, 4):
            errs.append(f"{d} 单练日子不是周一/周五")
    elif len(arr) == 2:
        if [w["slot"] for w in arr] != [1, 2]:
            errs.append(f"{d} slot 异常 {[w['slot'] for w in arr]}")
        ok = (arr[0]["kind"] == arr[1]["kind"] == "T") or arr[1]["kind"] == "RECOVERY"
        if not ok:
            errs.append(f"{d} 二练形式异常 {[w['kind'] for w in arr]}")
    else:
        errs.append(f"{d} 超过 2 练")

# 相邻日连续强度（看 slot-1 主课）
day_kind = {d: arr[0]["kind"] for d, arr in by_date.items()}
day_pz = {d: arr[0].get("pace_zone") for d, arr in by_date.items()}
def is_hard(d):
    k = day_kind.get(d)
    return k in HARD or (k == "LR" and day_pz.get(d) == "M")

dates = sorted(by_date)
for i, d in enumerate(dates[:-1]):
    nxt = date.fromisoformat(d) + timedelta(days=1)
    if nxt.isoformat() in day_kind and is_hard(d) and is_hard(nxt.isoformat()):
        errs.append(f"{d} {day_kind[d]} 与次日 {nxt.isoformat()} {day_kind[nxt.isoformat()]} 连续强度")

for w in ws:
    if w["kind"] == "STRENGTH":
        if w["phase"] in ("taper",) or w["date"] >= sorted(by_date)[-1]:
            errs.append(f"{w['date']} 减量/比赛周出现力量课")
        arr = by_date[w["date"]]
        if len(arr) > 2 or (len(arr) == 2 and arr[1]["kind"] != "RECOVERY"):
            errs.append(f"{w['date']} 力量课叠了不合法训练")

print("errors:", errs if errs else "无")
print("plan:", {k: plan[k] for k in ("id", "pro_mode", "double_days", "strength_days",
                                    "base_weekly_km", "peak_weekly_km", "vdot")})
tt = [d for d, a in by_date.items() if len(a) == 2 and a[0]["kind"] == "T"]
ez = [d for d, a in by_date.items() if len(a) == 2 and a[1]["kind"] == "RECOVERY"]
print("双阈值日:", tt)
print("带晚跑日:", ez)
single = [d for d, a in by_date.items() if len(a) == 1 and "原休息日" in a[0]["title"]]
print("原休息日轻松单练:", single)
