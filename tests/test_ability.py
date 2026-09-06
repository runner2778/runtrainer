"""能力预估：综合手表 VO2max / 配速-心率趋势 / 间歇能力 / 比赛成绩。"""
from __future__ import annotations

from datetime import timedelta

from runtrainer.domain import ability as ab
from runtrainer.domain import vdot as vd
from runtrainer.utils import dates


def _act(days_ago, pace_s_km, avg_hr, max_hr, dist_m, dur_s, name="轻松跑", structure=None):
    d = dates.today() - timedelta(days=days_ago)
    return {
        "name": name, "start_ts": dates.date_to_ts(d), "distance_m": dist_m,
        "duration_s": dur_s, "avg_pace_s_km": pace_s_km, "avg_hr": avg_hr,
        "max_hr": max_hr, "structure": structure or [],
    }


def test_empty_returns_no_vdot():
    est = ab.compute_ability([], None)
    assert est["vdot"] is None
    assert est["predictions"] is None
    assert est["evidence"] == []


def test_vo2max_only_uses_watch_reading():
    est = ab.compute_ability([], 55.0)
    assert est["vdot"] == 55.0
    assert est["predictions"]["5K"] < est["predictions"]["10K"] < est["predictions"]["半马"]
    assert any(ev["source"] == "garmin_vo2max" for ev in est["evidence"])


def test_race_is_hard_evidence_and_clamps_watch_overread():
    """比赛成绩是硬证据；手表读数虚高（63 vs 比赛等效约 53）时钳制到比赛 + 2。"""
    # 9.8km / 42:00，心率 180/190 = 94.7% ≥ 88% → 判定为 10K 比赛成绩
    acts = [_act(10, 259, 180, 190, 9800, 42 * 60)]
    race_vdot = round(vd.estimate_vdot(9800, 42 * 60), 1)
    est = ab.compute_ability(acts, 63.0)
    assert est["vdot"] is not None
    race_ev = next(ev for ev in est["evidence"] if ev["source"] == "recent_race")
    assert race_ev["vdot"] == race_vdot
    # 上限钳制：加权（手表 63 拉高）后不超过 race+2
    assert est["vdot"] <= race_ev["vdot"] + 2.1
    assert est["vdot"] >= race_ev["vdot"]  # 不会低于比赛成绩本身
    assert all(est["predictions"][k] > 0 for k in ("5K", "10K", "半马", "全马"))


def test_threshold_trend_from_pace_hr_regression():
    """配速-心率线性回归 → 88% HRmax 阈值配速 → VDOT 分量。"""
    # hr = -0.5 * pace + 300：pace 300 → 150；pace 260 → 170。HRmax=193 → 88%=170
    # 活动 max_hr 设 210：避免 hr/max_hr ≥ 0.88 把 10km 慢跑误判成比赛
    acts = []
    for i, (pace, hr) in enumerate([(310, 145), (300, 150), (295, 152.5), (290, 155),
                                    (280, 160), (270, 165), (265, 167.5), (260, 170)]):
        acts.append(_act(80 - i * 5, pace, hr, 210, 10000, 3000, name=f"跑 {i}"))
    est = ab.compute_ability(acts, None, profile_max_hr=193)
    thr = next(ev for ev in est["evidence"] if ev["source"] == "threshold_trend")
    # 88% × 193 = 169.84 → pace = (169.84 - 300) / -0.5 = 260.3 s/km
    assert abs(thr["pace_s_km"] - 260.3) < 2
    assert est["vdot"] is not None
    assert 45 < est["vdot"] < 62


def test_threshold_trend_robust_to_gps_and_hr_outliers():
    """GPS 漂移（假快配速）与心率异常低样本不应拉偏回归斜率。"""
    acts = []
    for i, (pace, hr) in enumerate([(310, 145), (300, 150), (295, 152.5), (290, 155),
                                    (280, 160), (270, 165), (265, 167.5), (260, 170)]):
        acts.append(_act(80 - i * 5, pace, hr, 210, 10000, 3000, name=f"跑 {i}"))
    # 两条失真样本：3:40/km @ 心率 110/119（传感器异常）——低于 HR 下限被剔除
    acts.append(_act(30, 220, 110, 210, 6000, 1400, name="漂移"))
    acts.append(_act(25, 242, 119, 210, 6000, 1600, name="漂移2"))
    est = ab.compute_ability(acts, None, profile_max_hr=193)
    thr = next(ev for ev in est["evidence"] if ev["source"] == "threshold_trend")
    assert abs(thr["pace_s_km"] - 260.3) < 3
    # 同样两条假样本若混入回归，阈值配速会被拉到明显更快（~3:40/km 外推）
    assert thr["pace_s_km"] > 255


