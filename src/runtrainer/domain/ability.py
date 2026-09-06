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
# ---- 间歇强度类型识别（第十五批）：同一组快跑段按
# 心率 / 休息结构 / 段落长度判断落在哪个生理区间，各自按对应 %VDOT 反算
INTERVAL_VO2MAX_HR_MIN = 0.92   # 段落平均心率 ≥92% HRmax：已到 V·O2 区（铁证，优先）
INTERVAL_T_HR_WIN = (0.84, 0.92)  # 长段落在阈值心率窗口内 → 乳酸阈值/巡航
INTERVAL_THRESH_REST_MAX = 0.55  # 休息/快跑比 ≤0.55 → 短休连续刺激 → 乳酸阈值型
INTERVAL_THRESH_DIST_MIN = 1000.0  # 无可靠心率时 ≥1km 长段落按阈值巡航处理
# 节奏跑（连续匀速巡航，非间歇）识别：15–60min、平均心率 84–94% HRmax。
# 配速窗口放宽（3:20–8:20/km，不只收 4:30 内）——阈值课配速随水平浮动，
# 心率带本身已排除慢走/假慢样本；GPS 假快样本是「快配速+低心率」，同样进不来
CRUISE_MIN_DURATION_S = 15 * 60
CRUISE_MAX_DURATION_S = 60 * 60
CRUISE_PACE_WIN = (200.0, 500.0)
CRUISE_HR_MIN = 0.84   # LT2≈88% HRmax，阈值课整场平均约在此下沿之上
CRUISE_HR_MAX = 0.94   # 更高多为全力/比赛节奏，不做巡航样本
CRUISE_MIN_RUNS = 2
# 近一年 PB 参与预估：只取超出现有估计的差距的一部分（PB 是过去最好状态，
# 当前水平不能被一次巅峰成绩拉满），且随时间衰减（越久越软），封顶防失控
PB_MIN_GAP = 0.3
PB_WEIGHT = 0.35
PB_BOOST_CAP = 1.5
PB_FULL_RECENT_DAYS = 180        # 半年内 PB 全额计权
PB_DECAY_DAYS = 364              # 一年前衰减到权重下限
PB_DECAY_FLOOR = 0.35
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


# ---- 近一年各距离最佳成绩 + 训练保持度（第十四批）----
# 水平预估此前只看近 180 天（30 天卡/180 天向导）；用户要求把「近一年各项
# 距离最好成绩、训练保持度」也读进来：比赛/近似距离整场活动是硬证据，平日
# 长跑里滑窗切出的最快分段（Garmin Best Effort 思路）补足没跑过正式比赛
# 的距离；保持度用「周活跃率 + 近 4 周跑量 vs 全年周均」量化。
YEAR_BEST_DISTANCES = (("5K", 5000), ("10K", 10000), ("半马", 21097), ("全马", 42195))
YEAR_BEST_TOL = 0.06        # 整场活动距离与标准距离容差（≈近似全程的比赛）
SEG_RANGE = (0.97, 1.12)    # 滑窗覆盖距离相对标准距离的下/上界
SEG_MIN_HR_RATIO = 0.82     # 分段平均心率 ≥82% 最大心率才认（防散步/轻松段）
YEAR_SAMPLE_RUN_CAP = 40    # 参与分段扫描的活动上限（防超大库拖慢聊天/看板）
CONSISTENCY_YEAR_DAYS = 364


