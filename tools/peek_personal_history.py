"""只读：打印个人误差学习历史（app_state.personal_history）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.services import plan_service as ps  # noqa: E402
from runtrainer.domain import personal  # noqa: E402

database.migrate()


def fmt(sec) -> str:
    h, r = divmod(int(round(sec)), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main() -> None:
    hist = ps._personal_history_load()
    print(f"历史 n={len(hist)}")
    for e in hist:
        print("  " + json.dumps(e, ensure_ascii=False))
    print("\n== 按距离偏差 ==")
    for label in ("5K", "10K", "半马", "全马"):
        print(f"  {label:>4} distance_stats={personal.distance_stats(hist, label)} "
              f"bias_applied={personal.bias_applied(hist, label)} "
              f"interval={personal.interval(hist, label)}")
    print("\n== 残差 ==", json.dumps(personal.residual_stats(hist), ensure_ascii=False))
    print("== 分量误差 ==", json.dumps(personal.component_mae(hist), ensure_ascii=False))


if __name__ == "__main__":
    main()
