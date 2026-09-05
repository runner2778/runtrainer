"""训练时期智能判断：识别近期强度与跑量分布，估计当前所处训练时期。

纯函数层，不碰 DB/网络，独立单测。

思路：不再机械地从基础期铺满全程，而是问「你近期在练什么」——
有间歇/重复课 = 速度期特征，节奏持续跑 = 专项期特征，跑量骤降 + 比赛
临近 = 减量期特征；只有轻松跑 = 基础期。输出时期 + 置信度 + 理由。
"""
from __future__ import annotations

from datetime import date, timedelta

PHASE_ORDER = ("base", "early", "transition", "final", "taper")
PHASE_NAMES = {"base": "基础期", "early": "早期强度", "transition": "过渡期",
               "final": "最终强度", "taper": "减量期"}

# 无分段结构时按配速相对中位数判断强度课（相对而非绝对：适应不同水平）
PACE_QUALITY_FACTOR = 0.88   # 快于窗口配速中位数 12% → 强度课（节奏/比赛）
WINDOW_WEEKS = 4             # 近 4 周 vs 前 4 周对比
MIN_RUNS_HIGH = 8            # 置信度阈值（窗口内活动数）
MIN_RUNS_MEDIUM = 4


def _day(a: dict) -> date:
    d = a.get("date")
    return date.fromisoformat(str(d)[:10]) if d else None


def _quality_kind(a: dict, max_hr: float | None, rest_hr: float | None,
                  med_pace: float | None) -> bool:
    """单次活动是否为强度课：结构识别优先，配速相对中位数兜底。"""
    segs = a.get("structure")
    if segs:
        from .workout_analysis import classify_workout
        try:
            kind = classify_workout(segs, a.get("duration_s"), a.get("distance_m"),
                                    a.get("avg_hr"), max_hr, rest_hr)["kind"]
        except Exception:  # noqa: BLE001
            kind = "unknown"
        if kind in ("interval", "repeats", "tempo", "anaerobic"):
            return True
        if kind in ("easy", "recovery"):
            return False
    p = a.get("avg_pace_s_km")
    if p and med_pace:
        return p <= med_pace * PACE_QUALITY_FACTOR
    return False


def suggest_phase(acts: list[dict], today: date, race_date: date | None = None,
                  max_hr: float | None = None, rest_hr: float | None = None,
                  weeks: int = 8) -> dict:
    """根据近 weeks 周的活动估计当前训练时期。

    acts: 活动行 [{date(ISO), distance_m, avg_pace_s_km, avg_hr,
          structure: [分段]}]，任意顺序、可含未来/无关日期。
    返回 {phase, phase_name, confidence(high|medium|low), reasons, stats}。
    """
    days = weeks * 7
    cutoff = today - timedelta(days=days - 1)
    recent = [a for a in acts
              if (d := _day(a)) is not None and cutoff <= d <= today
              and (a.get("distance_m") or 0) >= 1000]
    recent.sort(key=_day)
    w4 = [a for a in recent if _day(a) >= today - timedelta(days=WINDOW_WEEKS * 7 - 1)]
    prev4 = [a for a in recent if _day(a) < today - timedelta(days=WINDOW_WEEKS * 7 - 1)]

    km4 = sum((a.get("distance_m") or 0) for a in w4) / 1000 / WINDOW_WEEKS
    km_prev4 = (sum((a.get("distance_m") or 0) for a in prev4) / 1000 / WINDOW_WEEKS
                if prev4 else None)
    paces = sorted(a["avg_pace_s_km"] for a in w4 if a.get("avg_pace_s_km"))
    med_pace = paces[len(paces) // 2] if paces else None
    quality4 = sum(1 for a in w4 if _quality_kind(a, max_hr, rest_hr, med_pace))
    n4 = len(w4)

    confidence = "high" if n4 >= MIN_RUNS_HIGH else ("medium" if n4 >= MIN_RUNS_MEDIUM else "low")
    stats = {"runs": n4, "weekly_km": round(km4, 1),
             "prev_weekly_km": round(km_prev4, 1) if km_prev4 else None,
             "quality_sessions": quality4}

    phase = "base"
    reasons: list[str] = []
    if km_prev4 and km4 < km_prev4 * 0.7 and quality4 <= 1:
        if race_date and 0 <= (race_date - today).days <= 21:
            phase = "taper"
            reasons.append(f"近 {WINDOW_WEEKS} 周跑量 {km4:.0f}km 较前 "
                           f"{WINDOW_WEEKS} 周 {km_prev4:.0f}km 下降明显，比赛临近，"
                           "符合减量期特征")
        else:
            phase = "base"
            reasons.append(f"近 {WINDOW_WEEKS} 周跑量 {km4:.0f}km 较前 "
                           f"{WINDOW_WEEKS} 周 {km_prev4:.0f}km 明显下降（中断/恢复？），"
                           "建议从基础期重建")
    elif quality4 >= 3 and (km_prev4 is None or km4 >= km_prev4 * 0.9):
        phase = "final"
        reasons.append(f"近 {WINDOW_WEEKS} 周强度课 {quality4} 次且跑量维持"
                       f"（周均 {km4:.0f}km），符合最终强度期")
    elif quality4 >= 2:
        phase = "transition"
        reasons.append(f"近 {WINDOW_WEEKS} 周强度课 {quality4} 次"
                       f"（周均 {km4:.0f}km），接近过渡期/强化期")
    elif quality4 >= 1 or km4 >= 25:
        phase = "early"
        reasons.append(f"近 {WINDOW_WEEKS} 周跑量 {km4:.0f}km"
                       + (f"，强度课 {quality4} 次" if quality4 else "")
                       + "，处于早期强度建立阶段")
    else:
        phase = "base"
        reasons.append(f"近 {WINDOW_WEEKS} 周以轻松有氧为主"
                       f"（周均 {km4:.0f}km，强度课 {quality4} 次），符合基础期")
    if n4 < MIN_RUNS_MEDIUM:
        reasons.append("近期活动记录较少，判断可能不准确，可手动选择所处时期")

    return {"phase": phase, "phase_name": PHASE_NAMES[phase],
            "confidence": confidence, "reasons": reasons, "stats": stats}
