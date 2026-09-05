"""时期智能判断：近期强度与跑量分布 → 训练时期（不碰 DB 的纯函数）。"""
from __future__ import annotations

from datetime import date, timedelta

from runtrainer.domain.phase_estimator import suggest_phase

TODAY = date(2026, 9, 5)
MAX_HR = 200.0

INTERVAL_SEGS = [
    {"type": "recovery", "distance_m": 1600, "duration_s": 600, "pace_s_km": 375, "avg_hr": 130},
    {"type": "work", "distance_m": 1000, "duration_s": 250, "pace_s_km": 250, "avg_hr": 175},
    {"type": "rest", "distance_m": 400, "duration_s": 200, "pace_s_km": 500, "avg_hr": 140},
    {"type": "work", "distance_m": 1000, "duration_s": 250, "pace_s_km": 250, "avg_hr": 178},
    {"type": "rest", "distance_m": 400, "duration_s": 200, "pace_s_km": 500, "avg_hr": 145},
    {"type": "work", "distance_m": 1000, "duration_s": 250, "pace_s_km": 250, "avg_hr": 180},
    {"type": "recovery", "distance_m": 1600, "duration_s": 600, "pace_s_km": 375, "avg_hr": 135},
]


def _easy(days_ago: int, km: float = 8.0) -> dict:
    return {"date": (TODAY - timedelta(days=days_ago)).isoformat(),
            "distance_m": km * 1000, "duration_s": km * 330, "avg_pace_s_km": 330,
            "avg_hr": 130, "structure": None}


def _interval(days_ago: int) -> dict:
    return {"date": (TODAY - timedelta(days=days_ago)).isoformat(),
            "distance_m": 6000, "duration_s": 2100, "avg_pace_s_km": 300,
            "avg_hr": 158, "structure": INTERVAL_SEGS}


def _weeks_runs(weeks=8, days=(0, 2, 5), km=8.0) -> list[dict]:
    """每周 days 天各一次训练，跨近 weeks 周。"""
    return [_easy(7 * w + o, km) for w in range(weeks) for o in days]


def _ago(a: dict) -> int:
    return (TODAY - date.fromisoformat(a["date"])).days


def test_easy_only_suggests_base():
    res = suggest_phase(_weeks_runs(), TODAY, max_hr=MAX_HR)
    assert res["phase"] == "base"
    assert res["confidence"] == "high"
    assert res["stats"]["quality_sessions"] == 0


def test_two_intervals_suggest_transition():
    acts = _weeks_runs()
    acts += [_interval(10), _interval(3)]
    res = suggest_phase(acts, TODAY, max_hr=MAX_HR)
    assert res["phase"] == "transition"
    assert res["stats"]["quality_sessions"] == 2


def test_three_quality_sessions_suggest_final():
    acts = _weeks_runs()
    acts += [_interval(10), _interval(5), _interval(1)]
    res = suggest_phase(acts, TODAY, max_hr=MAX_HR)
    assert res["phase"] == "final"


def test_volume_drop_near_race_suggests_taper():
    # 前 4 周 10km×3/周，近 4 周掉到 6km×2/周，比赛 14 天后
    acts = [a for a in _weeks_runs(weeks=8, km=10.0) if _ago(a) >= 28]
    acts += _weeks_runs(weeks=4, days=(0, 4), km=6.0)
    res = suggest_phase(acts, TODAY, race_date=TODAY + timedelta(days=14), max_hr=MAX_HR)
    assert res["phase"] == "taper"


def test_volume_drop_without_race_suggests_base():
    acts = [a for a in _weeks_runs(weeks=8, km=10.0) if _ago(a) >= 28]
    acts += _weeks_runs(weeks=4, days=(0, 4), km=6.0)
    res = suggest_phase(acts, TODAY, race_date=TODAY + timedelta(days=60), max_hr=MAX_HR)
    assert res["phase"] == "base"


def test_few_runs_low_confidence():
    res = suggest_phase([_easy(1), _easy(5)], TODAY, max_hr=MAX_HR)
    assert res["confidence"] == "low"
    assert res["phase"] == "base"


def test_no_data_suggests_base_low():
    res = suggest_phase([], TODAY, max_hr=MAX_HR)
    assert res["phase"] == "base" and res["confidence"] == "low"
