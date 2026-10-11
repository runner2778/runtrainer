"""只读：确认新增的三条测试真能挡住「修前」行为（把生产代码改回旧口径，
三条测试必须各自失败）。不做任何修改，跑完即退。"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402
import test_evidence as te  # noqa: E402


def check(name, fn, patch, unpatch):
    patch()
    try:
        try:
            fn()
        except AssertionError:
            print(f"  [OK] {name}: 修前会失败（测试有效）", flush=True)
            return
        except Exception:
            traceback.print_exc()
            print(f"  [??] {name}: 修前抛了非断言异常", flush=True)
            return
        print(f"  [!!] {name}: 修前也通过——测试挡不住回归", flush=True)
    finally:
        unpatch()


orig_gap = evm.SAMPLE_GAP_MIN_S
orig_fresh = ab._race_channel_fresh
orig_span = ab.CONTRADICT_SPAN

print("== 修前口径下三条测试的判别力 ==", flush=True)
check("空档不计入最快分段",
      te.test_best_effort_window_excludes_pause_gap,
      lambda: setattr(evm, "SAMPLE_GAP_MIN_S", 1e9),
      lambda: setattr(evm, "SAMPLE_GAP_MIN_S", orig_gap))
check("过期同距离比赛不当锚",
      te.test_pipeline_stale_same_distance_race_does_not_anchor,
      lambda: setattr(ab, "_race_channel_fresh", lambda *a, **k: True),
      lambda: setattr(ab, "_race_channel_fresh", orig_fresh))
check("比赛通道压倒训练通道",
      te.test_pipeline_fresh_race_dominates_when_disagreeing,
      lambda: setattr(ab, "CONTRADICT_SPAN", 4.0),
      lambda: setattr(ab, "CONTRADICT_SPAN", orig_span))