def distance_bests(activities: list[dict], get_samples=None,
                   max_hr: float | None = None) -> list[dict]:
    """近一年（调用方给窗口内活动）各标准距离最佳成绩。

    - 整场近似比赛：距离在容差带内且（平均心率 ≥88% 活动最高心率 或 名称含
      比赛标记）→ 直接折算 VDOT；
    - 最长分段（Best Effort）：带样本的更长跑中，按时间滑窗找覆盖距离在
      [0.97, 1.12]×标准距离、最快的最短耗时窗口；窗口平均心率 ≥82% 最大心率
      才认（无心率样本的窗口不参与——配速可能被下坡/漂移虚高）。
    get_samples(activity_id) -> 按时间序的样本 dict 列表（t_offset_s/hr/speed_mps）。
    返回按距离升序的 [{distance, best_seconds(按最快分段配速折算标准距离), date,
    vdot, name, source: race|effort, pace_s_km, avg_hr}]；无依据的距离不出现。
    """
    best: dict[str, dict] = {}
    scanned = 0
    for a in activities:
        dist = a.get("distance_m") or 0
        dur = a.get("duration_s") or 0
        if not dist or not dur:
            continue
        name = (a.get("name") or "").lower()
        a_hr_ratio = ((a.get("avg_hr") or 0) / (a.get("max_hr") or 0)
                      if a.get("avg_hr") and a.get("max_hr") else None)
        name_ok = any(h in name for h in RACE_NAME_HINTS)
        whole_ok = (a_hr_ratio is not None and a_hr_ratio >= 0.88) or name_ok
        for label, std in YEAR_BEST_DISTANCES:
            if whole_ok and abs(dist - std) / std <= YEAR_BEST_TOL and dur > 0:
                v = vd.estimate_vdot(dist, dur)
                rec = {"distance": label, "best_seconds": round(dur),
                       "date": a.get("date"), "vdot": round(v, 1),
                       "name": a.get("name") or "", "source": "race",
                       "pace_s_km": round(dur / dist * 1000, 1),
                       "avg_hr": a.get("avg_hr")}
                _keep_best(best, label, rec)
        # 分段扫描：仅处理带样本、距离够长到能切出最小标准距离的跑；
        # 心率样本缺失的不切（配速可能虚高，宁缺毋滥）
        if not get_samples or scanned >= YEAR_SAMPLE_RUN_CAP:
            continue
        if not a.get("has_samples") or dist < YEAR_BEST_DISTANCES[0][1] * SEG_RANGE[0]:
            continue
        if a.get("id") is None:
            continue
        scanned += 1
        try:
            samples = get_samples(a["id"]) or []
        except Exception:
            continue
        seg_bests = _best_effort_windows(samples, max_hr)
        if not seg_bests:
            continue
        for label, (pace_s_m, hr_avg) in seg_bests.items():
            std_m = dict(YEAR_BEST_DISTANCES)[label]  # 外层循环结束后 std 已失效
            proj_s = pace_s_m * std_m  # 以最快分段配速折算整段标准距离
            rec = {"distance": label, "best_seconds": round(proj_s),
                   "date": a.get("date"),
                   "vdot": round(vd.estimate_vdot(std_m, proj_s), 1),
                   "name": f"{a.get('name') or '跑步'}·{label}最快段",
                   "source": "effort",
                   "pace_s_km": round(pace_s_m * 1000, 1),
                   "avg_hr": round(hr_avg) if hr_avg else None}
            _keep_best(best, label, rec)
    return [best[l] for l, _ in YEAR_BEST_DISTANCES if l in best]


def _keep_best(best: dict, label: str, rec: dict) -> None:
    """同距离多证据取等效 VDOT 最高者（比赛硬证据自带权重，无需特殊处理）。"""
    if label not in best or rec["vdot"] > best[label]["vdot"]:
        best[label] = rec