def test_interval_ability_component():
    """含 work 段的活动 → 间歇能力分量（按 I 强度反算）。"""
    acts = [
        _act(5, 320, 140, 185, 8000, 43 * 60, structure=[
            {"type": "work", "distance_m": 800, "pace_s_km": 250, "avg_hr": 178},
            {"type": "rest", "distance_m": 400, "pace_s_km": 430, "avg_hr": 150},
            {"type": "work", "distance_m": 800, "pace_s_km": 252, "avg_hr": 179},
            {"type": "rest", "distance_m": 400, "pace_s_km": 432, "avg_hr": 151},
        ]),
    ]
    est = ab.compute_ability(acts, None, profile_max_hr=185)
    ev = next(ev for ev in est["evidence"] if ev["source"] == "interval_ability")
    assert ev["vdot"] > 0
    # 无比赛无手表读数：阈值趋势（1 条样本不足）无分量，间歇是唯一依据
    assert est["vdot"] == ev["vdot"]


def test_interval_rest_ratio_adjusts_vdot():
    """休息/快跑比变量：休息越短、快段越快 → 间歇水平越高。"""
    # 两课快段配速相同，但休息比不同：短休息课修正后 VDOT 更高
    base = ab._vdot_for_pace(250, vd.I_PCT)
    short_rest = [
        _act(6, 320, 140, 185, 8000, 43 * 60, structure=[
            {"type": "work", "distance_m": 800, "elapsed_s": 200, "pace_s_km": 250, "avg_hr": 178},
            {"type": "rest", "distance_m": 200, "elapsed_s": 60, "pace_s_km": 400, "avg_hr": 150},
            {"type": "work", "distance_m": 800, "elapsed_s": 200, "pace_s_km": 252, "avg_hr": 179},
            {"type": "rest", "distance_m": 200, "elapsed_s": 60, "pace_s_km": 402, "avg_hr": 151},
            {"type": "work", "distance_m": 800, "elapsed_s": 200, "pace_s_km": 248, "avg_hr": 180},
        ]),
    ]
    long_rest = [
        _act(4, 320, 140, 185, 8000, 50 * 60, structure=[
            {"type": "work", "distance_m": 800, "elapsed_s": 200, "pace_s_km": 250, "avg_hr": 178},
            {"type": "rest", "distance_m": 400, "elapsed_s": 200, "pace_s_km": 450, "avg_hr": 148},
            {"type": "work", "distance_m": 800, "elapsed_s": 200, "pace_s_km": 252, "avg_hr": 179},
            {"type": "rest", "distance_m": 400, "elapsed_s": 200, "pace_s_km": 452, "avg_hr": 149},
            {"type": "work", "distance_m": 800, "elapsed_s": 200, "pace_s_km": 248, "avg_hr": 180},
        ]),
    ]
    # 短休息比 120/600=0.2 → ×(1+0.2×0.4)=1.08；长休息比 400/600=0.67 → ×0.986
    ev_short = ab.interval_ability(short_rest)
    ev_long = ab.interval_ability(long_rest)
    assert abs(ev_short["vdot"] - round(base * 1.08, 1)) < 0.21
    assert abs(ev_long["vdot"] - round(base * (1 + 0.2 * (0.6 - 400 / 600)), 1)) < 0.21
    assert ev_short["vdot"] > ev_long["vdot"]
    assert ev_short["rest_ratio"] == 0.2
    # 混合两课：中位数聚合（短休息课拉高）
    mixed = ab.interval_ability(short_rest + long_rest)
    assert mixed["n_workouts"] == 2
    assert abs(mixed["vdot"] - round(base * 1.08, 1)) < 0.21


