"""预估系统：环境折算（坡度/跑步机）与主观用力（RPE/feel）的接入。"""
from datetime import date, datetime, timezone

from runtrainer.domain import evidence as evm

MAX_HR = 186.0
AS_OF = date(2026, 10, 1)
# 窗口内的基准时间戳（默认落在 AS_OF 前 10 天，避免被 180 天滑窗切掉）
BASE_TS = int(datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc).timestamp())


def _act(**kw):
    a = {"id": 1, "source": "manual", "external_id": "a1", "name": "训练",
         "sport": "running",
         "start_ts": BASE_TS, "tz_offset_min": 480, "duration_s": 3000,
         "distance_m": 10000, "avg_pace_s_km": 300, "avg_hr": 160, "max_hr": 180,
         "has_samples": 0, "structure": []}
    a.update(kw)
    return a


def _steady(**kw):
    """一节「连续匀速强度课」：配速 300 s/km、心率 88% HRmax、时长 50 分钟。"""
    base = {"avg_pace_s_km": 300, "avg_hr": 164, "duration_s": 3000}
    base.update(kw)
    return _act(**base)


# --------------------------------------------------------------- 环境折算

def test_is_treadmill_by_name_in_both_languages():
    assert evm.is_treadmill({"name": "Treadmill Running"})
    assert evm.is_treadmill({"name": "跑步机"})
    assert evm.is_treadmill({"name": "室内跑 30 分钟"})
    assert not evm.is_treadmill({"name": "河边轻松跑"})
    assert not evm.is_treadmill({"name": None})


def test_env_adjust_flat_outdoor_is_unchanged():
    a = _act(distance_m=10000, elevation_gain_m=20)      # 2 m/km ≈ 平路
    assert evm.env_adjusted_pace(a, 300.0) == 300.0 - 2 * evm.GRADE_PACE_S_KM_PER_M


def test_env_adjust_hilly_run_reads_as_faster_flat_equivalent():
    """上坡同心率更慢：不折算会低估水平，折算后等效配速应当更快（秒数更小）。"""
    a = _act(distance_m=10000, elevation_gain_m=200)     # 20 m/km
    adj = evm.env_adjusted_pace(a, 330.0)
    assert adj < 330.0
    assert adj == 330.0 - 20 * evm.GRADE_PACE_S_KM_PER_M


def test_env_adjust_treadmill_reads_as_slower_flat_equivalent():
    """跑步机同心率更快（无风阻）：不折算会高估水平，折算后配速应当更慢。"""
    a = _act(name="Treadmill Running", distance_m=10000, elevation_gain_m=0)
    assert evm.env_adjusted_pace(a, 300.0) == \
        300.0 + evm.TREADMILL_PACE_DELTA_S_KM


def test_env_adjust_grade_is_capped_and_ignores_tiny_activities():
    steep = _act(distance_m=10000, elevation_gain_m=5000)   # 500 m/km（离谱）
    ref = evm.env_adjusted_pace(steep, 400.0)
    assert ref >= 400.0 - evm.GRADE_ADJ_CAP_S_KM
    # 距离不足 1km 时坡度不参与（分母没意义）
    tiny = _act(distance_m=800, elevation_gain_m=80)
    assert evm.env_adjusted_pace(tiny, 300.0) == 300.0
    # 配速缺失时原样返回
    assert evm.env_adjusted_pace(_act(), None) is None


def test_steady_evidence_keeps_raw_pace_and_reports_adjustment():
    ev = evm.extract_workout_evidence(
        [_steady(elevation_gain_m=200)], None, MAX_HR, AS_OF, window_days=180)
    assert len(ev) == 1
    r = ev[0]
    assert r["kind"] == "steady"
    assert r["pace_s_km"] == 300.0                       # 实测值原样保留（供显示）
    assert r["pace_adj_s_km"] < 300.0                    # 等效值更快
    assert r["env_adj"] == round(300.0 - 20 * evm.GRADE_PACE_S_KM_PER_M - 300.0, 1)


def test_hilly_steady_yields_higher_vdot_than_flat_at_same_pace():
    flat = evm.extract_workout_evidence([_steady()], None, MAX_HR, AS_OF,
                                        window_days=180)[0]
    hilly = evm.extract_workout_evidence([_steady(elevation_gain_m=200)], None,
                                         MAX_HR, AS_OF, window_days=180)[0]
    assert hilly["vdot"] > flat["vdot"]


