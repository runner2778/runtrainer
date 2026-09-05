"""真实数据上的能力预估证据链探针。用法：.venv\\Scripts\\python tools\\probe_ability_real.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.services import plan_service

config.ensure_dirs()
database.migrate()
ctx = plan_service.wizard_context()
ab = ctx.get("ability") or {}
print("vdot:", ab.get("vdot"), "max_hr:", ab.get("max_hr"), "as_of:", ab.get("as_of"))
for ev in ab.get("evidence") or []:
    print(" -", ev["source"], "vdot=", ev.get("vdot"), "|", ev.get("detail"))
pred = ab.get("predictions") or {}
for k, v in pred.items():
    print(f"   {k}: {int(v) // 60}:{int(v) % 60:02d}")
