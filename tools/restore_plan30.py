"""demo_ui 误跑创建了 5K 测试计划（31/goal 22），恢复职业双练半马计划 30 为 active。"""
import os
import sqlite3

c = sqlite3.connect(os.path.join(os.environ["APPDATA"], "RunTrainer", "runtrainer.db"))
c.execute("UPDATE training_plans SET status='superseded' WHERE id=31")
c.execute("UPDATE training_plans SET status='active' WHERE id=30")
c.execute("UPDATE goals SET status='archived' WHERE id=22")
c.commit()
print("active plans:", c.execute("SELECT id, race_date, pro_mode FROM training_plans WHERE status='active'").fetchall())
print("plan30:", c.execute("SELECT id, status, pro_mode, race_date FROM training_plans WHERE id=30").fetchall())
c.close()
