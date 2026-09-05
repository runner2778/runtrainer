"""当前水平预估（各距离）：综合手表 VO2max、近几个月配速-心率趋势、
间歇/节奏跑能力与近期比赛成绩，输出各距离等效成绩与建议 VDOT。

设计参考手表/训练平台的能力模型：
- 比赛成绩 = 硬证据（权重最高）
- 手表 VO2max = 长期能力底数
- 阈值配速（88% HRmax 对应的配速）来自近 90 天配速-心率回归，反映有氧能力趋势
- 间歇 work 段配速反映最大摄氧量上限
纯函数层：接收活动列表与档案读数，不碰 DB。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import vdot as vd

# 标准比赛距离带 ± 容差
RACE_BANDS = ((5000, 0.10), (10000, 0.10), (21097, 0.05), (42195, 0.05))
RACE_NAME_HINTS = ("比赛", "race", "5k", "10k", "半马", "全马", "marathon", "test")
# 心率-配速趋势样本要求
TREND_MIN_RUNS = 5
TREND_MIN_DURATION_S = 15 * 60
TREND_PACE_MAX_S_KM = 8 * 60  # 慢于 8:00/km 的散步不参与回归
TREND_PACE_MIN_S_KM = 270  # 快于 4:30/km 的样本剔除：多为间歇日（停表无 laps、
# 无法分段）或 GPS 漂移，整场配速被快段拉低而心率不高，不是稳定配速样本
TREND_MIN_HR = 100  # 跑中平均心率低于此值视为传感器异常/GPS 漂移失真样本
TREND_MIN_HR_SPAN = 20  # 样本心率跨度太窄的回归无外推意义
TREND_MAX_EXTRAP_BPM = 10  # 目标心率超出样本最大心率的外推上限；超出则弃用趋势
# 分量（E 区样本外推 20+ bpm 不可靠；跑过真 T 跑、样本覆盖 88% HRmax 时才生效）
# 间歇 work 段长度区间（米）
WORK_SEG_RANGE_M = (300.0, 2000.0)
SPRINT_SEG_MAX_M = 500.0  # ≤500m 的 work 段按冲刺（R）强度反算，否则按间歇（I）
# 间歇恢复时间变量：休息/快跑时长比参考值 0.6；休息越短 → 同样快段配速
# 代表更高水平，每偏离 1 个单位修正 20%（速度→VDOT 近似线性）
INTERVAL_REST_REF = 0.6
INTERVAL_REST_K = 0.20
# 储备心率(HRR)配速分量
HRR_TARGET = 0.70        # 用 70% HRR 对应配速反推（有氧区代表强度）
HRR_MIN = 0.40           # 过低 HRR 样本（慢走/热身）剔除
HRR_MAX = 0.85           # 高于此属阈值区，非有氧稳定样本
HRR_MIN_SPAN = 0.10      # 样本 HRR 跨度太窄回归无意义
HRR_MAX_EXTRAP = 0.08    # 目标 70% HRR 超出样本最大 HRR 的外推上限
# %HRR ≈ %VO2max（Karvonen）；跑步速度的 %vVO2 略高于 %VO2max（经验线性换算）
HRR_TO_VVO2 = 0.8
HRR_TO_VVO2_OFFSET = 0.2
# 同配速心率时期趋势（进步/退步检测）
HR_TREND_WINDOW_DAYS = 180
HR_TREND_VDOT_PER_BPM = 0.35   # 同一配速下心率每降 1 bpm ≈ 0.35 VDOT（保守经验值）
HR_TREND_ADJ_MIN = -0.5        # 心率上升惩罚下限（天气/疲劳也可能抬心率，不重罚）
HR_TREND_ADJ_MAX = 1.5         # 进步奖励上限（趋势是软证据，保守加分）


def _theil_sen(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Theil-Sen 稳健回归（两两斜率中位数，纯 Python）。

    真实手表数据混有 GPS 漂移（假快配速）与心率带接触不良（假低心率）
    样本，普通最小二乘会被离群点拉偏、外推阈值配速虚高；Theil-Sen 对
    最多 ~29% 离群点鲁棒。点不足返回 None。
    """
    n = len(xs)
    if n < 3:
        return None
    slopes: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            if dx != 0:
                slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return None
    slopes.sort()
    slope = slopes[len(slopes) // 2]
    intercepts = sorted(ys[i] - slope * xs[i] for i in range(n))
    return slope, intercepts[len(intercepts) // 2]


def _vdot_for_pace(pace_s_km: float, pct: float) -> float:
    """二分：找 vdot 使 pace_s_km(vdot, pct) == pace。pace_table 单调递减。"""
    lo, hi = 20.0, 100.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if vd.pace_s_km(mid, pct) > pace_s_km:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 1)


def estimate_max_hr(profile_max_hr, activities: list[dict]) -> float | None:
    """HRmax：档案设置优先；否则取近 90 天活动 max_hr 的 95 分位。"""
    if profile_max_hr:
        return float(profile_max_hr)
    vals = sorted(a["max_hr"] for a in activities if a.get("max_hr"))
    if not vals:
        return None
    idx = min(len(vals) - 1, int(len(vals) * 0.95))
    return float(vals[idx])


def _is_interval_day(structure) -> bool:
    """真间歇课判定：≥2 个快跑段。采样级识别后匀速跑是单个 continuous 段、
    长距离跑中的单次提速不会产生多快段；只有间歇/重复课才有多段 work。"""
    segs = structure or []
    return sum(1 for s in segs if s.get("type") == "work") >= 2


def threshold_pace_from_trend(activities: list[dict], max_hr: float) -> dict | None:
    """近 90 天配速-心率线性回归 → 88% HRmax（阈值）对应配速。

    只采用稳定配速样本：间歇课的整场 avg_pace 被快段拉低而 avg_hr 不高，
    混入会让回归斜率失真、外推阈值配速虚高——间歇能力已由 interval_ability
    分量单独反映，此处排除；心率 <100 的失真样本一并剔除。
    """
    if not max_hr:
        return None
    pts = [(a["avg_pace_s_km"], a["avg_hr"]) for a in activities
           if a.get("avg_pace_s_km") and a.get("avg_hr")
           and a["avg_hr"] >= TREND_MIN_HR
           and (a.get("duration_s") or 0) >= TREND_MIN_DURATION_S
           and TREND_PACE_MIN_S_KM <= a["avg_pace_s_km"] <= TREND_PACE_MAX_S_KM
           and not _is_interval_day(a.get("structure"))]
    if len(pts) < TREND_MIN_RUNS:
        return None
    hrs = [h for _, h in pts]
    if max(hrs) - min(hrs) < TREND_MIN_HR_SPAN:
        return None
    reg = _theil_sen([p for p, _ in pts], hrs)
    if not reg:
        return None
    slope, intercept = reg
    # 配速越快（s/km 越小）心率越高 → 斜率应为负；异常斜率直接弃用
    if slope >= 0:
        return None
    target_hr = max_hr * 0.88
    if target_hr - max(hrs) > TREND_MAX_EXTRAP_BPM:
        return None  # 外推超出样本心率范围：E 区样本推不出阈值配速
    pace = (target_hr - intercept) / slope
    if pace <= 0 or pace > TREND_PACE_MAX_S_KM:
        return None
    return {"pace_s_km": round(pace, 1), "n_runs": len(pts),
            "vdot": _vdot_for_pace(pace, vd.T_PCT)}


def best_recent_race(activities: list[dict]) -> dict | None:
    """近 180 天内的最佳比赛成绩（标准距离带内，名称/心率双判定）。"""
    best = None
    for a in activities:
        dist = a.get("distance_m")
        dur = a.get("duration_s")
        if not dist or not dur or dist < 3000 or dist > 43000:
            continue
        name = (a.get("name") or "").lower()
        hr_ok = bool(a.get("avg_hr") and a.get("max_hr")
                     and a["avg_hr"] / a["max_hr"] >= 0.88)
        name_ok = any(h in name for h in RACE_NAME_HINTS)
        if not (hr_ok or name_ok):
            continue
        for std, tol in RACE_BANDS:
            if abs(dist - std) / std > tol:
                continue
            v = vd.estimate_vdot(dist, dur)
            if best is None or v > best["vdot"]:
                best = {"distance_m": int(std), "duration_s": round(dur),
                        "date": a.get("start_ts"), "name": a.get("name"),
                        "vdot": round(v, 1), "raw_distance_m": dist,
                        "avg_hr": a.get("avg_hr"), "max_hr": a.get("max_hr")}
    return best


def interval_ability(activities: list[dict]) -> dict | None:
    """间歇能力（含恢复时间变量）：按课聚合 work 段配速，短段（≤500m）按
    冲刺强度 R 反算、长段按间歇强度 I 反算；再按该课「休息时长/快跑时长」
    修正——休息越短、快段越快 → 水平越高。多课取中位数抗单课噪声。"""
    workouts = []
    for a in activities:
        structure = a.get("structure") or []
        if isinstance(structure, str):
            continue
        works, rests = [], []
        for s in structure:
            if (s.get("type") == "work" and s.get("pace_s_km")
                    and WORK_SEG_RANGE_M[0] <= (s.get("distance_m") or 0) <= WORK_SEG_RANGE_M[1]):
                works.append(s)
            elif s.get("type") == "rest" and (s.get("elapsed_s") or 0) > 0:
                rests.append(s)
        if len(works) < 2:
            continue  # 单段 work 不算间歇课
        paces = sorted(w["pace_s_km"] for w in works)
        med_pace = paces[len(paces) // 2]
        dists = sorted(w["distance_m"] or 0 for w in works)
        med_dist = dists[len(dists) // 2]
        pct = vd.R_PCT if med_dist <= SPRINT_SEG_MAX_M else vd.I_PCT
        v = _vdot_for_pace(med_pace, pct)
        work_t = sum(w.get("elapsed_s") or (w.get("distance_m") or 0)
                     * (w["pace_s_km"] or 300) / 1000 for w in works)
        rest_t = sum(r.get("elapsed_s") or 0 for r in rests)
        # 无休息段（自动暂停吞掉休息圈）时休息比未知，不做修正——按 0 修正
        # 会误把缺数据当成"零休息"，虚高整课水平
        ratio = rest_t / work_t if work_t > 0 and rests else None
        if ratio is not None:
            v *= 1 + INTERVAL_REST_K * (INTERVAL_REST_REF - ratio)
        workouts.append({"vdot": v, "ratio": ratio,
                         "pace_s_km": med_pace, "n": len(works)})
    if not workouts:
        return None
    vdots = sorted(w["vdot"] for w in workouts)
    med_v = vdots[len(vdots) // 2]
    ratios = [w["ratio"] for w in workouts if w["ratio"] is not None]
    med_ratio = sorted(ratios)[len(ratios) // 2] if ratios else None
    seg_paces = sorted(w["pace_s_km"] for w in workouts)
    med_pace = seg_paces[len(seg_paces) // 2]
    return {"pace_s_km": round(med_pace, 1),
            "n_segments": sum(w["n"] for w in workouts),
            "n_workouts": len(workouts),
            "rest_ratio": round(med_ratio, 2) if med_ratio is not None else None,
            "vdot": round(med_v, 1)}


def hrr_ability(activities: list[dict], max_hr: float, rest_hr: float) -> dict | None:
    """储备心率(HRR)对应配速：有氧样本的 配速~HRR 回归 → 70% HRR 对应配速。

    HRR 剔除了静息心率的个体差异与波动，比绝对心率更可比；同一 HRR 下
    配速更快 = 有氧能力更强。%HRR≈%VO2max（Karvonen），换算 %vVO2 后反算
    VDOT。同样排除含 work 段的间歇日（整场配速被快段拉低失真）。
    """
    if not max_hr or not rest_hr or max_hr <= rest_hr:
        return None
    pts = [(a["avg_pace_s_km"], (a["avg_hr"] - rest_hr) / (max_hr - rest_hr))
           for a in activities
           if a.get("avg_pace_s_km") and a.get("avg_hr")
           and (a.get("duration_s") or 0) >= TREND_MIN_DURATION_S
           and TREND_PACE_MIN_S_KM <= a["avg_pace_s_km"] <= TREND_PACE_MAX_S_KM
           and not _is_interval_day(a.get("structure"))]
    if len(pts) < TREND_MIN_RUNS:
        return None
    hrrs = [h for _, h in pts]
    if max(hrrs) - min(hrrs) < HRR_MIN_SPAN:
        return None
    # x=HRR, y=配速：HRR 越高配速越快（斜率负）；异常斜率弃用
    reg = _theil_sen(hrrs, [p for p, _ in pts])
    if not reg or reg[0] >= 0:
        return None
    slope, intercept = reg
    if HRR_TARGET - max(hrrs) > HRR_MAX_EXTRAP:
        return None  # 样本没跑到 70% HRR 附近，外推不可靠
    pace = slope * HRR_TARGET + intercept
    if pace <= 0 or pace > TREND_PACE_MAX_S_KM:
        return None
    pct_vvo2 = HRR_TO_VVO2 * HRR_TARGET + HRR_TO_VVO2_OFFSET
    return {"pace_s_km": round(pace, 1), "n_runs": len(pts),
            "hrr_pct": HRR_TARGET, "rest_hr": round(rest_hr, 1),
            "vdot": _vdot_for_pace(pace, pct_vvo2)}


def hr_trend_ability(activities: list[dict], as_of: date | None = None) -> dict | None:
    """同配速平均心率的时期变化 → 有氧能力进步/退步检测（VDOT 调整量）。

    用 pace_bin_hr 首尾两期代表档（跑量最大档）的心率差：同一配速下
    心率下降 = 有氧能力进步。趋势是软证据，调整量保守钳制
    [-0.5, +1.5] VDOT；首尾期无可比档或样本不足返回 None。
    """
    from .workout_analysis import pace_bin_hr
    end = as_of or date.today()
    start = end - timedelta(days=HR_TREND_WINDOW_DAYS)
    out = pace_bin_hr(activities, start, end)
    s = out.get("summary") or {}
    drop = s.get("best_drop")
    if drop is None or len(out.get("periods") or []) < 2:
        return None
    adj = max(HR_TREND_ADJ_MIN, min(HR_TREND_ADJ_MAX, -drop * HR_TREND_VDOT_PER_BPM))
    return {"drop_bpm": round(drop, 1), "pace_label": s.get("best_label"),
            "adj_vdot": round(adj, 2)}


def _weighted(components: list[dict]) -> float | None:
    comps = [c for c in components if c and c.get("vdot")]
    if not comps:
        return None
    total = sum(c["weight"] for c in comps)
    return round(sum(c["vdot"] * c["weight"] for c in comps) / total, 1)


def compute_ability(activities: list[dict], vo2max: float | None,
                    profile_max_hr=None, rest_hr: float | None = None,
                    as_of: date | None = None) -> dict:
    """综合各数据源输出水平预估。

    activities: 近 180 天活动（含 avg_pace_s_km/avg_hr/max_hr/distance_m/
                duration_s/start_ts/name，可选 structure 分段）。
    rest_hr: 静息心率（HRR 分量必需）；缺省时该分量跳过。
    返回 {"vdot", "predictions", "evidence", "as_of"}；无任何依据时 vdot=None。
    """
    max_hr = estimate_max_hr(profile_max_hr, activities)
    race = best_recent_race(activities)
    threshold = threshold_pace_from_trend(activities, max_hr) if max_hr else None
    intervals = interval_ability(activities)
    hrr = hrr_ability(activities, max_hr, rest_hr) if max_hr and rest_hr else None

    evidence: list[dict] = []
    components: list[dict] = []
    if race:
        components.append({"vdot": race["vdot"], "weight": 0.40, "kind": "race"})
        evidence.append({
            "source": "recent_race",
            "vdot": race["vdot"],
            "detail": f"近期最佳比赛 {race['distance_m']}m",
            "race": race,
        })
    if vo2max:
        components.append({"vdot": round(float(vo2max), 1), "weight": 0.25, "kind": "vo2max"})
        evidence.append({"source": "garmin_vo2max", "vdot": round(float(vo2max), 1),
                         "detail": "手表 VO2max 读数"})
    if threshold:
        components.append({"vdot": threshold["vdot"], "weight": 0.15, "kind": "threshold"})
        evidence.append({"source": "threshold_trend", "vdot": threshold["vdot"],
                         "detail": f"阈值配速 {_fmt_pace(threshold['pace_s_km'])}/km"
                                   f"（{threshold['n_runs']} 次心率-配速回归）",
                         "pace_s_km": threshold["pace_s_km"]})
    if hrr:
        components.append({"vdot": hrr["vdot"], "weight": 0.10, "kind": "hrr"})
        evidence.append({"source": "hrr_pace", "vdot": hrr["vdot"],
                         "detail": f"{int(hrr['hrr_pct'] * 100)}% HRR 对应配速 "
                                   f"{_fmt_pace(hrr['pace_s_km'])}/km"
                                   f"（{hrr['n_runs']} 次有氧样本，静息心率 {hrr['rest_hr']}）",
                         "pace_s_km": hrr["pace_s_km"]})
    if intervals:
        components.append({"vdot": intervals["vdot"], "weight": 0.10, "kind": "interval"})
        detail = (f"间歇 {_fmt_pace(intervals['pace_s_km'])}/km"
                  f"（{intervals['n_workouts']} 课 {intervals['n_segments']} 段")
        if intervals.get("rest_ratio") is not None:
            detail += f"，休息/快跑比 {intervals['rest_ratio']}（越短水平越高）"
        evidence.append({"source": "interval_ability", "vdot": intervals["vdot"],
                         "detail": detail + "）",
                         "pace_s_km": intervals["pace_s_km"]})
    # 缺比赛时重新分配权重（表盘读数权重最高，趋势与 HRR 其次）
    if not race and components:
        weights = {"vo2max": 0.40, "threshold": 0.28, "hrr": 0.16,
                   "interval": 0.16, "race": 0.40}
        for c in components:
            c["weight"] = weights[c["kind"]]
    vdot_val = _weighted(components)
    # 同配速心率趋势调整：心率下降 = 有氧能力进步（软证据，保守幅度）。
    # 放在加权之后、比赛上限钳制之前——比赛成绩仍是最终硬上限。
    trend = hr_trend_ability(activities, as_of)
    if trend and vdot_val:
        vdot_val = round(vdot_val + trend["adj_vdot"], 1)
        up_down = "下降" if trend["drop_bpm"] < 0 else "上升"
        evidence.append({
            "source": "hr_trend",
            "vdot": vdot_val,
            "detail": f"同配速（{trend['pace_label']}/km）平均心率较早期"
                      f"{up_down} {abs(trend['drop_bpm']):.0f} bpm → "
                      f"调整 {'+' if trend['adj_vdot'] >= 0 else ''}{trend['adj_vdot']} VDOT",
        })
    # 上限钳制：能力预估不高于「已跑出的最佳比赛成绩等效 VDOT + 2」——
    # 手表 VO2max 读数偏高时（如 63 vs 比赛等效 48.9）不能把课表配速
    # 拉到无法完成的水平；下限不加钳制（状态差时按保守值训练）
    if race and vdot_val:
        vdot_val = round(min(vdot_val, race["vdot"] + 2.0), 1)
    result = {
        "vdot": vdot_val,
        "predictions": vd.equivalent_times(vdot_val) if vdot_val else None,
        "evidence": evidence,
        "max_hr": round(max_hr) if max_hr else None,
        "as_of": (as_of or date.today()).isoformat(),
    }
    return result


def _fmt_pace(s: float) -> str:
    m = int(s // 60)
    sec = int(round(s % 60))
    if sec >= 60:
        m += 1
        sec -= 60
    return f"{m}:{sec:02d}"
