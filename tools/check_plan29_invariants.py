"""plan 29 不变量检查：二练 slot [1,2] 且形式合法（T-T 双阈值 或 主课+放松晚跑）；
无同日超 2 练；力量课不进减量/比赛周（职业模式可叠傍晚放松跑）；相邻两日不连续强度。"""
import os
import sqlite3
from datetime import date, timedelta

c = sqlite3.connect(os.path.join(os.environ["APPDATA"], "RunTrainer", "runtrainer.db"))
c.row_factory = sqlite3.Row
ws = [dict(r) for r in c.execute("SELECT * FROM planned_workouts WHERE plan_id=29 ORDER BY date, slot")]
plan = dict(c.execute("SELECT * FROM training_plans WHERE id=29").fetchone())
pro = bool(plan.get("pro_mode"))
HARD = {"T", "I", "R", "M", "TUNEUP", "RACE"}

by_date = {}
for w in ws:
    by_date.setdefault(w["date"], []).append(w)

errs = []
for d, arr in by_date.items():
    if len(arr) == 2:
        if [w["slot"] for w in arr] != [1, 2]:
            errs.append(f"{d} 二练 slot 异常 {[w['slot'] for w in arr]}")
        # 合法二练：双阈值 T-T，或主课 + 放松晚跑（职业模式主课可为任何类型）
        ok = (arr[0]["kind"] == arr[1]["kind"] == "T") or \
             (arr[1]["kind"] == "RECOVERY" and (pro or arr[0]["kind"] in HARD))
        if not ok:
            errs.append(f"{d} 二练形式异常 {[w['kind'] for w in arr]}")
    if len(arr) > 2:
        errs.append(f"{d} 同日超过 2 练")

# 相邻日连续强度（同一日多练看 slot-1 主课；LR 仅在 M 配速时算强度）
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
        if len(by_date[w["date"]]) > 2 or \
                (len(by_date[w["date"]]) == 2 and by_date[w["date"]][1]["kind"] != "RECOVERY"):
            errs.append(f"{w['date']} 力量课叠了不合法训练")
        if w["phase"] in ("taper",) or w["date"] >= sorted(by_date)[-1]:
            errs.append(f"{w['date']} 减量/比赛周出现力量课")

print("errors:", errs if errs else "无")
print("pro_mode:", pro)
tt = [d for d, a in by_date.items() if len(a) == 2 and a[0]["kind"] == "T"]
ez = [d for d, a in by_date.items() if len(a) == 2 and a[1]["kind"] == "RECOVERY"]
print("双阈值日:", tt)
print("带晚跑日:", ez)
