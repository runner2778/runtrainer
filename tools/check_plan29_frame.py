"""plan 29 周框架核对：start/race/weeks + 第 4-5 周逐日课表。"""
import os
import sqlite3
from datetime import date, timedelta

c = sqlite3.connect(os.path.join(os.environ["APPDATA"], "RunTrainer", "runtrainer.db"))
c.row_factory = sqlite3.Row
p = dict(c.execute("SELECT * FROM training_plans WHERE id=29").fetchone())
print("start", p["start_date"], "race", p["race_date"], "weeks", p["total_weeks"],
      "run_days", p["run_days"], "lr_wd", p["long_run_weekday"])
print("race weekday:", date.fromisoformat(p["race_date"]).weekday())

ws = [dict(r) for r in c.execute("SELECT * FROM planned_workouts WHERE plan_id=29 ORDER BY date, slot")]
DOW = "一二三四五六日"
for w in ws:
    d = date.fromisoformat(w["date"])
    if p["start_date"] <= w["date"] <= "2026-10-04" and d.weekday() in (2, 3, 4, 5):
        print(w["date"], DOW[d.weekday()], "slot", w["slot"], w["kind"], w["title"][:24])
