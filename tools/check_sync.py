"""诊断：查看 settings / sync_state / 凭据状态。"""
import os
import sqlite3

import keyring

db = os.path.join(os.environ["APPDATA"], "RunTrainer", "runtrainer.db")
conn = sqlite3.connect(db)
print("== settings ==")
for k, v in conn.execute("SELECT key, value FROM settings"):
    print(f"  {k} = {v}")
print("== sync_state ==")
rows = list(conn.execute("SELECT * FROM sync_state"))
if rows:
    for row in rows:
        print("  ", row)
else:
    print("  (空)")
print("== keyring ==")
u = keyring.get_password("runtrainer", "garmin_username")
p = keyring.get_password("runtrainer", "garmin_password")
print(f"  username={u!r} password={'已设置' if p else '未设置'}")