def _best_effort_windows(samples: list[dict], max_hr: float | None,
                         ) -> dict[str, tuple[float, float | None]]:
    """一次活动内，为每个标准距离滑窗求最快分段。

    返回 {label: (最快分段配速 s/m, 窗口平均心率 | None)}。窗口覆盖距离在
    [0.97, 1.12]×标准之间时按「每米用时」比较——直接比总耗时会让刚好
    压到区间下沿的短窗口假快。前缀和 O(n) 双指针：覆盖 <0.97×标准时右端
    前进，>1.12×标准时左端前进直到窗口重新落回区间或右端到头。
    """
    n = len(samples)
    if n < 2:
        return {}
    # 前缀：pd[k]/pt[k] = 前 k 个样本累计距离/时间；ph/pc 为累计心率与样本数
    pd = [0.0] * (n + 1)
    pt = [0.0] * (n + 1)
    ph = [0.0] * (n + 1)
    pc = [0] * (n + 1)
    t_prev = None
    for i, s in enumerate(samples):
        t = float(s.get("t_offset_s") or 0)
        spd = float(s.get("speed_mps") or 0)
        dt = (t - t_prev) if t_prev is not None else 0
        if dt < 0:  # 异常时间戳回退视为 0
            dt = 0
        pd[i + 1] = pd[i] + (max(spd, 0) * dt if t_prev is not None else 0)
        pt[i + 1] = t
        hr = s.get("hr")
        ph[i + 1] = ph[i] + (float(hr) if hr else 0)
        pc[i + 1] = pc[i] + (1 if hr else 0)
        t_prev = t
    out: dict[str, tuple[float, float, float | None]] = {}
    for label, std in YEAR_BEST_DISTANCES:
        lo, hi = std * SEG_RANGE[0], std * SEG_RANGE[1]
        j = 0
        for i in range(n + 1):
            if j <= i:
                j = i + 1
            while j <= n and pd[j] - pd[i] < lo:
                j += 1
            if j > n:
                break
            d = pd[j] - pd[i]
            if d > hi:
                continue  # 距离越长耗时未必更长，左端继续前进找更短覆盖
            t = pt[j] - pt[i]
            if t <= 0:
                continue
            hr_n = pc[j] - pc[i]
            hr_avg = (ph[j] - ph[i]) / hr_n if hr_n > 0 else None
            # 有最大心率可依时，无心率样本的窗口不参与（配速可能被下坡/漂移虚高）
            if max_hr and (hr_avg is None or hr_avg < max_hr * SEG_MIN_HR_RATIO):
                continue
            pace_s_m = t / d if d > 0 else 0  # 每米用时：公平跨窗口长度比较
            cur = out.get(label)
            if cur is None or pace_s_m < cur[0]:
                out[label] = (pace_s_m, hr_avg)
    return out