def test_treadmill_interval_yields_lower_vdot_than_outdoor():
    def interval(name):
        works = [{"type": "work", "pace_s_km": 240, "distance_m": 1000,
                  "elapsed_s": 240, "avg_hr": 178} for _ in range(6)]
        rests = [{"type": "rest", "elapsed_s": 90} for _ in range(5)]
        return _act(name=name, structure=works + rests, distance_m=9000)

    outdoor = evm.extract_workout_evidence([interval("间歇课")], None, MAX_HR,
                                           AS_OF, window_days=180)[0]
    treadmill = evm.extract_workout_evidence([interval("Treadmill Running")], None,
                                             MAX_HR, AS_OF, window_days=180)[0]
    assert treadmill["vdot"] < outdoor["vdot"]
    assert treadmill["pace_s_km"] == outdoor["pace_s_km"] == 240.0


def test_best_effort_evidence_carries_adjusted_pace():
    samples = [{"t_offset_s": t, "hr": 170, "speed_mps": 1000 / 300}
               for t in range(0, 5500, 5)]
    # 时长拉长到 90 分钟，避开「连续匀速强度课」判据（15–75 min），落到最快分段
    a = _act(distance_m=18000, duration_s=5400, has_samples=1,
             elevation_gain_m=360)
    ev = evm.extract_workout_evidence([a], lambda aid: samples, MAX_HR, AS_OF,
                                      window_days=180)
    assert ev, "有采样的大课应当切出最快分段"
    for r in ev:
        assert r["kind"] == "best_effort"
        assert r["pace_adj_s_km"] < r["pace_s_km"]


# --------------------------------------------------------------- 主观用力

def test_rpe_weight_is_neutral_without_labels():
    assert evm.rpe_weight(None) == 1.0
    assert evm.rpe_weight(0) == 1.0
    assert evm.rpe_weight(7) == 1.0                       # 正常强度 → 不调
    assert evm.rpe_weight(5) == evm.RPE_EASY_WEIGHT
    assert evm.rpe_weight(2) == evm.RPE_VERY_EASY_WEIGHT


def test_rpe_weight_never_inflates():
    assert all(evm.rpe_weight(v) <= 1.0 for v in (None, 0, 1, 3, 5, 7, 9, 10))


def test_evidence_records_carry_rpe_and_feel():
    a = _steady(rpe=4, feel=2)
    r = evm.extract_workout_evidence([a], None, MAX_HR, AS_OF, window_days=180)[0]
    assert r["rpe"] == 4 and r["feel"] == 2


def test_easy_rpe_downweights_that_evidence():
    """标了「很轻松」的强度课不足以单独抬高水平 → 加权中位被它拉动的幅度更小。"""
    strong = _steady(avg_pace_s_km=290, external_id="s1")
    weak = _steady(avg_pace_s_km=260, start_ts=BASE_TS + 86400,
                   external_id="s2", rpe=2)

    def centre(with_rpe):
        acts = [strong, weak]
        if not with_rpe:
            acts = [{**a, "rpe": None} for a in acts]
        ev = evm.extract_workout_evidence(acts, None, MAX_HR, AS_OF, window_days=180)
        return evm.current_fitness(ev, None, AS_OF).get("steady")

    plain = centre(False)
    labelled = centre(True)
    assert plain and labelled
    # 标了 RPE 后，「很快但自述很轻松」那条的影响力下降 → 中心值不再被它抬那么高
    assert labelled["vdot"] <= plain["vdot"]


def test_grade_race_uses_rpe_when_present():
    base = {"name": "常规跑", "day": "2026-09-21", "actual_vdot": 45.0}
    # 赛前 30 天内 ≥3 条同期训练证据（RACE_EVIDENCE_MIN_N=3）且明显高于比赛成绩
    ev = [{"day": f"2026-09-{d:02d}", "kind": "interval",
           "vdot": 55.0, "hr_ratio": 0.95} for d in (5, 8, 12)]
    # 无标签：退回证据判据 → 明显低于同期训练证据 → 次全力
    assert evm.grade_race(dict(base), ev, None, MAX_HR) == "submax"
    # 自述拼到极限 → 全开（主观优先于证据推断）
    assert evm.grade_race({**base, "rpe": 10}, ev, None, MAX_HR) == "all_out"
    # 自述还有余力 → 次全力
    assert evm.grade_race({**base, "actual_vdot": 55.0, "rpe": 4}, ev, None,
                          MAX_HR) == "submax"


def test_grade_race_rpe_absent_keeps_old_behaviour():
    race = {"name": "常规跑", "day": "2026-09-26", "actual_vdot": 50.0}
    without = evm.grade_race(dict(race), [], None, MAX_HR)
    with_none = evm.grade_race({**race, "rpe": None, "feel": None}, [], None,
                               MAX_HR)
    assert without == with_none == "all_out"


def test_grade_race_name_hint_still_wins_over_rpe():
    assert evm.grade_race({"name": "5K 比赛", "rpe": 3}, [], None, MAX_HR) == "all_out"
