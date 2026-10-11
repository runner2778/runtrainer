"""主观数据（feel/rpe）接入健康系统与仪表盘：周聚合 + 近 7 天汇总。"""
from datetime import date, datetime, timezone

from runtrainer.domain.workout_analysis import weekly_subjective
from runtrainer.services.dashboard_service import _subjective_summary

AS_OF = date(2026, 10, 7)


def _ts(d: date, hour: int = 7) -> int:
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp())


def _act(d: date, **kw) -> dict:
    a = {"start_ts": _ts(d), "distance_m": 10000, "feel": None, "rpe": None,
         "name": "训练", "note": None}
    a.update(kw)
    return a


# --------------------------------------------------------------- 周聚合

def test_weekly_subjective_averages_labels_per_week():
    acts = [
        _act(date(2026, 9, 29), rpe=6, feel=3),
        _act(date(2026, 9, 30), rpe=8, feel=4),   # 同一 ISO 周（周一起）
        _act(date(2026, 10, 5), rpe=4),
    ]
    out = weekly_subjective(acts, date(2026, 9, 1), AS_OF)
    by_week = {w["week_start"]: w for w in out}
    w1 = by_week["2026-09-28"]
    assert w1["runs"] == 2 and w1["rpe_n"] == 2
    assert w1["rpe_avg"] == 7.0                   # (6+8)/2，简单平均不按距离加权
    assert w1["feel_avg"] == 3.5
    w2 = by_week["2026-10-05"]
    assert w2["rpe_avg"] == 4.0
    assert w2["feel_avg"] is None and w2["feel_n"] == 0
    # 最新周在前
    assert out[0]["week_start"] == "2026-10-05"


def test_weekly_subjective_unlabelled_week_still_listed():
    """没标标签的周也要出行——否则曲线会在没标签的周整段消失，
    看起来像「没跑步」而不是「没打分」。"""
    acts = [_act(date(2026, 9, 29), rpe=6), _act(date(2026, 10, 5))]
    out = weekly_subjective(acts, date(2026, 9, 1), AS_OF)
    assert len(out) == 2
    assert out[0]["runs"] == 1 and out[0]["rpe_avg"] is None


def test_weekly_subjective_filters_by_window():
    acts = [_act(date(2026, 1, 5), rpe=9), _act(date(2026, 10, 5), rpe=3)]
    out = weekly_subjective(acts, date(2026, 9, 1), AS_OF)
    assert [w["week_start"] for w in out] == ["2026-10-05"]


def test_weekly_subjective_ignores_rows_without_start_ts():
    out = weekly_subjective([{"feel": 3, "rpe": 5}], date(2026, 9, 1), AS_OF)
    assert out == []


# --------------------------------------------------------------- 仪表盘汇总

def test_subjective_summary_counts_and_averages():
    acts = [
        _act(date(2026, 10, 6), rpe=7, feel=4, note="风大"),
        _act(date(2026, 10, 5), rpe=9, feel=5),
        _act(date(2026, 9, 20), rpe=3),           # 窗口外（>7 天）
    ]
    s = _subjective_summary(acts, AS_OF)
    assert s["days"] == 7
    assert s["runs"] == 2                         # 窗口外的没算进来
    assert s["rpe_n"] == 2 and s["rpe_avg"] == 8.0 and s["rpe_max"] == 9
    assert s["feel_n"] == 2 and s["feel_avg"] == 4.5
    assert [n["note"] for n in s["notes"]] == ["风大"]
    assert s["notes"][0]["date"] == "2026-10-06"


def test_subjective_summary_is_empty_without_labels():
    s = _subjective_summary([_act(date(2026, 10, 6))], AS_OF)
    assert s["runs"] == 1
    assert s["rpe_n"] == 0 and s["rpe_avg"] is None and s["rpe_max"] is None
    assert s["feel_avg"] is None and s["notes"] == []


def test_subjective_summary_caps_notes_at_five_newest():
    acts = [_act(date(2026, 10, d), note=f"备注{d}") for d in range(1, 8)]
    notes = _subjective_summary(acts, AS_OF)["notes"]
    assert len(notes) == 5
    assert notes[0]["note"] == "备注7"            # 最新在前