def training_consistency(activities: list[dict], today: date | None = None) -> dict:
    """近一年训练保持度：周活跃率 + 近 4 周跑量相对全年周均。

    activities 为近一年活动（含 date 或 start_ts）。返回
    {run_weeks, total_weeks, run_week_pct, recent_4w_avg_km, recent_vs_year_pct}；
    无任何跑步时 total_weeks=1、各值 0（避免除零）。
    """
    today = today or date.today()
    runs = []
    for a in activities:
        d = a.get("date") or a.get("start_ts")
        if not d:
            continue
        try:
            day = date.fromisoformat(d[:10]) if isinstance(d, str) else date.fromisoformat(d)
        except (ValueError, TypeError):
            continue
        km = (a.get("distance_m") or 0) / 1000
        if day > today or day < today - timedelta(days=CONSISTENCY_YEAR_DAYS):
            continue
        runs.append((day, km))
    if not runs:
        return {"run_weeks": 0, "total_weeks": 1, "run_week_pct": 0,
                "recent_4w_avg_km": 0, "recent_vs_year_pct": 0}
    first_day = min(day for day, _ in runs)
    week_span = (today - first_day).days // 7 + 1   # 覆盖周数（从首次跑步起）
    run_weeks = {((today - day).days // 7) for day, _ in runs}
    run_weeks = {i for i in run_weeks if 0 <= i < week_span}
    year_km = sum(km for _, km in runs)
    year_avg = year_km / week_span if week_span else 0
    recent_km = sum(km for day, km in runs if day >= today - timedelta(days=27))
    recent_avg = recent_km / 4
    return {
        "run_weeks": len(run_weeks),
        "total_weeks": week_span,
        "run_week_pct": round(len(run_weeks) / week_span * 100),
        "recent_4w_avg_km": round(recent_avg, 1),
        "recent_vs_year_pct": round(recent_avg / year_avg * 100) if year_avg > 0 else 0,
    }


def _interval_type(works: list[dict], rests: list[dict], max_hr: float | None) -> str:
    """按心率/休息结构/段落长度判定整课间歇的刺激类型。

    规则（顺序即优先级）：
    1. 段落平均心率 ≥92% HRmax → 最大摄氧量型（心率是生理金标准，最高优先；
       无氧冲刺段短、段均心率读不到这么高，不会误伤）；
    2. 休息/快跑比 ≤0.55 → 短休连续刺激 → 乳酸阈值型（典型 T 巡航间歇）；
    3. 段落中位距离 ≤500m → 无氧冲刺型（R）；
    4. 段均心率落在阈值窗口 84–92% HRmax、或 >=1km 长段落无心率依据 →
       乳酸阈值型（巡航间歇/长距离重复）；
    5. 其余 → 最大摄氧量型（600–1000m 经典 I 间歇）。
    """
    hrs = [w.get("avg_hr") for w in works if w.get("avg_hr")]
    hr_ratio = (sorted(hrs)[len(hrs) // 2] / max_hr) if hrs and max_hr else None
    work_t = sum(w.get("elapsed_s") or (w.get("distance_m") or 0)
                 * (w["pace_s_km"] or 300) / 1000 for w in works)
    rest_t = sum(r.get("elapsed_s") or 0 for r in rests)
    rest_ratio = rest_t / work_t if work_t > 0 and rests else None
    dists = sorted(w["distance_m"] or 0 for w in works)
    med_dist = dists[len(dists) // 2]
    if hr_ratio is not None and hr_ratio >= INTERVAL_VO2MAX_HR_MIN:
        return "vo2max"
    if rest_ratio is not None and rest_ratio <= INTERVAL_THRESH_REST_MAX:
        return "threshold"
    if med_dist <= SPRINT_SEG_MAX_M:
        return "speed"
    if med_dist >= INTERVAL_THRESH_DIST_MIN and (
            hr_ratio is None or INTERVAL_T_HR_WIN[0] <= hr_ratio < INTERVAL_T_HR_WIN[1]):
        return "threshold"
    return "vo2max"


_PCT_BY_INTERVAL_TYPE = {"threshold": vd.T_PCT, "vo2max": vd.I_PCT, "speed": vd.R_PCT}


def interval_ability(activities: list[dict], max_hr: float | None = None) -> dict | None:
    """间歇能力（按刺激类型 + 恢复时间变量）：逐课先按心率/休息结构/段落
    长度识别类型（乳酸阈值/最大摄氧量/无氧冲刺），再以该类型对应 %VDOT
    反算单课 VDOT；休息越短、快段越快 → 水平越高（恢复比修正）。返回总体
    中位数 vdot（跨类型合并抗单课噪声）与各类型分项 types，供分量与展示用。"""
    workouts: list[dict] = []
    by_type: dict[str, list[dict]] = {"threshold": [], "vo2max": [], "speed": []}
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
        itype = _interval_type(works, rests, max_hr)
        v = _vdot_for_pace(med_pace, _PCT_BY_INTERVAL_TYPE[itype])
        work_t = sum(w.get("elapsed_s") or (w.get("distance_m") or 0)
                     * (w["pace_s_km"] or 300) / 1000 for w in works)
        rest_t = sum(r.get("elapsed_s") or 0 for r in rests)
        # 无休息段（自动暂停吞掉休息圈）时休息比未知，不做修正——按 0 修正
        # 会误把缺数据当成"零休息"，虚高整课水平
        ratio = rest_t / work_t if work_t > 0 and rests else None
        if ratio is not None:
            v *= 1 + INTERVAL_REST_K * (INTERVAL_REST_REF - ratio)
        rec = {"vdot": v, "ratio": ratio, "pace_s_km": med_pace,
               "n": len(works), "type": itype}
        workouts.append(rec)
        by_type[itype].append(rec)
    if not workouts:
        return None
    vdots = sorted(w["vdot"] for w in workouts)
    med_v = vdots[len(vdots) // 2]
    ratios = [w["ratio"] for w in workouts if w["ratio"] is not None]
    med_ratio = sorted(ratios)[len(ratios) // 2] if ratios else None
    seg_paces = sorted(w["pace_s_km"] for w in workouts)
    med_pace = seg_paces[len(seg_paces) // 2]

    def _type_median(key: str) -> dict | None:
        recs = by_type[key]
        if not recs:
            return None
        vs = sorted(r["vdot"] for r in recs)
        ps = sorted(r["pace_s_km"] for r in recs)
        return {"vdot": round(vs[len(vs) // 2], 1),
                "pace_s_km": round(ps[len(ps) // 2], 1),
                "n_workouts": len(recs),
                "n_segments": sum(r["n"] for r in recs)}

    types = {k: _type_median(k) for k in ("threshold", "vo2max", "speed")}
    types = {k: v for k, v in types.items() if v}
    return {"pace_s_km": round(med_pace, 1),
            "n_segments": sum(w["n"] for w in workouts),
            "n_workouts": len(workouts),
            "rest_ratio": round(med_ratio, 2) if med_ratio is not None else None,
            "vdot": round(med_v, 1), "types": types}


def cruise_ability(activities: list[dict], max_hr: float) -> dict | None:
    """节奏跑/巡航能力：连续（非间歇、非比赛）15–60min 匀速跑，平均心率
    84–94% HRmax → 阈值课特征。多课取中位配速按 T（88% VDOT）反算——
    作为「阈值配速回归」缺失时的替代分量（同一生理信号的另一种测法）。"""
    if not max_hr:
        return None
    paces = []
    for a in activities:
        if not a.get("avg_pace_s_km") or not a.get("avg_hr"):
            continue
        if _is_interval_day(a.get("structure")):
            continue
        dur = a.get("duration_s") or 0
        if not (CRUISE_MIN_DURATION_S <= dur <= CRUISE_MAX_DURATION_S):
            continue
        if not (CRUISE_PACE_WIN[0] <= a["avg_pace_s_km"] <= CRUISE_PACE_WIN[1]):
            continue
        hr_ratio = a["avg_hr"] / max_hr
        if not (CRUISE_HR_MIN <= hr_ratio <= CRUISE_HR_MAX):
            continue
        name = (a.get("name") or "").lower()
        if any(h in name for h in RACE_NAME_HINTS):
            continue  # 比赛/测试跑不进巡航样本
        if (a.get("distance_m") or 0) >= 3000 and hr_ratio >= 0.88:
            for std, tol in RACE_BANDS:
                if abs(a["distance_m"] - std) / std <= tol:
                    break  # 心率够高且距离落在标准比赛带 → 按比赛剔除
            else:
                paces.append(a["avg_pace_s_km"])
        else:
            paces.append(a["avg_pace_s_km"])
    if len(paces) < CRUISE_MIN_RUNS:
        return None
    paces.sort()
    med_pace = paces[len(paces) // 2]
    return {"pace_s_km": round(med_pace, 1), "n_runs": len(paces),
            "vdot": _vdot_for_pace(med_pace, vd.T_PCT)}


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
                    as_of: date | None = None,
                    year_bests: list[dict] | None = None) -> dict:
    """综合各数据源输出水平预估。

    activities: 近 180 天活动（含 avg_pace_s_km/avg_hr/max_hr/distance_m/
                duration_s/start_ts/name，可选 structure 分段）。
    rest_hr: 静息心率（HRR 分量必需）；缺省时该分量跳过。
    year_bests: distance_bests() 的近一年各距离最佳（含 vdot/date/distance/
                best_seconds/source）；显著快于当前估计时给保守加分（带时间衰减）。
    返回 {"vdot", "predictions", "zones", "evidence", "max_hr", "as_of"}；
    无任何依据时 vdot=None。
    """
    max_hr = estimate_max_hr(profile_max_hr, activities)
    race = best_recent_race(activities)
    threshold = threshold_pace_from_trend(activities, max_hr) if max_hr else None
    intervals = interval_ability(activities, max_hr)
    hrr = hrr_ability(activities, max_hr, rest_hr) if max_hr and rest_hr else None
    # 阈值分量三来源按可靠性递补：心率-配速回归（整场稳定样本，最全）→
    # 节奏/巡航跑中位配速（连续匀速课）→ 乳酸阈值型间歇（短休长段）。
    # 后者即便存在也仅作替代——混入会让单一阈值槽被多次加权。
    threshold_src = None
    t_typed = (intervals.get("types") or {}).get("threshold") if intervals else None
    if not threshold:
        cruise = cruise_ability(activities, max_hr) if max_hr else None
        if cruise:
            threshold, threshold_src = cruise, "cruise"
        elif t_typed:
            threshold = {"vdot": t_typed["vdot"], "pace_s_km": t_typed["pace_s_km"],
                         "n_runs": t_typed["n_workouts"]}
            threshold_src = "t_intervals"

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
        if threshold_src == "cruise":
            evidence.append({"source": "cruise_ability", "vdot": threshold["vdot"],
                             "detail": f"节奏/巡航跑中位配速 {_fmt_pace(threshold['pace_s_km'])}/km"
                                       f"（{threshold['n_runs']} 课，平均心率 84–94% HRmax）",
                             "pace_s_km": threshold["pace_s_km"]})
        elif threshold_src == "t_intervals":
            evidence.append({"source": "t_intervals", "vdot": threshold["vdot"],
                             "detail": f"乳酸阈值型间歇（短休长段）中位配速 "
                                       f"{_fmt_pace(threshold['pace_s_km'])}/km"
                                       f"（{threshold['n_runs']} 课）",
                             "pace_s_km": threshold["pace_s_km"]})
        else:
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
        types = intervals.get("types") or {}
        if len(types) > 1:
            name_map = {"threshold": "阈值", "vo2max": "摄氧", "speed": "冲刺"}
            parts = [f"{name_map[k]}型 {v['n_workouts']} 课" for k, v in types.items()]
            detail += "，" + "、".join(parts)
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
    # 近一年 PB 加分：PB 是「曾经跑出过」的证据——比当前估计快时按差距的
    # 一部分加分（0.35×），随距今时间衰减（180 天内全值 → 一年衰减到 35%），
    # 单次封顶 1.5 VDOT；与近期比赛同为一条记录（等效 VDOT 相同）时不重复计。
    if year_bests and vdot_val:
        pbs = [r for r in year_bests if r.get("vdot")]
        if pbs:
            pb = max(pbs, key=lambda r: r["vdot"])
            dup_race = bool(race and pb.get("source") == "race"
                            and abs(pb["vdot"] - race["vdot"]) < 0.1)
            gap = pb["vdot"] - vdot_val
            if not dup_race and gap > PB_MIN_GAP:
                age = _days_old(pb.get("date") or pb.get("start_ts"), as_of)
                recency = 1.0 if age <= PB_FULL_RECENT_DAYS else max(
                    PB_DECAY_FLOOR,
                    1.0 - (1.0 - PB_DECAY_FLOOR) * (age - PB_FULL_RECENT_DAYS)
                    / max(PB_DECAY_DAYS - PB_FULL_RECENT_DAYS, 1))
                boost = min(gap * PB_WEIGHT * recency, PB_BOOST_CAP)
                if boost >= 0.1:
                    vdot_val = round(vdot_val + boost, 1)
                    age_txt = f"（{age} 天前）" if age > 0 else ""
                    evidence.append({
                        "source": "year_best",
                        "vdot": vdot_val,
                        "detail": f"近一年最佳 {pb['distance']} "
                                  f"{_fmt_time(pb['best_seconds'])}{age_txt}等效 "
                                  f"VDOT {pb['vdot']} 高于当前估计 → 上调 "
                                  f"+{boost:.1f}（PB 加成，按 {int(recency * 100)}% 计权）",
                    })
    # 上限钳制：能力预估不高于「已跑出的最佳比赛成绩等效 VDOT + 2」——
    # 手表 VO2max 读数偏高时（如 63 vs 比赛等效 48.9）不能把课表配速
    # 拉到无法完成的水平；下限不加钳制（状态差时按保守值训练）
    if race and vdot_val:
        vdot_val = round(min(vdot_val, race["vdot"] + 2.0), 1)
    result = {
        "vdot": vdot_val,
        "predictions": vd.equivalent_times(vdot_val) if vdot_val else None,
        "zones": vd.intensity_zones(vdot_val) if vdot_val else None,
        "evidence": evidence,
        "max_hr": round(max_hr) if max_hr else None,
        "as_of": (as_of or date.today()).isoformat(),
    }
    return result


def _days_old(value, as_of: date | None) -> int:
    """记录日期（date/iso 字符串）距 as_of 的天数；无法解析视为 0。"""
    day = None
    if isinstance(value, str):
        try:
            day = date.fromisoformat(value[:10])
        except ValueError:
            day = None
    if day is None:
        return 0
    return max(((as_of or date.today()) - day).days, 0)


def _fmt_time(s: float) -> str:
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _fmt_pace(s: float) -> str:
    m = int(s // 60)
    sec = int(round(s % 60))
    if sec >= 60:
        m += 1
        sec -= 60
    return f"{m}:{sec:02d}"
