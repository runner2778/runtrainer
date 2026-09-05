"""训练负荷指标（纯函数）：周跑量 / ACWR / 单调性 / 应变 / 心率区间。"""
from __future__ import annotations

from datetime import date, timedelta


def _d(value) -> date:
    return date.fromisoformat(str(value)[:10])


def daily_totals(sessions: list[dict]) -> dict[date, dict]:
    """sessions: [{date, distance_km, duration_min}] → {date: {km, min, n}}。"""
    out: dict[date, dict] = {}
    for s in sessions:
        d = _d(s["date"])
        e = out.setdefault(d, {"km": 0.0, "min": 0.0, "n": 0})
        e["km"] += float(s.get("distance_km") or 0)
        e["min"] += float(s.get("duration_min") or 0)
        e["n"] += 1
    return out


def weekly_totals(sessions: list[dict]) -> list[dict]:
    """按周（周一为起点）汇总 → [{week_start, km, duration_min, n_sessions}]，时间升序。"""
    daily = daily_totals(sessions)
    weeks: dict[date, dict] = {}
    for d, e in daily.items():
        ws = d - timedelta(days=d.weekday())
        w = weeks.setdefault(ws, {"km": 0.0, "duration_min": 0.0, "n_sessions": 0})
        w["km"] += e["km"]
        w["duration_min"] += e["min"]
        w["n_sessions"] += e["n"]
    return [{"week_start": ws.isoformat(), **v} for ws, v in sorted(weeks.items())]


def acwr(sessions: list[dict], ref_date: date) -> dict | None:
    """急慢性负荷比：近 7 天跑量 / 近 28 天周均跑量。"""
    daily = daily_totals(sessions)
    acute = sum(e["km"] for d, e in daily.items() if ref_date - timedelta(days=6) <= d <= ref_date)
    chronic_km = sum(e["km"] for d, e in daily.items() if ref_date - timedelta(days=27) <= d <= ref_date)
    chronic = chronic_km / 4.0
    if chronic <= 0:
        return None
    return {"acute_km": round(acute, 1), "chronic_km": round(chronic, 1),
            "ratio": round(acute / chronic, 2)}


def monotony(sessions: list[dict], ref_date: date, days: int = 7) -> float | None:
    """单调性：窗口内日均负荷均值 / 标准差（std=0 时返回 None）。"""
    daily = daily_totals(sessions)
    loads = [e["km"] for d, e in daily.items() if ref_date - timedelta(days=days - 1) <= d <= ref_date]
    if len(loads) < 3:
        return None
    mean = sum(loads) / len(loads)
    var = sum((x - mean) ** 2 for x in loads) / (len(loads) - 1)
    if var <= 0:
        return None
    return round(mean / (var ** 0.5), 2)


def strain(sessions: list[dict], ref_date: date) -> float | None:
    """应变 = 近 7 天跑量 × 单调性。"""
    mono = monotony(sessions, ref_date)
    if mono is None:
        return None
    daily = daily_totals(sessions)
    week_km = sum(e["km"] for d, e in daily.items() if ref_date - timedelta(days=6) <= d <= ref_date)
    return round(week_km * mono, 1)


# ---- 心率区间（5 区模型，%HRmax）----
HR_ZONE_BOUNDS = (0.50, 0.60, 0.70, 0.80, 0.90)   # Z1 下限 50%


def hr_zone(max_hr: float, hr: float) -> int:
    """按 %HRmax 返回 1–5 区。"""
    if max_hr <= 0 or hr <= 0:
        return 0
    pct = hr / max_hr
    for i, b in enumerate(HR_ZONE_BOUNDS):
        if pct < b:
            return i            # 低于 50% → 0 区（热身）
    return 5


def time_in_zones(samples: list[dict], max_hr: float) -> dict:
    """心率采样 [{t_offset_s, hr}] → 各区累计秒数 {Z0..Z5}。"""
    out = {str(i): 0.0 for i in range(6)}
    prev_t = None
    prev_hr = None
    for s in sorted(samples, key=lambda x: x["t_offset_s"]):
        t, hr = s["t_offset_s"], s["hr"]
        if hr is None or hr <= 0:
            continue
        if prev_t is not None:
            dt = t - prev_t
            if dt > 0 and dt <= 60:
                z = hr_zone(max_hr, prev_hr)
                out[str(z)] += dt
        prev_t, prev_hr = t, hr
    return {k: round(v) for k, v in out.items()}


# ---- 计划完成度 ----
def compliance(planned: list[dict], done: list[dict], start: date, end: date) -> dict:
    """窗口内计划 vs 已完成跑量。planned/done: [{date, distance_km}]。"""
    def _sum(items):
        return sum(float(i.get("distance_km") or 0)
                   for i in items if start <= _d(i["date"]) <= end)
    p, d = _sum(planned), _sum(done)
    return {"planned_km": round(p, 1), "done_km": round(d, 1),
            "ratio": round(d / p, 3) if p > 0 else None}
