"""只读：仪表盘能力卡实际显示的数字（含 quick 缓存优先级）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.services import dashboard_service, plan_service  # noqa: E402

database.migrate()


def fmt(sec) -> str:
    h, r = divmod(int(round(sec)), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


d = dashboard_service.get_dashboard()
a = d.get("ability_30d") or {}
print("dashboard ability_30d:")
for k in ("vdot", "mode", "window_days", "assessed_at", "plan_vdot", "as_of"):
    print(f"  {k} = {a.get(k)}")
print("  predictions =", json.dumps({k: fmt(v) for k, v in (a.get("predictions") or {}).items()},
                                  ensure_ascii=False))
print("  prediction_bands =", json.dumps(a.get("prediction_bands"), ensure_ascii=False))
print("  personal_races =", a.get("personal_races"))
print("  year_bests =", json.dumps(a.get("year_bests"), ensure_ascii=False))

print("\n== quick 缓存是否存在 ==")
q = plan_service.quick_assessment_cached()
print("  cached:", "有" if q else "无")
if q:
    print("  vdot =", q.get("vdot"), " window =", q.get("window_days"),
          " assessed_at =", q.get("assessed_at"))
    print("  predictions =", json.dumps({k: fmt(v) for k, v in (q.get("predictions") or {}).items()},
                                        ensure_ascii=False))
