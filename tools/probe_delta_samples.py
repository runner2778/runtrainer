"""只读：复现 conversion_deltas 的学习样本（每场全开比赛的 实际−证据）。"""
from __future__ import annotations

import io
import sys
import json
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtrainer.domain import evidence as evm  # noqa: E402

SNAPS: list[list[dict]] = []
_orig = evm.conversion_deltas


def spy(history):
    SNAPS.append([dict(e) for e in history])
    return _orig(history)


evm.conversion_deltas = spy

src = (ROOT / "tools" / "backtest_predict_v2.py").read_text(encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(compile(src, str(ROOT / "tools" / "backtest_predict_v2.py"), "exec"),
         {"__name__": "__main__",
          "__file__": str(ROOT / "tools" / "backtest_predict_v2.py")})

hist = max(SNAPS, key=len) if SNAPS else []
print(f"== conversion_deltas 调用 {len(SNAPS)} 次，最长 history n={len(hist)} ==")
print("\n每场全开比赛的学习样本（实际 VDOT − 证据 eff）：")
hdr = f"  {'日期':<12}{'距离':>5}{'实际':>7}{'interval':>10}{'steady':>9}{'best_eff':>10}"
print(hdr)
for e in sorted(hist, key=lambda x: x.get("day") or ""):
    if e.get("quality") == "submax":
        continue
    f = e.get("ev_feats") or {}
    av = e.get("actual_vdot")
    def cell(k):
        return f"{av - f[k]:+.1f}" if (k in f and av is not None) else "—"
    print(f"  {e.get('day'):<12}{e.get('label') or '':>5}{av if av is not None else float('nan'):>7.1f}"
          f"{cell('interval'):>10}{cell('steady'):>9}{cell('best_effort'):>10}")

deltas = _orig(hist)
print("\n最终转换差:", json.dumps(deltas, ensure_ascii=False))
