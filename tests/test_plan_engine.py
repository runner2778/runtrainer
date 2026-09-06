"""M3：课表生成器不变量测试（无相邻强度日、周增幅≤10%、down week、taper、容量上限）。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from runtrainer.domain.plan_engine import (
    MIN_WEEKS, PEAK_CAP, PlanSpec, allocate_phases, generate_plan,
)

HARD_KINDS = {"T", "I", "R", "M", "TUNEUP", "RACE"}


def _is_hard(w) -> bool:
    return w.kind in HARD_KINDS or (w.kind == "LR" and w.pace_zone == "M")


def _spec(**kw) -> PlanSpec:
    defaults = dict(goal_distance_m=21097, race_date=date(2026, 12, 20), vdot=45,
                    base_weekly_km=40, start_date=date(2026, 9, 28), run_days=5,
                    long_run_weekday=6, target_seconds=1 * 3600 + 45 * 60)
    defaults.update(kw)
    return PlanSpec(**defaults)


def _weeks_of(res):
    out = {}
    for w in res.workouts:
        out.setdefault(w.week_index, []).append(w)
    return out


# ---------- 阶段分配 ----------

@pytest.mark.parametrize("weeks", [8, 10, 12, 16, 18, 24])
def test_allocate_phases(weeks):
    alloc = allocate_phases(weeks)
    assert sum(alloc.values()) == weeks
    assert list(alloc) == ["base", "early", "transition", "final", "taper"]
    assert all(v >= 1 for v in alloc.values())
    # 比例大致符合 35/20/18/15/12（误差 ≤1 周）
    for p, ratio in {"base": 0.35, "early": 0.20, "transition": 0.18,
                     "final": 0.15, "taper": 0.12}.items():
        assert abs(alloc[p] - weeks * ratio) <= 1.5


# ---------- 时期截断（从所选时期向后制定） ----------

def test_allocate_phases_truncated():
    alloc = allocate_phases(12, "transition")
    assert sum(alloc.values()) == 12
    assert alloc["base"] == 0 and alloc["early"] == 0
    assert all(alloc[p] >= 1 for p in ("transition", "final", "taper"))


def test_allocate_phases_taper_only():
    alloc = allocate_phases(3, "taper")
    assert alloc == {"base": 0, "early": 0, "transition": 0, "final": 0, "taper": 3}


def test_allocate_phases_invalid_raises():
    with pytest.raises(ValueError):
        allocate_phases(12, "bogus")


def test_generate_plan_from_transition_skips_earlier_phases():
    res = generate_plan(_spec(start_phase="transition", start_date=None, weeks=12))
    phases = {w.phase for w in res.workouts}
    assert "base" not in phases and "early" not in phases
    assert res.workouts[0].phase == "transition"
    assert res.phase_weeks["base"] == 0 and res.phase_weeks["early"] == 0
    assert res.start_phase == "transition"
    # 截断后的课表仍满足核心不变量：无相邻强度日
    by_date = {w.date: w for w in res.workouts}
    for w in res.workouts:
        if not _is_hard(w):
            continue
        for delta in (1, 2):
            nxt = by_date.get(w.date + timedelta(days=delta))
            assert nxt is None or not _is_hard(nxt), \
                f"{w.date} {w.kind} 后 {delta} 天仍有强度课 {nxt and nxt.kind}"


def test_generate_plan_from_taper_all_taper():
    res = generate_plan(_spec(start_phase="taper", start_date=None, weeks=4))
    assert {w.phase for w in res.workouts} == {"taper"}
    assert res.phase_weeks["taper"] == 4


def test_generate_plan_full_cycle_unchanged():
    res = generate_plan(_spec(start_date=None, weeks=12))
    assert res.start_phase is None
    assert res.phase_weeks["base"] >= 1


# ---------- 核心不变量 ----------

def test_no_adjacent_hard_days():
    res = generate_plan(_spec())
    by_date = {w.date: w for w in res.workouts}
    for w in res.workouts:
        if not _is_hard(w):
            continue
        for delta in (1, 2):
            nxt = by_date.get(w.date + timedelta(days=delta))
            assert nxt is None or not _is_hard(nxt), \
                f"{w.date} {w.kind} 后 {delta} 天仍有强度课 {nxt and nxt.kind}"


def test_hard_days_gap_at_least_3():
    res = generate_plan(_spec())
    hard_dates = sorted(w.date for w in res.workouts if _is_hard(w))
    for a, b in zip(hard_dates, hard_dates[1:]):
        assert (b - a).days >= 3, f"强度课 {a} 与 {b} 间隔不足 2 天"


def test_weekly_ramp_and_down_week():
    res = generate_plan(_spec())
    ts = res.weekly_km
    taper_start = res.total_weeks - res.phase_weeks["taper"]
    for w in range(taper_start):
        if w % 4 == 3:
            assert abs(ts[w] - 0.8 * ts[w - 1]) <= 0.15, f"第 {w} 周 down week 应为 -20%"
        elif w > 0 and (w - 1) % 4 != 3:
            assert abs(ts[w] - ts[w - 1]) <= 0.05, f"第 {w} 周块内应持平"
    # 块间增幅 ≤10%
    for w in range(0, taper_start - 4, 4):
        assert ts[w + 4] <= 1.105 * ts[w] + 0.1, f"第 {w + 4} 周增幅超 10%"
    # taper 递减且末周 = 40% 峰值
    for w in range(taper_start, res.total_weeks - 1):
        assert ts[w + 1] < ts[w]
    assert abs(ts[-1] - 0.4 * res.peak_weekly_km) <= 0.15


def test_taper_all_easy_last_week():
    res = generate_plan(_spec())
    last_week = [w for w in res.workouts if w.week_index == res.total_weeks - 1]
    race = [w for w in last_week if w.kind == "RACE"]
    assert len(race) == 1 and race[0].date == res.race_date
    others = [w for w in last_week if w.kind != "RACE"]
    assert others and all(w.kind in ("E", "RECOVERY") for w in others)
    assert all(not _is_hard(w) for w in others)


def test_last_3_days_before_race_no_hard():
    res = generate_plan(_spec())
    for w in res.workouts:
        if res.race_date - timedelta(days=3) <= w.date < res.race_date:
            assert w.kind in ("E", "RECOVERY"), f"赛前 {w.date} 仍有 {w.kind}"


def test_capacity_caps():
    res = generate_plan(_spec())
    weeks = _weeks_of(res)
    for wi, ws in weeks.items():
        target = res.weekly_km[wi]
        for w in ws:
            if w.kind == "I":
                assert w.hard_km <= min(0.08 * target, 10.0) + 0.01
            elif w.kind == "R":
                assert w.hard_km <= min(0.05 * target, 8.0) + 0.01
            elif w.kind == "LR":
                assert w.distance_km <= 0.30 * target + 0.5, f"LR 超周量 30%: {w}"
            if w.pace_zone == "M":
                m_km = next((s["distance_km"] for s in w.segments if s.get("zone") == "M"), 0)
                assert m_km <= 32
            if w.kind == "T":
                tempo_min = sum(s.get("duration_min", 0) * s.get("reps", 1)
                                for s in w.segments if s["type"] == "tempo")
                assert tempo_min <= 50


def test_quality_days_on_wed_sat():
    res = generate_plan(_spec())
    weeks = _weeks_of(res)
    for wi, ws in weeks.items():
        if wi >= res.total_weeks - 1:      # 比赛周无质量课
            continue
        hard = [w for w in ws if _is_hard(w)]
        if not hard:
            continue
        for w in hard:
            assert w.date.weekday() in (2, 5, 6), f"强度课落在 {w.date.weekday()}"


def test_one_lr_per_week_and_rest_days():
    res = generate_plan(_spec())
    weeks = _weeks_of(res)
    for wi, ws in weeks.items():
        if wi == res.total_weeks - 1:      # 比赛周无 LR
            assert not any(w.kind == "LR" for w in ws)
            continue
        lrs = [w for w in ws if w.kind == "LR"]
        assert len(lrs) == 1 and lrs[0].date.weekday() == 6
    # 每周训练天数 = run_days（完整非比赛周）
    full = [ws for wi, ws in weeks.items() if wi < res.total_weeks - 1]
    assert all(len(ws) == 5 for ws in full)


def test_weekly_distance_close_to_target():
    res = generate_plan(_spec())
    weeks = _weeks_of(res)
    for wi, ws in weeks.items():
        if wi >= res.total_weeks - res.phase_weeks["taper"]:
            continue
        total = sum(w.distance_km or 0 for w in ws)
        target = res.weekly_km[wi]
        assert 0.85 * target <= total <= 1.15 * target, \
            f"第 {wi} 周实际 {total:.1f}km 偏离目标 {target}km"


def test_deterministic():
    a = generate_plan(_spec())
    b = generate_plan(_spec())
    assert [(w.date, w.kind, w.title, w.distance_km) for w in a.workouts] == \
           [(w.date, w.kind, w.title, w.distance_km) for w in b.workouts]
    assert a.weekly_km == b.weekly_km


def test_peak_cap_respected():
    res = generate_plan(_spec(base_weekly_km=100))
    assert res.peak_weekly_km == PEAK_CAP[21097]
    assert max(res.weekly_km) <= res.peak_weekly_km + 0.01
    assert any("上限" in x for x in res.warnings)


def test_short_prep_warns():
    res = generate_plan(_spec(start_date=date(2026, 11, 20)))   # 4 周
    assert res.total_weeks < MIN_WEEKS[21097]
    assert any("建议" in x for x in res.warnings)


def test_insufficient_gap_raises():
    with pytest.raises(ValueError):
        generate_plan(_spec(start_date=date(2026, 12, 15)))


# ---------- 距离差异 ----------

def test_5k_plan_has_interval_and_reps():
    res = generate_plan(_spec(goal_distance_m=5000, race_date=date(2026, 11, 15),
                              base_weekly_km=30, target_seconds=22 * 60))
    kinds = {w.kind for w in res.workouts}
    assert "I" in kinds and "R" in kinds


def test_fm_plan_has_lr_with_m_block():
    res = generate_plan(_spec(goal_distance_m=42195, race_date=date(2027, 3, 21),
                              base_weekly_km=50, target_seconds=3 * 3600 + 40 * 60))
    assert any(w.kind == "LR" and w.pace_zone == "M" for w in res.workouts)
    kinds = {w.kind for w in res.workouts}
    assert "T" in kinds


def test_tuneup_present_in_final_phase_when_long_enough():
    res = generate_plan(_spec(weeks=20, start_date=None))   # final 期 3 周才设测试赛
    tuneups = [w for w in res.workouts if w.kind == "TUNEUP"]
    assert tuneups, "16 周计划 final 期应有测试赛"
    assert all(w.phase == "final" for w in tuneups)


def test_vdot_out_of_range_clamped():
    res = generate_plan(_spec(vdot=12))
    assert res.vdot == 30
    assert any("钳制" in x for x in res.warnings)


def test_saturday_race_shifts_frame_with_warning():
    """非周日比赛：周框架随比赛日平移（长距离挪到周六），并给出提示。"""
    res = generate_plan(_spec(race_date=date(2026, 12, 19)))
    assert any("不是周日" in x for x in res.warnings)
    race = [w for w in res.workouts if w.kind == "RACE"]
    assert len(race) == 1 and race[0].date == date(2026, 12, 19)
    weeks = _weeks_of(res)
    for wi, ws in weeks.items():
        if wi == res.total_weeks - 1:
            assert not any(w.kind == "LR" for w in ws)
            continue
        lrs = [w for w in ws if w.kind == "LR"]
        assert len(lrs) == 1 and lrs[0].date.weekday() == 5
    # 比赛前两天（周四/周五）休息，比赛周其余课为轻松课
    rw = weeks[res.total_weeks - 1]
    assert not any(w.date in (date(2026, 12, 17), date(2026, 12, 18)) for w in rw)
    assert all(w.kind in ("E", "RECOVERY") for w in rw if w.kind != "RACE")


# ---------- 一天两练 / 力量课 ----------

def _by_date(res):
    out = {}
    for w in res.workouts:
        out.setdefault(w.date, []).append(w)
    return out


def test_double_days_threshold_split():
    """一周两练 1 天 + 双阈值模式：transition/final 期 T 日拆成上+下两练（slot 1/2）。"""
    res = generate_plan(_spec(double_days=1, double_mode="threshold"))
    pairs = [(d, ws) for d, ws in _by_date(res).items() if len(ws) == 2]
    assert pairs, "应有同一天两练的日子"
    for d, ws in pairs:
        assert [w.slot for w in ws] == [1, 2]
    # 至少一对是双阈值（T 日拆分，T 日只在 transition/final 期）
    assert any(w1.kind == w2.kind == "T" for _d, ws in pairs for w1, w2 in [ws])
    # 双阈值单练时段阈值主体 ≤30 分钟（3×8'=24 / 5×5'=25，全天 ~49 分钟）
    for w in res.workouts:
        if w.kind == "T":
            tempo_min = sum(s["duration_min"] for s in w.segments if s["type"] == "tempo")
            assert tempo_min <= 30


def test_double_days_easy_evening():
    """easy 模式：强度日保持主课，傍晚加 30 分钟放松晚跑（RECOVERY）。"""
    res = generate_plan(_spec(double_days=1, double_mode="easy"))
    pairs = [(d, ws) for d, ws in _by_date(res).items() if len(ws) == 2]
    assert pairs
    assert all(w2.kind == "RECOVERY" and w1.kind in HARD_KINDS
               for _d, ws in pairs for w1, w2 in [ws])


def test_double_days_two_per_week():
    """一周两练 2 天：两个质量日都配二练；taper/比赛周不排二练。"""
    res = generate_plan(_spec(double_days=2))
    by_date = _by_date(res)
    weeks = _weeks_of(res)
    double_weeks = [wi for wi, ws in weeks.items()
                    if any(len(by_date[w.date]) == 2 for w in ws)]
    assert double_weeks
    race_wi = res.total_weeks - 1
    assert race_wi not in double_weeks
    # 每对 slot 合法
    for ws in by_date.values():
        if len(ws) == 2:
            assert [w.slot for w in ws] == [1, 2]


def test_double_days_disabled_no_second_sessions():
    res = generate_plan(_spec(double_days=0))
    assert all(len(ws) == 1 for ws in _by_date(res).values())


def test_strength_sessions_inserted_on_filler_days():
    """力量课穿插在轻松填充日：不占跑量、非强度日、减量/比赛周不排。"""
    res = generate_plan(_spec(strength_days=2))
    strengths = [w for w in res.workouts if w.kind == "STRENGTH"]
    assert strengths
    weeks = _weeks_of(res)
    for w in strengths:
        assert w.distance_km is None and w.duration_min == 40
        assert w.phase != "taper"
        assert not _is_hard(w)
        assert not any(w.date == o.date and o is not w for o in res.workouts)
        day_ws = [o for o in res.workouts if o.date == w.date]
        assert len(day_ws) == 1  # 力量日不再叠跑课（slot 1 独立占用）
    # 比赛周无力量课
    assert not any(w.kind == "STRENGTH" for w in weeks[res.total_weeks - 1])


def test_strength_and_double_days_coexist():
    res = generate_plan(_spec(double_days=1, strength_days=1))
    kinds = {w.kind for w in res.workouts}
    assert "STRENGTH" in kinds
    assert any(len(ws) == 2 for ws in _by_date(res).values())


def test_double_days_weekly_volume_still_near_target():
    """二练的跑量计入周量平衡：周总量仍贴近目标（填充跑让出份额）。"""
    res = generate_plan(_spec(double_days=2, double_mode="threshold"))
    weeks = _weeks_of(res)
    for wi, ws in weeks.items():
        if wi >= res.total_weeks - 2:
            continue
        km = sum(w.distance_km or 0 for w in ws)
        assert km <= res.weekly_km[wi] * 1.05 + 2
        assert km >= res.weekly_km[wi] * 0.8


# ---------- 职业双练模式（pro_mode：休息日轻松跑单练，其余皆双练） ----------

def test_pro_mode_rest_days_become_single_easy_run():
    """职业模式：非减量/比赛周无完全休息日——原休息日改 30 分钟轻松跑单练。"""
    res = generate_plan(_spec(pro_mode=True))
    by_date = _by_date(res)
    weeks = _weeks_of(res)
    taper_wi = res.total_weeks - res.phase_weeks["taper"]
    for wi, ws in weeks.items():
        if wi >= taper_wi:          # 减量周起恢复常规（不检查单练形态）
            continue
        singles = [w for w in ws if len(by_date[w.date]) == 1]
        assert singles, f"week {wi} 应有休息日改成的轻松跑单练"
        for w in singles:
            assert w.kind == "E" and w.slot == 1
            assert "原休息日" in w.title
            assert 25 <= w.duration_min <= 35
            assert w.date.weekday() in (0, 4)   # run_days=5 → 原休息日为周一/周五
        # 其余日子全部两练，slot 合法
        for w in ws:
            if len(by_date[w.date]) == 2:
                assert [o.slot for o in by_date[w.date]] == [1, 2]


def test_pro_mode_all_other_days_double_sessions():
    """职业模式：非休息日（含长距离日）皆两练；T 日挪威双阈值拆分，其余傍晚放松跑。"""
    res = generate_plan(_spec(pro_mode=True))
    by_date = _by_date(res)
    tt = []
    for d, ws in by_date.items():
        if len(ws) != 2:
            continue
        if ws[0].kind == ws[1].kind == "T":
            tt.append(ws)
            for w in ws:      # 双阈值：单练时段亚阈主体 ≤30 分钟（3×8'/5×5'）
                tempo_min = sum(s["duration_min"] for s in w.segments if s["type"] == "tempo")
                assert tempo_min <= 30
        else:
            assert ws[1].kind == "RECOVERY"
    assert tt, "应有 T 日双阈值拆分（transition/final 期）"
    # 长距离日也配傍晚放松跑（效仿职业运动员）
    lrs = [w for w in res.workouts if w.kind == "LR" and w.phase != "taper"]
    assert all(len(by_date[w.date]) == 2 and by_date[w.date][1].kind == "RECOVERY"
               for w in lrs)


def test_pro_mode_taper_and_race_weeks_rest_restored():
    """职业模式：减量/比赛周恢复常规——无两练，休息日恢复。"""
    res = generate_plan(_spec(pro_mode=True))
    by_date = _by_date(res)
    weeks = _weeks_of(res)
    taper_wi = res.total_weeks - res.phase_weeks["taper"]
    for wi in (taper_wi, res.total_weeks - 1):
        ws = weeks[wi]
        assert all(len(by_date[w.date]) == 1 for w in ws), f"week {wi} 不应有两练"
    # 比赛周前两天（周五/周六）休息
    rw = weeks[res.total_weeks - 1]
    rest_dates = {res.race_date - timedelta(days=2), res.race_date - timedelta(days=1)}
    assert all(w.date not in rest_dates for w in rw)


def test_pro_mode_weekly_volume_band_around_reported():
    """职业模式：周量展示上浮约 7×30 分钟；实际总量落在展示值的 0.85–1.15 区间。"""
    res = generate_plan(_spec(pro_mode=True))
    plain = generate_plan(_spec(pro_mode=False))
    weeks = _weeks_of(res)
    # 展示上浮：非减量周 target 高于普通模式
    assert res.weekly_km[0] > plain.weekly_km[0]
    for wi, ws in weeks.items():
        if wi >= res.total_weeks - 2:
            continue
        km = sum(w.distance_km or 0 for w in ws)
        assert res.weekly_km[wi] * 0.85 <= km <= res.weekly_km[wi] * 1.15, \
            f"week {wi}: {km:.1f} vs reported {res.weekly_km[wi]}"


def test_pro_mode_down_week_keeps_doubles_but_no_threshold_split():
    """职业模式 down 恢复周：保留二练频率但 T 日不做双阈值拆分（降级为晚跑）。"""
    res = generate_plan(_spec(pro_mode=True))
    by_date = _by_date(res)
    down_wis = [wi for wi in range(res.total_weeks) if wi % 4 == 3
                and wi < res.total_weeks - 1 - res.phase_weeks["taper"]]
    assert down_wis
    for wi in down_wis:
        ws = [w for w in res.workouts if w.week_index == wi]
        pairs = [by_date[d] for d in sorted({w.date for w in ws}) if len(by_date[d]) == 2]
        assert pairs, f"down 周 {wi} 应保留二练"
        assert not any(p[0].kind == p[1].kind == "T" for p in pairs), \
            f"down 周 {wi} 不应做双阈值拆分"


def test_pro_mode_strength_day_double_with_evening_run():
    """职业模式力量课：力量课 + 傍晚放松跑（当日两练，不再独占）。"""
    res = generate_plan(_spec(pro_mode=True, strength_days=2))
    by_date = _by_date(res)
    strengths = [w for w in res.workouts if w.kind == "STRENGTH"]
    assert strengths
    for w in strengths:
        ws = by_date[w.date]
        assert len(ws) == 2 and ws[0].slot == 1 and ws[1].slot == 2
        assert ws[1].kind == "RECOVERY"


# ---------- 恢复/轻松课目标配速带（六区接入） ----------

def test_recovery_workouts_land_in_recovery_band_not_easy():
    """恢复跑（kind=RECOVERY）目标带应为 50–59%VDOT 恢复带，不再共用 E 带
    （旧引擎误按 59–74% 落库致配速过快）；轻松跑仍落 E 带，各强度分化明确。"""
    from runtrainer.domain import vdot as vd
    v = 50.0
    t = vd.pace_table(v)
    res = generate_plan(_spec(vdot=v, double_days=1))
    recs = [w for w in res.workouts if w.kind == "RECOVERY"]
    es = [w for w in res.workouts if w.kind == "E"]
    assert recs and es, "双练计划应含恢复跑与轻松跑"
    for w in recs:
        assert w.pace_zone == "RECOVERY"
        assert w.pace_slow_s_km == t["RECOVERY"]["slow_s_km"]
        assert w.pace_fast_s_km == t["RECOVERY"]["fast_s_km"]
        # 主体段标注恢复带（恢复晚跑不再显示为 E 段）
        assert any(s["zone"] == "RECOVERY" for s in w.segments), w.title
    for w in es:
        assert w.pace_zone == "E"
        assert w.pace_slow_s_km == t["E"]["slow_s_km"]
        assert w.pace_fast_s_km == t["E"]["fast_s_km"]
    # 分化：恢复带整体慢于 E 带（慢端慢一档；59% 为两带共享边界）
    assert t["RECOVERY"]["slow_s_km"] > t["E"]["slow_s_km"]
    assert t["RECOVERY"]["fast_s_km"] <= t["E"]["slow_s_km"] + 1
    # 质量课不受影响：T 仍单锚点
    ts = [w for w in res.workouts if w.kind == "T"]
    assert ts and all(w.pace_zone == "T" and w.pace_slow_s_km == t["T"] for w in ts)
