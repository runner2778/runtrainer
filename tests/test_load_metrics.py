"""执行覆盖 / 自动完成判定纯函数（load_metrics，第十七批）。

背景口径：执行率只回答「课表被执行的程度」——按计划日与实际跑步配对、
逐日封顶、只认跑步；计划外的自由跑与减载周自由跑另有展示位，不能把
执行率撑成几百 %（真实库曾出现 2325%）。
"""
from __future__ import annotations

from runtrainer.domain import load_metrics as lm


def _w(id_, day, km, kind="E", status="planned"):
    return {"id": id_, "date": day, "distance_km": km, "kind": kind, "status": status}


def test_is_running_accepts_codes():
    """跑步代码双码容忍：英文 code 与中文名（跨训练不算执行课表）。"""
    assert lm.is_running("running") and lm.is_running("run")
    assert lm.is_running("跑步") and lm.is_running("跑步机")
    assert not lm.is_running("cycling") and not lm.is_running("骑行")
    assert not lm.is_running(None) and not lm.is_running("")


def test_run_days_from_sessions_merges_same_day():
    """同日多次跑合并（早上 5km + 晚上 7km = 12km）。"""
    s = [{"date": "2026-09-01T07:00:00", "distance_km": 5.0},
         {"date": "2026-09-01T19:00:00", "distance_km": 7.0},
         {"date": "2026-09-02T07:00:00", "distance_km": 3.0}]
    out = lm.run_days_from_sessions(s)
    assert out == {"2026-09-01": 12.0, "2026-09-02": 3.0}


def test_plan_coverage_caps_overrun_per_day():
    """计划日实际跑量超出计划量时按计划量封顶；自由跑日不计入执行。"""
    rows = [_w(1, "2026-09-01", 6.0), _w(2, "2026-09-03", 10.0)]
    runs = {"2026-09-01": 30.0, "2026-09-02": 50.0}   # 09-02 无计划课 → 自由跑
    c = lm.plan_coverage(rows, runs)
    assert c["planned_km"] == 16.0
    assert c["done_km"] == 30.0                        # 计划日实际（不封顶）
    assert c["covered_km"] == 6.0
    assert c["ratio"] == 6.0 / 16.0                    # 0.375 ≤ 1，不再 2325%
    assert c["planned_days"] == 2 and c["covered_days"] == 1


def test_plan_coverage_no_plan_rows_ratio_none():
    """窗口内无计划课（开课前/比赛后）：ratio=None，不做分母比较。"""
    c = lm.plan_coverage([], {"2026-09-01": 12.0})
    assert c["planned_km"] == 0 and c["ratio"] is None and c["covered_km"] == 0


def test_plan_coverage_partial_day_counts_proportionally():
    """只跑计划量一半 → 该日覆盖一半（逐日比例配对，不做全有或全无）。"""
    rows = [_w(1, "2026-09-01", 10.0)]
    c = lm.plan_coverage(rows, {"2026-09-01": 4.0})
    assert c["covered_km"] == 4.0 and c["ratio"] == 0.4
    assert c["covered_days"] == 1                       # 有跑即算覆盖日


def test_auto_done_half_distance_rule():
    """单课当天跑量 ≥ 计划量一半（且 ≥0.8km）→ 自动完成；不足不算。"""
    rows = [_w(1, "2026-09-01", 8.0), _w(2, "2026-09-02", 8.0)]
    assert lm.workout_auto_done(rows, {"2026-09-01": 4.0}) == {1}
    assert lm.workout_auto_done(rows, {"2026-09-01": 3.9}) == set()
    # 下限 0.8km：短课不至于因为几百米散步被判定完成
    tiny = [_w(3, "2026-09-01", 1.0)]
    assert lm.workout_auto_done(tiny, {"2026-09-01": 0.79}) == set()
    assert lm.workout_auto_done(tiny, {"2026-09-01": 0.8}) == {3}
    # 无距离课（力量外的时长课）按 1km 计计划量 → 阈值 max(0.5, 0.8)=0.8
    nodist = [_w(4, "2026-09-01", None)]
    assert lm.workout_auto_done(nodist, {"2026-09-01": 0.79}) == set()
    assert lm.workout_auto_done(nodist, {"2026-09-01": 0.8}) == {4}


def test_auto_done_greedy_split_multi_same_day():
    """同日多节课贪心摊分：计划量大的课先匹配，跑量不足后面的课不算完成。"""
    rows = [_w(1, "2026-09-01", 10.0), _w(2, "2026-09-01", 5.0)]
    assert lm.workout_auto_done(rows, {"2026-09-01": 12.0}) == {1}
    assert lm.workout_auto_done(rows, {"2026-09-01": 13.0}) == {1, 2}


def test_auto_done_excludes_skipped_and_strength():
    """显式跳过的课与力量课不参与自动完成。"""
    rows = [_w(1, "2026-09-01", 10.0, status="skipped"),
            _w(2, "2026-09-01", 6.0, kind="STRENGTH"),
            _w(3, "2026-09-01", 8.0)]
    assert lm.workout_auto_done(rows, {"2026-09-01": 30.0}) == {3}


def test_auto_done_ignores_irrelevant_run_days():
    """没有计划课的日期跑再多也不影响任何课的判定。"""
    rows = [_w(1, "2026-09-01", 8.0)]
    assert lm.workout_auto_done(rows, {"2026-09-05": 20.0}) == set()
