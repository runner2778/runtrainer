"""只读：为新增的三条测试量取「修前 / 修后」的数值，确定断言阈值。

修前口径 = 把 ability._race_channel_fresh 强制为 True（等价于原先
「同距离比赛不看新旧都当锚 + 矛盾加权只在别的距离有近期比赛时放行」）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402
from runtrainer.domain import vdot as vd  # noqa: E402
from test_evidence import _act, _interval_acts, _samples, MAX_HR  # noqa: E402


def show(tag, est, dists=(5000, 10000, 21097)):
    row = " ".join(
        f"{lbl}={vd.estimate_vdot(d, est['predictions'][lbl]):.1f}"
        for d, lbl in ((5000, "5K"), (10000, "10K"), (21097, "半马"))
        if est["predictions"].get(lbl))
    print(f"  {tag:<14} {row}", flush=True)


# ---------- 场景 A：过期同距离比赛（5K）不该当锚 ----------
def scenario_a():
    print("== 场景 A：150 天前的快 5K 比赛 + 10 天前的半马比赛 ==", flush=True)
    fresh_hm = _act(10, 300, 180, 190, 21097, 88 * 60, name="半马 比赛", aid=7)
    stale_5k = _act(150, 200, 190, 190, 5000, 16 * 60, name="5K 比赛", aid=8)
    base = _interval_acts(6, 12)
    print(f"  旧 5K 比赛 VDOT {vd.estimate_vdot(5000, 16 * 60):.1f}；"
          f"间歇课水平 {ab._vdot_for_pace(232, vd.T_PCT):.1f}", flush=True)
    est0 = ab.compute_ability(base + [fresh_hm], None, MAX_HR, personal_history=[])
    est1 = ab.compute_ability(base + [fresh_hm, stale_5k], None, MAX_HR,
                              personal_history=[])
    print("  修后（不含旧 5K）:", flush=True)
    show("no_stale", est0)
    print("  修后（含旧 5K）:", flush=True)
    show("with_stale", est1)
    _orig = ab._race_channel_fresh
    ab._race_channel_fresh = lambda *a, **k: True
    try:
        est2 = ab.compute_ability(base + [fresh_hm, stale_5k], None, MAX_HR,
                                  personal_history=[])
        est3 = ab.compute_ability(base + [fresh_hm], None, MAX_HR,
                                  personal_history=[])
    finally:
        ab._race_channel_fresh = _orig
    print("  修前（含旧 5K）:", flush=True)
    show("with_stale", est2)
    print("  修前（不含旧 5K）:", flush=True)
    show("no_stale", est3)


# ---------- 场景 B：近期同距离比赛与训练差 ≥3 VDOT ----------
def scenario_b():
    print("\n== 场景 B：10 天前 5K 比赛低于训练通道（扫目标差）==", flush=True)
    base = _interval_acts(6, 12)
    no_race = ab.compute_ability(base, None, MAX_HR, personal_history=[])
    train0 = vd.estimate_vdot(5000, no_race["predictions"]["5K"])
    print(f"  无比赛时 5K 预测 VDOT {train0:.2f}", flush=True)
    for target in (3.0, 4.0, 4.5, 5.0):
        dur = float(vd.predict_time(5000, train0 - target))
        race = _act(10, dur / 5.0, 188, 190, 5000, dur, name="5K 比赛", aid=9)
        rv = round(vd.estimate_vdot(5000, dur), 1)
        post = ab.compute_ability(base + [race], None, MAX_HR, personal_history=[])
        pv = vd.estimate_vdot(5000, post["predictions"]["5K"])
        _orig = ab.CONTRADICT_SPAN
        ab.CONTRADICT_SPAN = 4.0
        try:
            pre = ab.compute_ability(base + [race], None, MAX_HR,
                                     personal_history=[])
        finally:
            ab.CONTRADICT_SPAN = _orig
        qv = vd.estimate_vdot(5000, pre["predictions"]["5K"])
        print(f"  目标差 {target:.1f}（比赛 VDOT {rv:.1f}，dur {dur:.1f}s）: "
              f"修后 pred−race {pv - rv:+.3f} | 修前 {qv - rv:+.3f}", flush=True)


# ---------- 场景 C：停表空档不该进最快分段 ----------
def scenario_c():
    print("\n== 场景 C：停表空档对最快 15K 分段的影响 ==", flush=True)

    def run(with_pause):
        out, t = [], 0.0
        for _ in range(600):                       # 快跑 6000 m
            out.append((t, 5.0))
            t += 2.0
        if with_pause:
            t += 600.0
        for _ in range(5):                         # 恢复期慢走 10 m
            out.append((t, 1.0))
            t += 2.0
        for _ in range(1200):                      # 快跑 12000 m
            out.append((t, 5.0))
            t += 2.0
        return [{"t_offset_s": a, "speed_mps": b, "hr": 170} for a, b in out]

    dists = ((15000, "15K"),)
    for tag, samples in (("no_pause", run(False)), ("paused", run(True))):
        w = evm._best_effort_windows_for(samples, dists, MAX_HR)
        p = w["15K"][0]
        print(f"  修后 {tag:<9} 每米 {p:.6f} s/m → {p * 1000:.1f} s/km", flush=True)
    _orig = evm.SAMPLE_GAP_MIN_S
    evm.SAMPLE_GAP_MIN_S = 1e9
    try:
        for tag, samples in (("no_pause", run(False)), ("paused", run(True))):
            w = evm._best_effort_windows_for(samples, dists, MAX_HR)
            p = w["15K"][0]
            print(f"  修前 {tag:<9} 每米 {p:.6f} s/m → {p * 1000:.1f} s/km", flush=True)
    finally:
        evm.SAMPLE_GAP_MIN_S = _orig


if __name__ == "__main__":
    scenario_a()
    scenario_b()
    scenario_c()
