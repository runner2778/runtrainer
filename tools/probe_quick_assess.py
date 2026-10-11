"""看一眼「一键评估」在真实库上到底算了什么：分量权重、证据、与长期口径对照。

用法：.venv\\Scripts\\python tools\\probe_quick_assess.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402

database.migrate()

from runtrainer.db.repos import kv_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.services import plan_service  # noqa: E402
from runtrainer.utils import jsonutil  # noqa: E402

stored = jsonutil.loads(kv_repo.get_app_state(plan_service.QUICK_KEY) or "{}")
print("落库切片: mode=%s window=%s acts_n=%s assessed_at=%s"
      % (stored.get("mode"), stored.get("window_days"), stored.get("acts_n"),
         stored.get("assessed_at")))
print("落库 VDOT:", stored.get("vdot"), " 等效:", stored.get("predictions"))
print("--- 证据 ---")
for ev in stored.get("evidence") or []:
    print("  %-18s vdot=%-6s %s" % (ev.get("source"), ev.get("vdot"), ev.get("detail")))
print("近一年 PB:", [(b.get("distance"), b.get("best_seconds"), b.get("source"))
                   for b in (stored.get("year_bests") or [])])

long_a = plan_service.ability_assessment()
print("--- 对照（长期口径 180 天）---")
print("VDOT:", long_a.get("vdot"))
for ev in long_a.get("evidence") or []:
    print("  %-18s vdot=%-6s %s" % (ev.get("source"), ev.get("vdot"), ev.get("detail")))

# 分量明细：直接跑一次 quick 口径，把每个在场分量的权重打出来
acts_year = [_ for _ in long_a.get("acts_year") or []]
quick_acts = long_a.get("acts_window") or []
print("--- 三周窗口活动数: %d / 全年 %d ---" % (len(quick_acts), len(acts_year)))
for a in quick_acts:
    segs = a.get("structure") or []
    kinds = {}
    for s in segs:
        kinds[s.get("type")] = kinds.get(s.get("type"), 0) + 1
    print("  %s %-8s %5.1fkm %s 段%s" % (
        a.get("date"), (a.get("sport") or "")[:8],
        (a.get("distance_m") or 0) / 1000.0, len(segs), kinds or ""))
print("QUICK_WEIGHTS:", ab.QUICK_WEIGHTS, "QUICK_WINDOW_DAYS:", ab.QUICK_WINDOW_DAYS)