def test_hrr_pace_component():
    """储备心率对应配速：70% HRR 回归配速 → VDOT 分量。"""
    # HRR = (hr-50)/150；配速 = -222.2×HRR + 441.1（精确线性，70% HRR → 285.6）
    pairs = [(330, 125), (318.9, 132.5), (307.8, 140), (296.7, 147.5), (285.6, 155)]
    acts = [_act(40 - i * 5, pace, hr, 200, 10000, 3000, name=f"有氧 {i}")
            for i, (pace, hr) in enumerate(pairs)]
    est = ab.compute_ability(acts, None, profile_max_hr=200, rest_hr=50)
    ev = next(ev for ev in est["evidence"] if ev["source"] == "hrr_pace")
    # 70% HRR → 285.6s/km
    assert abs(ev["pace_s_km"] - 285.6) < 2
    assert ev["vdot"] > 0
    # 无比赛无手表：HRR 是唯一依据
    assert est["vdot"] == ev["vdot"]


def test_hrr_pace_skips_without_rest_hr():
    """缺静息心率 → 无 HRR 分量（不报错）。"""
    acts = [_act(40 - i, 300, 140, 200, 10000, 3000) for i in range(6)]
    est = ab.compute_ability(acts, None, profile_max_hr=200)
    assert not any(ev["source"] == "hrr_pace" for ev in est["evidence"])


def test_max_hr_estimate_falls_back_to_activities():
    """档案无 HRmax → 活动 max_hr 的 95 分位。"""
    acts = [_act(30 - i, 300, 150, 180 + i, 5000, 25 * 60) for i in range(10)]
    est = ab.compute_ability(acts, None)
    assert est["max_hr"] == 189  # 95 分位


def test_hr_trend_decline_adjusts_vdot_up():
    """同配速心率下降 → 有氧能力进步，VDOT 保守上调（封顶 +1.5）。"""
    acts = [
        # 早期（~3 个月前）：5:00/km 心率 155/153（同档同档期，180 天窗口 5 期时
        # 105/100 天前都在第 2 期——75/70 天前会踩 36 天期界被拆散）
        _act(105, 300, 155, 210, 10000, 3000, name="有氧 1"),
        _act(100, 301, 153, 210, 10000, 3000, name="有氧 2"),
        # 近期：同配速心率 149/147（降 6 bpm）
        _act(10, 300, 149, 210, 10000, 3000, name="有氧 3"),
        _act(5, 302, 147, 210, 10000, 3000, name="有氧 4"),
    ]
    est = ab.compute_ability(acts, 50.0)
    ev = next(ev for ev in est["evidence"] if ev["source"] == "hr_trend")
    assert ev["detail"].startswith("同配速（5:00/km）")
    assert abs(est["vdot"] - 51.5) < 0.01  # 50 + min(6×0.35, 1.5)


def test_hr_trend_rise_penalty_clamped():
    """同配速心率上升 → 退步/疲劳，惩罚保守（下限 -0.5，天气也会抬心率）。"""
    acts = [
        _act(105, 300, 149, 210, 10000, 3000, name="有氧 1"),
        _act(100, 301, 147, 210, 10000, 3000, name="有氧 2"),
        _act(10, 300, 155, 210, 10000, 3000, name="有氧 3"),
        _act(5, 302, 153, 210, 10000, 3000, name="有氧 4"),
    ]
    est = ab.compute_ability(acts, 50.0)
    next(ev for ev in est["evidence"] if ev["source"] == "hr_trend")
    assert abs(est["vdot"] - 49.5) < 0.01  # 50 - 0.5


def test_hr_trend_skips_without_comparable_periods():
    """只有单时期样本 → 无趋势调整，不虚增/虚减。"""
    acts = [_act(10 - i, 300, 150, 210, 10000, 3000) for i in range(4)]
    est = ab.compute_ability(acts, 50.0)
    assert not any(ev["source"] == "hr_trend" for ev in est["evidence"])
    assert est["vdot"] == 50.0

# ---- 近一年最佳成绩 / 训练保持度（第十四批）----
def _samples_km(km, pace_s_km, hr=160):
    """匀速跑：每 1 秒一个样本（距离≈km*1000 m）。"""
    spd = 1000.0 / pace_s_km
    # 每样本 1 秒、覆盖 spd 米 → 共需 km*1000/spd = km*pace_s_km 个样本
    return [{"t_offset_s": float(i), "hr": hr, "speed_mps": spd}
            for i in range(int(km * pace_s_km))]


