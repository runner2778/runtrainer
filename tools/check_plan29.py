"""检查重建后的 plan 29：slot/力量课/二练/完成状态搬运。"""
import sqlite3
import os

c = sqlite3.connect(os.path.join(os.environ["APPDATA"], "RunTrainer", "runtrainer.db"))
c.row_factory = sqlite3.Row
print("old completed:", c.execute(
    "SELECT COUNT(*) FROM planned_workouts WHERE plan_id=28 AND status!='planned'").fetchone()[0])
print("new by slot:", [tuple(r) for r in c.execute(
    "SELECT slot,COUNT(*) FROM planned_workouts WHERE plan_id=29 GROUP BY slot")])
print("new by kind:", [tuple(r) for r in c.execute(
    "SELECT kind,COUNT(*) FROM planned_workouts WHERE plan_id=29 GROUP BY kind ORDER BY kind")])
print("strength sample:", [tuple(r) for r in c.execute(
    "SELECT date,slot FROM planned_workouts WHERE plan_id=29 AND kind='STRENGTH' ORDER BY date LIMIT 6")])
print("double dates:", [tuple(r) for r in c.execute(
    "SELECT date,COUNT(*) FROM planned_workouts WHERE plan_id=29 GROUP BY date HAVING COUNT(*)>1 LIMIT 8")])
print("new plan row:", dict(c.execute(
    "SELECT id,status,vdot,double_days,double_mode,strength_days FROM training_plans WHERE id=29").fetchone()))
