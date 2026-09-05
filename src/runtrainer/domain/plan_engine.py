"""周期化课表生成器（确定性规则引擎，不依赖 AI）。

阶段比例：base 35% / early 20% / transition 18% / final 15% / taper 12%（最大余数法取整）
周跑量：每 3 周 +10%（封顶峰值），第 4 周 down week -20%；taper 按 80/60/40%·峰值递减
质量日：Q1 周三 / Q2 周六，间隔 ≥2 天，前后必为 E/休息；LR 每周固定日（默认周日）
容量上限：I ≤8% 周量且 ≤10km；R ≤5% 且 ≤8km；T ≤50min；M ≤32km；LR ≤30% 周量
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from ..config import ENGINE_VERSION
from ..utils import jsonutil
from . import vdot as vd
from .workout_catalog import (
    DBL_EASY, E_30, E_40, LR_MENU, LRM_MENU, Q1_BASE, Q1_EARLY, Q1_FINAL,
    Q1_TAPER, Q1_TRANSITION, Q2_BASE, Q2_EARLY, Q2_FINAL, Q2_TAPER,
    Q2_TRANSITION, REC_30, REC_35, STRENGTH, SUBT_AM, SUBT_PM, TUNEUP,
    Template, build_segments, distance_class, lr_template, session_stats,
)

PHASE_ORDER = ("base", "early", "transition", "final", "taper")
PHASE_RATIOS = {"base": 0.35, "early": 0.20, "transition": 0.18, "final": 0.15, "taper": 0.12}
DISTANCES = (5000, 10000, 21097, 42195)
MIN_WEEKS = {5000: 8, 10000: 8, 21097: 12, 42195: 12}
PEAK_CAP = {5000: 60.0, 10000: 75.0, 21097: 85.0, 42195: 110.0}
TUNEUP_KM = {"5K": 3.0, "10K": 4.0, "HM": 5.0, "FM": 5.0}

Q1_MENU = {"base": Q1_BASE, "early": Q1_EARLY, "transition": Q1_TRANSITION,
           "final": Q1_FINAL, "taper": Q1_TAPER}
Q2_MENU = {"base": Q2_BASE, "early": Q2_EARLY, "transition": Q2_TRANSITION,
           "final": Q2_FINAL, "taper": Q2_TAPER}


def pro_extra_km(vdot_val: float) -> float:
    """职业双练模式每周固定叠加跑量：7 天 × 30 分钟放松晚跑的量（展示用）。"""
    return 7 * session_stats(DBL_EASY, vdot_val)["total_km"]


@dataclass
class PlanSpec:
    goal_distance_m: int
    race_date: date
    vdot: float
    base_weekly_km: float
    start_date: date | None = None
    weeks: int | None = None
    run_days: int = 5
    long_run_weekday: int = 6          # 0=周一 … 6=周日
    goal_name: str | None = None
    target_seconds: int | None = None
    start_phase: str | None = None     # None=完整周期；否则从该时期向后生成（截断前面时期）
    double_days: int = 0               # 每周一天两练天数（0–2）
    double_mode: str = "auto"          # threshold=双阈值拆分 / easy=强度后放松跑 / auto=按阶段自动
    strength_days: int = 0             # 每周力量课次数（0–2，穿插在轻松填充日）
    pro_mode: bool = False             # 职业双练模式：休息日轻松跑单练，其余每天两练


@dataclass
class WorkoutDraft:
    date: date
    week_index: int
    phase: str
    kind: str
    title: str
    description: str
    pace_zone: str | None
    distance_km: float | None
    duration_min: float | None
    pace_slow_s_km: float | None
    pace_fast_s_km: float | None
    segments: list = field(default_factory=list)
    is_quality: bool = False
    hard_km: float = 0.0
    slot: int = 1                       # 一天两练时段：1=当日第一练，2=第二练

    def to_row(self) -> dict:
        return {
            "date": self.date.isoformat(), "slot": self.slot,
            "week_index": self.week_index,
            "phase": self.phase, "kind": self.kind, "title": self.title,
            "description": self.description, "distance_km": self.distance_km,
            "duration_min": self.duration_min, "pace_zone": self.pace_zone,
            "pace_slow_s_km": self.pace_slow_s_km, "pace_fast_s_km": self.pace_fast_s_km,
            "target_hr_zone": None, "source": "engine",
            "segments_json": jsonutil.dumps(self.segments) if self.segments else None,
        }


@dataclass
class PlanResult:
    start_date: date
    race_date: date
    total_weeks: int
    phase_weeks: dict
    vdot: float
    base_weekly_km: float
    peak_weekly_km: float
    weekly_km: list
    workouts: list
    warnings: list
    start_phase: str | None = None


def allocate_phases(weeks: int, start_phase: str | None = None) -> dict:
    """按比例分配阶段周数（最大余数法），保证总量 == weeks 且每阶段 ≥1。

    start_phase：只分配该时期及之后的阶段（比例重新归一），之前阶段为 0
    ——「从所选时期向后制定，截断冗长课表」。完整周期时各阶段 ≥1。
    """
    if start_phase is not None and start_phase not in PHASE_ORDER:
        raise ValueError(f"未知训练时期：{start_phase}")
    if weeks < 5 and start_phase is None:
        raise ValueError(f"周数过少（{weeks}），至少需要 5 周")
    active = PHASE_ORDER if start_phase is None else PHASE_ORDER[PHASE_ORDER.index(start_phase):]
    if start_phase is None:
        ratios = dict(PHASE_RATIOS)
    else:
        total = sum(PHASE_RATIOS[p] for p in active)
        ratios = {p: PHASE_RATIOS[p] / total for p in active}
    raw = {p: weeks * ratios.get(p, 0.0) for p in PHASE_ORDER}
    alloc = {p: int(raw[p]) for p in PHASE_ORDER}
    remaining = weeks - sum(alloc.values())
    # 0 周但属于活跃时期的阶段保底 1 周（截断起点之前的时期保持 0）
    for p in PHASE_ORDER:
        if p in active and alloc[p] == 0 and remaining > 0:
            alloc[p] = 1
            remaining -= 1
    fracs = sorted(((raw[p] - alloc[p], -i) for i, p in enumerate(PHASE_ORDER) if p in active),
                   reverse=True)
    idx = 0
    while remaining > 0:
        _, neg_i = fracs[idx % len(fracs)]
        p = PHASE_ORDER[-neg_i]
        alloc[p] += 1
        remaining -= 1
        idx += 1
    return alloc


def weekly_km_targets(weeks: int, base: float, peak: float, taper_weeks: int) -> list[float]:
    """3+1 递增：3 周 +10%（封顶），第 4 周 -20%；taper 末段 80/60/40%·峰值。"""
    out = []
    level = min(base, peak)
    for w in range(weeks):
        if w >= weeks - taper_weeks:
            back = weeks - 1 - w
            out.append(round(peak * {0: 0.4, 1: 0.6}.get(back, 0.8), 1))
        elif w % 4 == 3:
            out.append(round(level * 0.8, 1))
            level = min(level * 1.1, peak)
        else:
            out.append(round(min(level, peak), 1))
    return out


def generate_plan(spec: PlanSpec) -> PlanResult:
    warnings: list[str] = []
    dist = spec.goal_distance_m
    if dist not in DISTANCES:
        raise ValueError(f"目标距离 {dist}m 不在支持范围内（5K/10K/半马/全马）")

    vdot_val = spec.vdot
    if not 30 <= vdot_val <= 85:
        vdot_val = min(85.0, max(30.0, vdot_val))
        warnings.append("VDOT 超出 30–85 合理范围，已钳制")

    run_days = min(7, max(4, spec.run_days))
    if run_days != spec.run_days:
        warnings.append(f"每周训练天数已钳制到 {run_days} 天（4–7）")

    double_days = min(2, max(0, spec.double_days))
    if double_days != spec.double_days:
        warnings.append(f"一周两练天数已钳制到 {double_days}（0–2）")
    double_mode = spec.double_mode if spec.double_mode in ("threshold", "easy", "auto") else "auto"
    if double_mode != spec.double_mode:
        warnings.append("一天两练形式未知，已回退为按阶段自动")
    strength_days = min(2, max(0, spec.strength_days))
    if strength_days != spec.strength_days:
        warnings.append(f"每周力量课次数已钳制到 {strength_days}（0–2）")

    lr_wd = spec.long_run_weekday if 0 <= spec.long_run_weekday <= 6 else 6
    if lr_wd != spec.long_run_weekday:
        warnings.append("长距离日不在有效范围内，已回退到周日")

    min_w = MIN_WEEKS[dist]
    if spec.start_date and spec.start_date < spec.race_date:
        days = (spec.race_date - spec.start_date).days
        if days < 7:
            raise ValueError("比赛日期须在开始日期至少 1 周之后")
        weeks = max(1, math.ceil(days / 7))
    else:
        weeks = spec.weeks or min_w
    if weeks < min_w:
        warnings.append(f"备赛周期 {weeks} 周少于该距离建议的 {min_w} 周，计划强度会较为紧凑")

    start = spec.race_date - timedelta(days=weeks * 7 - 1)   # 末周最后一天 = 比赛日
    if spec.race_date.weekday() != 6:
        warnings.append(
            "比赛日期不是周日，课表周框架随比赛日平移：长距离安排在比赛日前一天，"
            "比赛周前两天安排休息")

    cap = PEAK_CAP[dist]
    peak = min(cap, spec.base_weekly_km * 1.5)
    base = spec.base_weekly_km
    if spec.base_weekly_km * 1.5 > cap:
        warnings.append(f"峰值跑量受距离上限 {cap:.0f}km 约束（1.5 倍基础为 {spec.base_weekly_km * 1.5:.0f}km）")
    if base > peak:
        base = peak
        warnings.append(f"基础周跑量超过峰值上限，已钳制为 {peak:.0f}km")

    phase_weeks = allocate_phases(weeks, spec.start_phase)
    offsets: dict[str, int] = {}
    acc = 0
    for p in PHASE_ORDER:
        offsets[p] = acc
        acc += phase_weeks[p]
    taper_weeks = phase_weeks["taper"]
    targets = weekly_km_targets(weeks, base, peak, taper_weeks)

    # ---- 职业双练模式（效仿职业运动员）：休息日轻松跑单练，其余每天两练 ----
    # 第二练/休息日轻松跑带来固定跑量增量（7 天 × ~30 分钟轻松跑）。课表内配速、
    # 容量护栏与填充跑量仍按原目标曲线约束（第二练不计入），周量展示在返回处上浮。
    pro_extra = pro_extra_km(vdot_val) if spec.pro_mode else 0.0

    cls = distance_class(dist)
    paces = vd.pace_table(vdot_val)
    workouts: list[WorkoutDraft] = []

    for w in range(weeks):
        phase = next(p for p in reversed(PHASE_ORDER) if w >= offsets[p])
        pi = w - offsets[phase]
        is_taper = w >= weeks - taper_weeks
        is_race_week = w == weeks - 1
        target = targets[w]
        week_start = start + timedelta(days=7 * w)

        # ---- 长距离（30% 上限按含热身冷身的总量约束）----
        lr_tpl: Template | None = None
        lr_km = m_block = 0.0
        if not is_race_week:
            lr_tpl = lr_template(phase, pi, dist)
            wucd = session_stats(lr_tpl, vdot_val, lr_km=0.0, m_block_km=0.0)["total_km"]
            cap_lr = max(0.0, 0.30 * target - wucd)
            menu = LR_MENU[cls]
            lr_km = min(menu[pi % len(menu)], cap_lr)
            if is_taper:
                lr_km *= 0.6
            if lr_tpl.lr_m:
                mlr, mm = LRM_MENU[cls][pi % len(LRM_MENU[cls])]
                lr_km = min(mlr, cap_lr)
                m_block = min(mm, lr_km * 0.5, 32.0)
                if m_block < 5:      # M 段太短则退化为普通长距离
                    m_block = 0.0
                    lr_tpl = lr_template("base", 0, dist)

        # ---- 质量课（按容量上限钳制组数）----
        def _pick(menu, phase_key, pidx):
            tpl = menu[phase_key][pidx % len(menu[phase_key])]
            if tpl.kind == "I":
                cap_km = min(0.08 * target, 10.0)
            elif tpl.kind == "R":
                cap_km = min(0.05 * target, 8.0)
            else:
                return tpl
            n, m = tpl.reps[0]
            max_n = max(1, int(cap_km * 1000 / m))
            if n > max_n:
                return replace(tpl, reps=((max_n, m),))
            return tpl

        q1_tpl = q2_tpl = None
        if is_race_week:
            q1_tpl = REC_35
        elif is_taper:
            q1_tpl = _pick(Q1_MENU, "taper", pi)
            q2_tpl = Q2_MENU["taper"][0]          # 减量期 Q2 无质量课
        else:
            q1_tpl = _pick(Q1_MENU, phase, pi)
            q2_tpl = _pick(Q2_MENU, phase, pi)
            if lr_tpl.lr_m:            # LR 含 M 段 → 周日已强度，周六改轻松
                q2_tpl = E_40
        # 最终强度期倒数第 3 周设测试赛（Q2 位）；测试周 LR 不叠加 M 段
        tuneup_km = 0.0
        if phase == "final" and phase_weeks["final"] >= 3 and pi == phase_weeks["final"] - 3:
            q2_tpl = TUNEUP
            tuneup_km = TUNEUP_KM[cls]
            if lr_tpl.lr_m:
                lr_tpl = lr_template("base", 0, dist)
                m_block = 0.0

        # ---- 休息日 ----
        rest_days = {0}
        if run_days <= 5:
            rest_days.add(4)
        if run_days <= 4:
            rest_days.add(3)
        if is_race_week:
            rest_days |= {4, 5}
        # 职业双练模式：休息日改为轻松跑单练（减量/比赛周除外）
        rest_easy_wds: set[int] = set()
        if spec.pro_mode and not is_race_week and not is_taper:
            rest_easy_wds = set(rest_days)
            rest_days = set()

        # ---- 质量日槽位（Q1 周三 / Q2 周六，与 LR 冲突时后移）----
        q1_wd, q2_wd = 2, 5
        if lr_wd == q1_wd:
            q1_wd = 3
        if lr_wd == q2_wd:
            q2_wd = 4
        if q2_wd == q1_wd:
            q2_wd = 4

        # ---- 一天两练（slot=2）----
        # 职业双练模式（效仿职业运动员）：休息日轻松跑单练，其余所有训练日两练
        # ——T 日按挪威模式拆上（3×8' 亚阈）+下（5×5' 亚阈），其他日主课 + 30 分钟
        # 放松晚跑；down 恢复周保留二练频率但降级为放松晚跑；减量/比赛周不排。
        # 普通模式：每周 double_days 天二练优先挑 T 日；减量/比赛/down 周不排。
        def _pair(tpl: Template | None) -> tuple[Template | None, Template | None]:
            if tpl is None:
                return None, None
            if tpl.kind == "T" and double_mode in ("threshold", "auto"):
                return SUBT_AM, SUBT_PM
            if tpl.is_quality:
                return tpl, DBL_EASY
            return tpl, None

        q1_slot2 = q2_slot2 = None
        extra_slot2: dict[int, Template] = {}   # 职业模式：质量日以外日子的第二练
        if spec.pro_mode:
            if not is_race_week and not is_taper:
                split_ok = double_mode in ("threshold", "auto") and w % 4 != 3
                for wd in range(7):
                    if wd in rest_easy_wds:
                        continue
                    if wd == q1_wd:
                        if q1_tpl.kind == "T" and split_ok:
                            q1_tpl, q1_slot2 = SUBT_AM, SUBT_PM
                        else:
                            q1_slot2 = DBL_EASY
                    elif wd == q2_wd:
                        if q2_tpl.kind == "T" and split_ok:
                            q2_tpl, q2_slot2 = SUBT_AM, SUBT_PM
                        else:
                            q2_slot2 = DBL_EASY
                    else:
                        extra_slot2[wd] = DBL_EASY
        elif double_days and not is_race_week and not is_taper and w % 4 != 3:
            if double_days >= 2:
                q2_tpl, q2_slot2 = _pair(q2_tpl)
                q1_tpl, q1_slot2 = _pair(q1_tpl)
            else:
                # 单日二练优先挑 T 日做双阈值拆分（挪威模式）；无 T 日则配 Q2 强度日
                if q2_tpl is not None and q2_tpl.kind == "T":
                    q2_tpl, q2_slot2 = _pair(q2_tpl)
                elif q1_tpl is not None and q1_tpl.kind == "T":
                    q1_tpl, q1_slot2 = _pair(q1_tpl)
                elif q2_tpl is not None:
                    q2_tpl, q2_slot2 = _pair(q2_tpl)
                elif q1_tpl is not None:
                    q1_tpl, q1_slot2 = _pair(q1_tpl)

        # ---- 质量课/长距离占用 ----
        def _stats(tpl, **kw):
            return session_stats(tpl, vdot_val, **kw)

        lr_stats = _stats(lr_tpl, lr_km=lr_km, m_block_km=m_block) if lr_tpl else None
        q1_stats = _stats(q1_tpl) if q1_tpl else None
        q1s2_stats = _stats(q1_slot2) if q1_slot2 else None
        q2_stats = _stats(q2_tpl, tuneup_km=tuneup_km) if q2_tpl else None
        q2s2_stats = _stats(q2_slot2) if q2_slot2 else None
        used = (lr_stats["total_km"] if lr_stats else 0) + \
               (q1_stats["total_km"] if q1_stats else 0) + \
               (q2_stats["total_km"] if q2_stats else 0)
        if not spec.pro_mode:
            # 普通模式的二练计入周量平衡；职业模式二练/休息日轻松跑是叠加跑量，
            # 不计入 used（填充按原目标曲线分配，避免质量日二练吃满预算挤掉填充日）
            used += (q1s2_stats["total_km"] if q1s2_stats else 0) + \
                    (q2s2_stats["total_km"] if q2s2_stats else 0)

        # ---- 力量课（穿插在轻松填充日，不占跑量；比赛周/减量周不排）----
        # 保留至少 1 个纯跑填充日兜住周跑量；候选日即后续的填充日
        strength_wds: list[int] = []
        if strength_days and not is_race_week and not is_taper:
            cands = [wd for wd in range(7)
                     if wd not in rest_days and wd not in rest_easy_wds
                     and wd not in (q1_wd, q2_wd, lr_wd)]
            strength_wds = cands[:min(strength_days, max(0, len(cands) - 1))]

        # ---- 填充日（恢复跑 + 动态轻松跑分摊剩余跑量）----
        filler_wds = [wd for wd in range(7)
                      if wd not in rest_days and wd not in rest_easy_wds
                      and wd not in (q1_wd, q2_wd, lr_wd)
                      and wd not in strength_wds]
        down_week = (not is_taper and not is_race_week and w % 4 == 3)
        # down 周仍按 -20% 目标量精确填充（用轻松跑）；比赛周填充用恢复跑
        rec_wds = [wd for wd in filler_wds if is_race_week]
        dyn_wds = [wd for wd in filler_wds if wd not in rec_wds]
        rec_used = sum(_stats(REC_30)["total_km"] for _ in rec_wds)
        # 剩余跑量先按动态天数均摊；人均 <3km 的日子改成恢复跑 30 分钟
        # （避免 2km 小碎跑），但只改到人均回到 ≥3km 为止——REC_30 固定
        # 5km+，无脑全改会突破周量目标（一天两练吃掉跑量后尤其明显）。
        rec_km = _stats(REC_30)["total_km"]
        remaining = target - used - rec_used
        k = 0
        while k < len(dyn_wds) and remaining > 0 \
                and remaining / (len(dyn_wds) - k) < 3.0:
            k += 1
            remaining -= rec_km
        if remaining < 0 and k > 0:      # 改过头会超目标，回退一步
            k -= 1
            remaining += rec_km
        fillers: dict[int, Template | None] = {wd: REC_30 for wd in rec_wds}
        for wd in dyn_wds[:k]:
            fillers[wd] = REC_30
        for wd in dyn_wds[k:]:
            fillers[wd] = None        # 动态轻松跑，距离按剩余跑量均摊
        dyn_wds = dyn_wds[k:]
        dyn_each = remaining / len(dyn_wds) if dyn_wds else 0.0

        # ---- 组装一周 ----
        def _append_extra(wd_: int, slot_: int = 2, title: str | None = None) -> None:
            """职业模式：当日第二练（质量日以外的日子，或主课被跑量挤掉时降为单练）。"""
            t2 = extra_slot2.get(wd_)
            if not t2:
                return
            workouts.append(_mk_draft(
                d, w, phase, t2, title or t2.name, t2.description, paces,
                _stats(t2), build_segments(t2), is_quality=False, slot=slot_))

        for wd in range(7):
            d = week_start + timedelta(days=wd)
            if d > spec.race_date:
                continue
            if is_race_week and wd == 6:
                label = spec.goal_name or ({5000: "5K", 10000: "10K",
                                            21097: "半马", 42195: "全马"}[dist])
                workouts.append(WorkoutDraft(
                    date=d, week_index=w, phase=phase, kind="RACE", title=f"比赛日 · {label}",
                    description="比赛！前 3 公里压住速度，按目标配速执行，后半程稳中求进。",
                    pace_zone=None, distance_km=round(dist / 1000, 1),
                    duration_min=round(spec.target_seconds / 60, 1) if spec.target_seconds else None,
                    pace_slow_s_km=None, pace_fast_s_km=None, is_quality=True,
                    hard_km=round(dist / 1000, 1)))
                continue
            if wd in rest_days:
                continue
            if wd in rest_easy_wds:
                # 职业模式休息日 → 轻松跑单练（30 分钟，不排第二练）
                stats = _stats(E_30)
                workouts.append(_mk_draft(
                    d, w, phase, E_30, "轻松跑 30 分钟（原休息日）", E_30.description,
                    paces, stats, build_segments(E_30), is_quality=False))
                continue
            if wd == lr_wd and lr_tpl:
                title = f"长距离 {lr_km:.0f}km" + (f"（含 {m_block:.0f}km M 配速）" if m_block else "")
                workouts.append(_mk_draft(
                    d, w, phase, lr_tpl, title, lr_tpl.description, paces, lr_stats,
                    build_segments(lr_tpl, lr_km=lr_km, m_block_km=m_block),
                    is_quality=lr_tpl.lr_m))
                _append_extra(wd)
            elif wd == q1_wd and q1_tpl:
                workouts.append(_mk_draft(
                    d, w, phase, q1_tpl, q1_tpl.name, q1_tpl.description, paces, q1_stats,
                    build_segments(q1_tpl, tuneup_km=tuneup_km),
                    is_quality=q1_tpl.is_quality, slot=1))
                if q1_slot2:
                    workouts.append(_mk_draft(
                        d, w, phase, q1_slot2, q1_slot2.name, q1_slot2.description, paces,
                        q1s2_stats, build_segments(q1_slot2),
                        is_quality=q1_slot2.is_quality, slot=2))
            elif wd == q2_wd and q2_tpl:
                workouts.append(_mk_draft(
                    d, w, phase, q2_tpl, q2_tpl.name, q2_tpl.description, paces, q2_stats,
                    build_segments(q2_tpl, tuneup_km=tuneup_km),
                    is_quality=q2_tpl.is_quality, slot=1))
                if q2_slot2:
                    workouts.append(_mk_draft(
                        d, w, phase, q2_slot2, q2_slot2.name, q2_slot2.description, paces,
                        q2s2_stats, build_segments(q2_slot2),
                        is_quality=q2_slot2.is_quality, slot=2))
            elif wd in strength_wds:
                workouts.append(WorkoutDraft(
                    date=d, week_index=w, phase=phase, kind=STRENGTH.kind,
                    title=STRENGTH.name, description=STRENGTH.description,
                    pace_zone=None, distance_km=None, duration_min=40.0,
                    pace_slow_s_km=None, pace_fast_s_km=None,
                    segments=build_segments(STRENGTH), is_quality=False, slot=1))
                _append_extra(wd)
            else:
                t = fillers[wd]
                if t is None:
                    if dyn_each <= 0:
                        # 质量课/二练已吃满周量目标：职业模式下降为单练放松跑，普通模式放假
                        _append_extra(wd, slot_=1, title="轻松跑 30 分钟")
                        continue
                    km = max(3.0, dyn_each)
                    minutes = km * paces["E"]["slow_s_km"] / 60.0
                    workouts.append(WorkoutDraft(
                        date=d, week_index=w, phase=phase, kind="E",
                        title=f"轻松跑 {minutes:.0f} 分钟",
                        description="轻松有氧，用于凑周跑量，按体感放松完成。",
                        pace_zone="E", distance_km=round(km, 1),
                        duration_min=round(minutes, 0),
                        pace_slow_s_km=paces["E"]["slow_s_km"],
                        pace_fast_s_km=paces["E"]["fast_s_km"],
                        segments=build_segments(E_30, easy_min=round(minutes)), is_quality=False))
                    _append_extra(wd)
                else:
                    stats = _stats(t)
                    workouts.append(_mk_draft(
                        d, w, phase, t, t.name, t.description, paces, stats,
                        build_segments(t), is_quality=False))
                    _append_extra(wd)

    return PlanResult(
        start_date=start, race_date=spec.race_date, total_weeks=weeks,
        phase_weeks=phase_weeks, vdot=vdot_val,
        base_weekly_km=round(base + pro_extra, 1),
        peak_weekly_km=round(peak + pro_extra, 1),
        weekly_km=[round(t + pro_extra, 1) if w < weeks - taper_weeks else t
                   for w, t in enumerate(targets)],
        workouts=workouts, warnings=warnings, start_phase=spec.start_phase,
    )


def _mk_draft(d: date, w: int, phase: str, tpl: Template, title: str, description: str,
              paces: dict, stats: dict, segments: list, is_quality: bool,
              slot: int = 1) -> WorkoutDraft:
    slow = fast = None
    if tpl.pace_zone:
        if tpl.pace_zone == "E":
            slow, fast = paces["E"]["slow_s_km"], paces["E"]["fast_s_km"]
        else:
            slow = fast = paces[tpl.pace_zone]
    return WorkoutDraft(
        date=d, week_index=w, phase=phase, kind=tpl.kind, title=title,
        description=description, pace_zone=tpl.pace_zone,
        distance_km=round(stats["total_km"], 1),
        duration_min=round(stats["duration_min"], 0) or None,
        pace_slow_s_km=slow, pace_fast_s_km=fast, segments=segments,
        is_quality=is_quality, hard_km=round(stats["hard_km"], 2), slot=slot)
