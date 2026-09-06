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


# ---- 计划-实际按日配对（执行率/完成进度；第十七批）----
# 背景：真实用户每天自由跑 12km+、计划 9/7 才开课，旧口径把窗口内所有跑步都
# 算「完成」→ 计划 5.6km/实际 130km → 2325%。执行率应回答「课表被执行的
# 程度」：只与计划日/计划跑量对齐，计划外自由跑量与减载周单独展示。
RUN_SPORTS = ("running", "run", "跑步", "跑步机")


def is_running(sport) -> bool:
    """活动是否跑步（配对/进度只认跑步；跨训练不算执行课表）。"""
    s = (sport or "").strip().lower()
    return s in RUN_SPORTS or s.startswith("run")


def run_days_from_sessions(sessions: list[dict]) -> dict[str, float]:
    """[{date, distance_km}]（调用方已只传跑步）→ {date: 当日跑步总 km}。"""
    out: dict[str, float] = {}
    for s in sessions:
        day = str(s["date"])[:10]
        out[day] = out.get(day, 0.0) + float(s.get("distance_km") or 0)
    return out


def plan_coverage(planned_rows: list[dict], runs_day: dict[str, float]) -> dict:
    """计划执行覆盖（按计划日配对、逐日封顶，纯函数）。

    planned_rows: 计划课行（需 date/distance_km，调用方已按课表生命周期
    [start_date, race_date] 裁好）；runs_day: {date: 当日跑步 km}。
    非计划日的自由跑不计入执行（那是额外跑量，另有展示位）；某计划日实际
    跑量超出当日计划时只按计划量封顶 —— 避免「每天自由跑 12km」把一周
    5 节课的课表撑成几百 %。
    返回 {planned_km, done_km(计划日实际，不封顶), covered_km(逐日封顶),
    ratio=covered/planned∈[0,1]（无计划跑量 → None）, planned_days,
    covered_days}。
    """
    planned_km = covered = 0.0
    covered_days = 0
    for w in planned_rows:
        p = float(w.get("distance_km") or 0)
        if p <= 0:
            continue
        day = str(w["date"])[:10]
        actual = float(runs_day.get(day) or 0)
        planned_km += p
        if actual > 0:
            covered += min(p, actual)
            covered_days += 1
    done = sum(float(runs_day.get(str(w["date"])[:10]) or 0)
               for w in planned_rows)
    return {
        "planned_km": round(planned_km, 1), "done_km": round(done, 1),
        "covered_km": round(covered, 1),
        "ratio": round(covered / planned_km, 3) if planned_km > 0 else None,
        "planned_days": sum(1 for w in planned_rows
                            if float(w.get("distance_km") or 0) > 0),
        "covered_days": covered_days,
    }


def workout_auto_done(planned_rows: list[dict], runs_day: dict[str, float]) -> set[int]:
    """计划课按当日实际跑步的自动完成判定（读取侧，不写库）。

    用户常不手动勾「完成」，真实库所有课停在 planned → 进度/完成度永远 0。
    这里按日期配对：同一天多节课/多次跑按计划跑量从大到小、跑步从大到小
    贪心摊分（让每节有距离的课尽量摊到当日跑量）；某课摊到 ≥ 其计划跑量
    一半（至少 0.8km，无距离课按 1km 计）视为已执行。已显式跳过
    (status='skipped') 与力量课（STRENGTH，无跑量语义）不参与。返回
    planned_workouts id 集合。
    """
    from collections import defaultdict
    by_day: dict[str, list] = defaultdict(list)
    for w in planned_rows:
        if w.get("status") == "skipped" or w.get("kind") == "STRENGTH":
            continue
        by_day[str(w["date"])[:10]].append(w)
    done: set[int] = set()
    for day, ws in by_day.items():
        pool = [float(runs_day.get(day) or 0)]
        if pool[0] <= 0:
            continue
        for w in sorted(ws, key=lambda x: (float(x.get("distance_km") or 0) or 1.0),
                        reverse=True):
            p = float(w.get("distance_km") or 0) or 1.0
            pool.sort(reverse=True)
            if pool[0] <= 0:
                break
            take = pool[0]
            matched = min(take, p)
            if matched >= max(p * 0.5, 0.8):
                done.add(w["id"])
            pool[0] = take - matched
    return done
