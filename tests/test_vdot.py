"""M3：VDOT 公式与配速表——对照公开 VDOT 表锚点。

锚点来源：Jack Daniels VDOT 表（多数公开来源一致值）。
校准强度：M=82%、T=88%、I=98%、R=105%、E=59–74%。
"""
import pytest

from runtrainer.domain import vdot

# (vdot, kind, 表值 s/km, 容差 s/km)
PACE_ANCHORS = [
    (45, "M", 296, 3),   # 4:56/km
    (45, "T", 278, 3),   # 4:38/km
    (45, "I", 255, 3),   # 4:15/km
    (50, "M", 271, 3),   # 4:31/km
    (50, "T", 255, 3),   # 4:15/km
    (50, "I", 235, 3),   # 3:55/km
    (50, "R", 221, 3),   # 3:41/km
    (55, "M", 250, 3),   # 4:10/km
    (55, "T", 236, 3),   # 3:56/km
    (55, "I", 216, 3),   # 3:36/km
]

# (距离, 成绩 s, 期望 VDOT, 容差)
VDOT_EQUIVALENTS = [
    (10000.0, 41 * 60 + 20, 50, 0.7),          # 10K 41:20 → VDOT 50
    (42195.0, 3 * 3600 + 10 * 60 + 49, 50, 0.7),  # 全马 3:10:49 → VDOT 50
    (10000.0, 45 * 60 + 13, 45, 0.7),          # 10K 45:13 → VDOT 45
    (10000.0, 38 * 60 + 6, 55, 0.7),           # 10K 38:06 → VDOT 55
]

# (VDOT, 距离, 期望成绩 s, 容差 s)
TIME_PREDICTIONS = [
    (50, 42195.0, 3 * 3600 + 10 * 60 + 49, 60),
    (45, 10000.0, 45 * 60 + 13, 15),
    (50, 10000.0, 41 * 60 + 20, 15),
    (55, 21097.5, 84 * 60 + 16, 30),
]


@pytest.mark.parametrize("vd,kind,expected,tol", PACE_ANCHORS)
def test_pace_table_anchors(vd, kind, expected, tol):
    table = vdot.pace_table(vd)
    assert abs(table[kind] - expected) <= tol, f"VDOT {vd} {kind} 配速 {table[kind]} 偏离表值 {expected}"


@pytest.mark.parametrize("dist,time_s,expected,tol", VDOT_EQUIVALENTS)
def test_estimate_vdot_equivalents(dist, time_s, expected, tol):
    assert abs(vdot.estimate_vdot(dist, time_s) - expected) <= tol


@pytest.mark.parametrize("vd,dist,expected,tol", TIME_PREDICTIONS)
def test_predict_time(vd, dist, expected, tol):
    assert abs(vdot.predict_time(dist, vd) - expected) <= tol


def test_estimate_predict_roundtrip():
    """成绩 → VDOT → 同距离预测成绩应复原。"""
    for dist, time_s in [(5000, 21 * 60 + 49), (10000, 45 * 60 + 13), (42195, 3 * 3600 + 28 * 60 + 16)]:
        vd = vdot.estimate_vdot(dist, time_s)
        assert abs(vdot.predict_time(dist, vd) - time_s) <= 1.0


def test_pace_ordering():
    """配速快慢顺序：E_slow > E_fast > M > T > I > R（s/km 越大越慢）。"""
    for vd in (35, 40, 45, 50, 55, 60, 70):
        t = vdot.pace_table(vd)
        assert t["E"]["slow_s_km"] > t["E"]["fast_s_km"] > t["M"] > t["T"] > t["I"] > t["R"]


def test_vdot_monotonic():
    """同距离越快 VDOT 越高；同 VDOT 距离越长用时越长。"""
    assert vdot.estimate_vdot(10000, 40 * 60) > vdot.estimate_vdot(10000, 45 * 60)
    assert vdot.predict_time(42195, 55) < vdot.predict_time(42195, 50) < vdot.predict_time(42195, 45)


def test_v_for_vo2_inverse():
    for vo2 in (30.0, 45.0, 60.0, 70.0):
        v = vdot.v_for_vo2(vo2)
        assert abs(vdot._vo2cost(v) - vo2) < 1e-9


def test_equivalent_times_known_values():
    eq = vdot.equivalent_times(50)
    assert abs(eq["10K"] - 2480) <= 15
    assert abs(eq["全马"] - 11449) <= 60
    assert eq["5K"] < eq["10K"] < eq["半马"] < eq["全马"]


def test_invalid_inputs():
    with pytest.raises(ValueError):
        vdot.estimate_vdot(0, 100)
    with pytest.raises(ValueError):
        vdot.estimate_vdot(1000, 0)
    with pytest.raises(ValueError):
        vdot.pace_table(0)
    with pytest.raises(ValueError):
        vdot.predict_time(10000, -5)