def _yr_act(days_ago, dist_m, dur_s, avg_hr=None, max_hr=None,
            name="训练", aid=1, has_samples=False):
    d = dates.today() - timedelta(days=days_ago)
    return {"id": aid, "name": name, "start_ts": dates.date_to_ts(d),
            "date": d.isoformat(), "distance_m": dist_m, "duration_s": dur_s,
            "avg_pace_s_km": dur_s / dist_m * 1000 if dist_m else None,
            "avg_hr": avg_hr, "max_hr": max_hr, "has_samples": has_samples}


def test_year_bests_race_whole_run():
    """整场近似比赛（名称含 10k + 心率 0.89）→ 直接折算。"""
    acts = [_yr_act(20, 10000, 40 * 60, avg_hr=170, max_hr=190,
                    name="10K 比赛", aid=1)]
    out = ab.distance_bests(acts, None, max_hr=190)
    assert len(out) == 1 and out[0]["distance"] == "10K"
    assert out[0]["source"] == "race"
    assert abs(out[0]["best_seconds"] - 2400) < 5
    assert abs(out[0]["vdot"] - round(vd.estimate_vdot(10000, 2400), 1)) < 0.01


def test_year_bests_segment_from_long_run():
    """长跑滑窗切最快分段：10K@4:00 → 5K 最快 = 20:00、10K = 40:00。"""
    acts = [_yr_act(10, 10000, 40 * 60, name="长距离", aid=1, has_samples=True)]
    out = ab.distance_bests(acts, lambda aid: _samples_km(10, 240), max_hr=190)
    assert [b["distance"] for b in out] == ["5K", "10K"]
    assert out[0]["source"] == "effort"
    assert abs(out[0]["best_seconds"] - 1200) < 15  # 20:00 折算 5K
    assert abs(out[1]["best_seconds"] - 2400) < 15
    # 折算成绩与 VDOT 一致性：5K 20:00 等效 VDOT
    assert abs(out[0]["vdot"] - round(vd.estimate_vdot(5000, 1200), 1)) < 0.01


def test_year_bests_hr_gate_excludes_easy_segments():
    """低强度分段的平均心率 <82% 最大心率 → 不当最佳成绩。"""
    acts = [_yr_act(10, 10000, 40 * 60, avg_hr=130, max_hr=190,
                    name="放松跑", aid=1, has_samples=True)]
    samples = _samples_km(10, 240, hr=130)
    assert ab.distance_bests(acts, lambda aid: samples, max_hr=190) == []
    # 无最大心率参考时不设心率门（纯配速信息给最低限参考）
    assert len(ab.distance_bests(acts, lambda aid: samples, max_hr=None)) == 2


def test_year_bests_effort_beats_race_when_faster():
    """同日段证据取等效 VDOT 更高者：快分段覆盖慢比赛。"""
    races = [_yr_act(5, 10000, 43 * 60, avg_hr=175, max_hr=190,
                     name="10K 比赛", aid=1)]
    fast = _yr_act(3, 12000, 50 * 60, name="节奏跑", aid=2, has_samples=True)
    loader = {1: None, 2: _samples_km(12, 235, hr=165)}
    out = ab.distance_bests(races + [fast],
                            lambda aid: loader[aid], max_hr=190)
    ten = next(b for b in out if b["distance"] == "10K")
    assert ten["source"] == "effort"
    assert ten["best_seconds"] < 43 * 60


def test_consistency_weeks_and_recent_vs_year():
    """保持度：近 7 天天天跑（10km）+ 40/90 天前各一次 → 周活跃率 & 跑量对比。"""
    acts = []
    for days_ago in range(7):
        acts.append(_yr_act(days_ago, 10000, 3000, aid=100 + days_ago))
    acts.append(_yr_act(40, 8000, 2400, aid=200))
    acts.append(_yr_act(90, 5000, 1500, aid=201))
    c = ab.training_consistency(acts)
    span = 90 // 7 + 1  # 首次跑步到今天的周数
    assert c["total_weeks"] == span
    assert c["run_weeks"] == 3  # 最近 1 周 + 40 天周 + 90 天周
    assert c["run_week_pct"] == round(3 / span * 100)
    year_avg = (7 * 10 + 8 + 5) / span
    assert abs(c["recent_4w_avg_km"] - 7 * 10 / 4) < 0.01
    assert c["recent_vs_year_pct"] == round(7 * 10 / 4 / year_avg * 100)
