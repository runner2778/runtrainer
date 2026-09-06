"""VDOT（伪最大摄氧量）与训练配速表（纯函数，不查表）。

基于 Daniels-Gilbert 公式：
  VO2cost(v)  = -4.60 + 0.182258·v + 0.000104·v²      (v: m/min, ml/kg/min)
  fraction(t) = 0.8 + 0.1894393·e^(-0.012778t) + 0.2989558·e^(-0.1932605t)   (t: min)
即持续运动 t 分钟时可维持的 VO2max 比例；VDOT = VO2cost / fraction。

强度区间的 %VDOT 已对照公开 VDOT 配速表校准（VDOT 45/50/55 三档锚点误差 ≤2 s/km）：
  E: 59–74%    M: 82%    T: 88%    I: 98%    R: 105%（超 VO2max）
"""
from __future__ import annotations

import math

# 强度区间的 %VDOT（校准值，勿随意改动——test_vdot 锚点依赖）
E_LOW, E_HIGH = 0.59, 0.74
M_PCT = 0.82
T_PCT = 0.88
I_PCT = 0.98
R_PCT = 1.05

KINDS = ("E", "M", "T", "I", "R")

# 常用比赛距离（用于等效成绩换算）
DISTANCES = {"5K": 5000.0, "10K": 10000.0, "半马": 21097.5, "全马": 42195.0}

# 全区间配速表（%VDOT 连续覆盖，供「水平预估 → 各区对应配速」展示；
# 边界以 E/M/T/I/R 锚点为准：M=0.82 / T=0.88 / I=0.98 / R=1.05 均在区间内）
PACE_ZONES = (
    {"key": "recovery", "label": "恢复跑", "mark": "", "pct_lo": 0.50, "pct_hi": 0.59,
     "use": "放松排酸/热身"},
    {"key": "easy", "label": "轻松跑", "mark": "E", "pct_lo": 0.59, "pct_hi": 0.74,
     "use": "日常轻松跑（E 区）"},
    {"key": "aerobic", "label": "有氧跑", "mark": "M", "pct_lo": 0.74, "pct_hi": 0.82,
     "use": "长距离/马拉松配速（M 0.82）"},
    {"key": "threshold", "label": "乳酸阈值", "mark": "T", "pct_lo": 0.82, "pct_hi": 0.92,
     "use": "节奏跑/巡航间歇（T 0.88）"},
    {"key": "vo2max", "label": "最大摄氧量", "mark": "I", "pct_lo": 0.92, "pct_hi": 1.00,
     "use": "V·O2max 间歇（I 0.98）"},
    {"key": "anaerobic", "label": "无氧冲刺", "mark": "R", "pct_lo": 1.00, "pct_hi": 1.05,
     "use": "短冲重复跑（R 1.05）"},
)


def intensity_zones(vdot: float) -> list[dict] | None:
    """给定 VDOT 的各强度区间配速表（配速随 %VDOT 升高而变快）。

    返回 PACE_ZONES 每行加 pace_slow_s_km（低 % 端，慢）/pace_fast_s_km
    （高 % 端，快），供水平预估卡/向导预览/日历区间标注共用。
    """
    if not vdot or vdot <= 0:
        return None
    rows = []
    for z in PACE_ZONES:
        slow = pace_s_km(vdot, z["pct_lo"])
        fast = pace_s_km(vdot, z["pct_hi"])
        rows.append({**z,
                     "band": f"{int(z['pct_lo'] * 100)}–{int(z['pct_hi'] * 100)}% VDOT",
                     "pace_slow_s_km": round(slow),
                     "pace_fast_s_km": round(fast)})
    return rows


def _vo2cost(v_m_min: float) -> float:
    return -4.60 + 0.182258 * v_m_min + 0.000104 * v_m_min * v_m_min


def _fraction(t_min: float) -> float:
    return (0.8
            + 0.1894393 * math.exp(-0.012778 * t_min)
            + 0.2989558 * math.exp(-0.1932605 * t_min))


def estimate_vdot(distance_m: float, time_s: float) -> float:
    """由比赛成绩估算 VDOT。distance_m: 米；time_s: 秒。"""
    if distance_m <= 0 or time_s <= 0:
        raise ValueError("距离与时间必须为正")
    t_min = time_s / 60.0
    v = distance_m / t_min
    return _vo2cost(v) / _fraction(t_min)


def v_for_vo2(vo2: float) -> float:
    """反解 VO2cost 二次方程，返回速度 m/min。"""
    a, b, c = 0.000104, 0.182258, -4.60 - vo2
    disc = b * b - 4 * a * c
    if disc < 0:
        raise ValueError(f"vo2 过小无法反解: {vo2}")
    return (-b + math.sqrt(disc)) / (2 * a)


def predict_time(distance_m: float, vdot: float) -> float:
    """二分法求等价成绩（秒）。estimate_vdot 随用时单调递减。"""
    if distance_m <= 0:
        raise ValueError("距离必须为正")
    if vdot <= 0:
        raise ValueError("vdot 必须为正")
    lo, hi = 1.0, 2 * 3600.0
    # 扩大上界直至 estimate(hi) < vdot（即 hi 足够慢）
    while estimate_vdot(distance_m, hi) > vdot and hi < 1e7:
        hi *= 2
    for _ in range(80):
        mid = (lo + hi) / 2
        if estimate_vdot(distance_m, mid) > vdot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def pace_s_km(vdot: float, pct: float) -> float:
    """给定 %VDOT 对应的配速（秒/公里）。"""
    v = v_for_vo2(vdot * pct)
    return 1000.0 * 60.0 / v


def pace_table(vdot: float) -> dict:
    """E/M/T/I/R 配速表（s/km）。

    E 为区间 [slow, fast]，其余为单值；均四舍五入到整数秒。
    """
    if vdot <= 0:
        raise ValueError("vdot 必须为正")
    return {
        "vdot": round(vdot, 1),
        "E": {"slow_s_km": round(pace_s_km(vdot, E_LOW)),
              "fast_s_km": round(pace_s_km(vdot, E_HIGH))},
        "M": round(pace_s_km(vdot, M_PCT)),
        "T": round(pace_s_km(vdot, T_PCT)),
        "I": round(pace_s_km(vdot, I_PCT)),
        "R": round(pace_s_km(vdot, R_PCT)),
    }


def pace_zone_bounds(vdot: float) -> tuple[float, float]:
    """全表配速范围（s/km），供 AI 护栏校验配速落表用。"""
    return pace_s_km(vdot, E_LOW), pace_s_km(vdot, R_PCT)


def equivalent_times(vdot: float) -> dict:
    """常用距离等效成绩（秒），用于向导预览。"""
    return {label: round(predict_time(dist, vdot))
            for label, dist in DISTANCES.items()}
