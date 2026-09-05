"""M4：AI 输出护栏规则测试（覆盖 10 条规则中可测的部分）。"""
from __future__ import annotations

from datetime import date, timedelta

from runtrainer.ai import guardrails
from runtrainer.ai.contracts import (
    AddExtraAdvice, AdjustmentItem, ChangeSet, CoachOutput, ExtraSuggestion,
)

TODAY = date(2026, 9, 7)   # 周一
PACES = {"vdot": 45.0, "E": {"slow_s_km": 335, "fast_s_km": 300},
         "M": 296, "T": 278, "I": 255, "R": 235}

# 一周课表：Mon E8 / Tue REC5 / Wed T10(硬) / Thu E8 / Fri 空 / Sat I7(硬) / Sun LR14
WORKOUTS = [
    {"id": 1, "date": "2026-09-07", "kind": "E", "pace_zone": "E",
     "distance_km": 8.0, "duration_min": 50.0, "status": "planned"},
    {"id": 2, "date": "2026-09-08", "kind": "RECOVERY", "pace_zone": "E",
     "distance_km": 5.0, "duration_min": 32.0, "status": "planned"},
    {"id": 3, "date": "2026-09-09", "kind": "T", "pace_zone": "T",
     "distance_km": 10.0, "duration_min": 60.0, "status": "planned"},
    {"id": 4, "date": "2026-09-10", "kind": "E", "pace_zone": "E",
     "distance_km": 8.0, "duration_min": 50.0, "status": "planned"},
    {"id": 6, "date": "2026-09-12", "kind": "I", "pace_zone": "I",
     "distance_km": 7.0, "duration_min": 45.0, "status": "planned"},
    {"id": 7, "date": "2026-09-13", "kind": "LR", "pace_zone": "E",
     "distance_km": 14.0, "duration_min": 90.0, "status": "planned"},
]


def _ctx(**kw) -> guardrails.GuardContext:
    defaults = dict(
        today=TODAY, race_date=TODAY + timedelta(days=90),
        week_start=TODAY, week_end=TODAY + timedelta(days=6),
        week_km=40.0, workouts=WORKOUTS, paces=PACES,
        add_extra_count_this_week=0, extra_requested=False,
    )
    defaults.update(kw)
    return guardrails.GuardContext(**defaults)


def _out(adjustments: list[dict], extra: dict | None = None) -> CoachOutput:
    items = []
    for a in adjustments:
        items.append(AdjustmentItem(
            date=a["date"], planned_workout_id=a.get("planned_workout_id"),
            action=a["action"],
            changes=ChangeSet(**a["changes"]) if a.get("changes") else None,
            reason=a.get("reason") or "测试理由",
        ))
    out = CoachOutput(summary="测试", readiness="ok", adjustments=items)
    if extra:
        out.add_extra_advice = AddExtraAdvice(
            allowed=extra.get("allowed", True),
            suggestion=ExtraSuggestion(**extra["suggestion"]))
    return out


def _extra(kind="E", duration=30.0):
    return {"allowed": True, "suggestion": {"kind": kind, "duration_min": duration,
                                            "max_duration_min": 45, "pace_zone": "E",
                                            "reason": "测试"}}


def _run(adjustments, ctx=None, extra=None):
    out = _out(adjustments, extra)
    return guardrails.validate(out, ctx or _ctx())


# ---- 规则 2：日期范围与目标存在性 ----

def test_date_out_of_range_dropped():
    items, log = _run([{"date": "2026-09-20", "planned_workout_id": 1, "action": "keep"}])
    assert items == []
    assert any("超出" in x for x in log)


def test_unknown_workout_dropped():
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 999, "action": "modify",
                        "changes": {"kind": "E"}}])
    assert items == []
    assert any("不存在" in x for x in log)


def test_date_mismatch_fixed_to_workout_date():
    items, log = _run([{"date": "2026-09-08", "planned_workout_id": 1, "action": "keep"}])
    assert len(items) == 1 and items[0]["date"] == "2026-09-07"


# ---- 规则 5：加练 ----

def test_add_easy_on_occupied_day_dropped():
    items, log = _run([{"date": "2026-09-08", "planned_workout_id": None,
                        "action": "add_easy"}], extra=_extra())
    assert items == []
    assert any("已有课" in x for x in log)


def test_add_easy_on_empty_slot_accepted():
    items, log = _run([{"date": "2026-09-11", "planned_workout_id": None,
                        "action": "add_easy"}], extra=_extra())
    assert len(items) == 1
    assert items[0]["changes"]["duration_min"] == 30.0
    assert items[0]["changes"]["kind"] == "E"


def test_add_easy_duration_clamped_to_45():
    items, _ = _run([{"date": "2026-09-11", "planned_workout_id": None,
                      "action": "add_easy"}], extra=_extra(duration=60.0))
    assert items[0]["changes"]["duration_min"] == 45.0


def test_add_easy_third_time_dropped():
    items, log = _run([{"date": "2026-09-11", "planned_workout_id": None,
                        "action": "add_easy"}],
                      ctx=_ctx(add_extra_count_this_week=2), extra=_extra())
    assert items == []
    assert any("上限" in x for x in log)


def test_add_easy_forbidden_last_3_days_before_race():
    ctx = _ctx(race_date=TODAY + timedelta(days=2))
    items, log = _run([{"date": "2026-09-11", "planned_workout_id": None,
                        "action": "add_easy"}], ctx=ctx, extra=_extra())
    assert items == []
    assert any("赛前 3 天" in x for x in log)


def test_add_easy_taper_window_only_recovery():
    ctx = _ctx(race_date=TODAY + timedelta(days=10))   # 14 天窗口内但不在最后 3 天
    items, log = _run([{"date": "2026-09-11", "planned_workout_id": None,
                        "action": "add_easy"}], ctx=ctx, extra=_extra(kind="E"))
    assert items == []
    assert any("只允许恢复跑" in x for x in log)
    items2, _ = _run([{"date": "2026-09-11", "planned_workout_id": None,
                       "action": "add_easy"}], ctx=ctx, extra=_extra(kind="RECOVERY"))
    assert len(items2) == 1


# ---- 规则 6：modify 距离 ±30% 与配速区 ----

def test_modify_distance_over_30pct_clamped():
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                        "action": "modify", "changes": {"distance_km": 20.0}}])
    assert items[0]["changes"]["distance_km"] == round(8.0 * 1.3, 1)
    assert any("30%" in x for x in log)


def test_modify_invalid_pace_zone_kept():
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                        "action": "modify", "changes": {"pace_zone": "X"}}])
    assert items[0]["changes"]["pace_zone"] == "E"
    assert any("非法 pace_zone" in x for x in log)


# ---- 规则 3：相邻强度日 ----

def test_modify_creating_adjacent_hard_dropped():
    items, log = _run([{"date": "2026-09-10", "planned_workout_id": 4,
                        "action": "modify", "changes": {"kind": "I", "distance_km": 6.0}}])
    assert items == []
    assert any("相邻" in x for x in log)


def test_rest_on_hard_day_ok():
    items, _ = _run([{"date": "2026-09-09", "planned_workout_id": 3, "action": "rest"}])
    assert len(items) == 1 and items[0]["action"] == "rest"


# ---- 规则 4：已有课扩容累计 ≤10% ----

def test_week_increase_over_10pct_dropped():
    # 三节课各自 +30% 钳制后累计增量 6.3km > 4km 上限 → 第三节被丢弃
    items, log = _run([
        {"date": "2026-09-07", "planned_workout_id": 1, "action": "modify",
         "changes": {"distance_km": 13.0}},   # 8→10.4 (+2.4)
        {"date": "2026-09-08", "planned_workout_id": 2, "action": "modify",
         "changes": {"distance_km": 8.0}},    # 5→6.5 (+1.5)
        {"date": "2026-09-10", "planned_workout_id": 4, "action": "modify",
         "changes": {"distance_km": 13.0}},   # 8→10.4 (+2.4)，累计 6.3 > 4
    ])
    assert len(items) == 2
    assert any("周量变化超" in x for x in log)


def test_decrease_never_blocked_by_week_rule():
    # 减量（休息 LR 14km，-35%）是安全方向，不应被周量规则拦截
    items, log = _run([{"date": "2026-09-13", "planned_workout_id": 7, "action": "rest"}])
    assert len(items) == 1
    assert not any("周量" in x for x in log)


# ---- 规则 7：赛前 14 天 ----

def test_taper_modify_to_hard_dropped():
    ctx = _ctx(race_date=TODAY + timedelta(days=7))
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                        "action": "modify", "changes": {"kind": "I"}}], ctx=ctx)
    assert items == []
    assert any("赛前 14 天" in x for x in log)


def test_taper_modify_to_easy_ok():
    ctx = _ctx(race_date=TODAY + timedelta(days=7))
    items, _ = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                      "action": "modify", "changes": {"kind": "RECOVERY"}}], ctx=ctx)
    assert len(items) == 1


def test_taper_shift_dropped():
    ctx = _ctx(race_date=TODAY + timedelta(days=7))
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                        "action": "shift", "changes": {"date": "2026-09-11"}}], ctx=ctx)
    assert items == []
    assert any("不可挪课" in x for x in log)


# ---- 强制模式（用户坚持要求改课：不拒绝，降级落地） ----

def test_force_taper_modify_to_hard_lands_as_easy():
    ctx = _ctx(race_date=TODAY + timedelta(days=7), force=True)
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                        "action": "modify", "changes": {"kind": "I", "distance_km": 6.0}}],
                      ctx=ctx)
    assert len(items) == 1
    assert items[0]["changes"]["kind"] == "E"
    assert any("强制模式" in x for x in log)


def test_force_adjacent_conflict_lands_as_easy():
    # 周四 E→I 与周三 T 相邻：非强制被丢弃（见 test_modify_creating_adjacent_hard_dropped），
    # 强制时降 E 落地而不是拒绝
    items, log = _run([{"date": "2026-09-10", "planned_workout_id": 4,
                        "action": "modify", "changes": {"kind": "I", "distance_km": 6.0}}],
                      ctx=_ctx(force=True))
    assert len(items) == 1
    assert items[0]["changes"]["kind"] == "E"
    assert any("相邻" in x for x in log)


def test_force_week_gain_over_10pct_lands():
    items, log = _run([
        {"date": "2026-09-07", "planned_workout_id": 1, "action": "modify",
         "changes": {"distance_km": 13.0}},   # 8→10.4 (+2.4)
        {"date": "2026-09-08", "planned_workout_id": 2, "action": "modify",
         "changes": {"distance_km": 8.0}},    # 5→6.5 (+1.5)
        {"date": "2026-09-10", "planned_workout_id": 4, "action": "modify",
         "changes": {"distance_km": 13.0}},   # 8→10.4 (+2.4)，累计 +6.3 > +4 上限
    ], ctx=_ctx(force=True))
    assert len(items) == 3
    assert any("豁免" in x for x in log)


def test_force_taper_shift_allowed():
    ctx = _ctx(race_date=TODAY + timedelta(days=7), force=True)
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                        "action": "shift", "changes": {"date": "2026-09-11"}}], ctx=ctx)
    assert len(items) == 1 and items[0]["action"] == "shift"


def test_force_shift_into_hard_gap_lands_as_easy():
    # 周六 I7 是强度日，把强度课挪到周五会与周六相邻 → 强制时降 E 挪入
    items, log = _run([{"date": "2026-09-09", "planned_workout_id": 3,
                        "action": "shift", "changes": {"date": "2026-09-11"}}], ctx=_ctx(force=True))
    assert len(items) == 1 and items[0]["action"] == "shift"
    assert any("降为 E" in x for x in log)


def test_force_add_easy_taper_lands_as_recovery():
    ctx = _ctx(race_date=TODAY + timedelta(days=10), force=True)   # 14 天窗口内、非最后 3 天
    items, log = _run([{"date": "2026-09-11", "planned_workout_id": None,
                        "action": "add_easy"}], ctx=ctx, extra=_extra(kind="E"))
    assert len(items) == 1
    assert items[0]["changes"]["kind"] == "RECOVERY"
    assert items[0]["changes"]["duration_min"] <= 30.0


def test_force_add_easy_last_3_days_lands_short_recovery():
    ctx = _ctx(race_date=TODAY + timedelta(days=2), force=True)   # race 09-09
    items, log = _run([{"date": "2026-09-11", "planned_workout_id": None,
                        "action": "add_easy"}], ctx=ctx, extra=_extra())
    assert len(items) == 1
    assert items[0]["changes"]["kind"] == "RECOVERY"
    assert items[0]["changes"]["duration_min"] == 20.0


def test_force_still_enforces_data_validity():
    # 强制模式只豁免训练学规则，不豁免数据合法性
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 999,
                        "action": "modify", "changes": {"kind": "E"}}], ctx=_ctx(force=True))
    assert items == []
    assert any("不存在" in x for x in log)
    ctx = _ctx(workouts=[dict(w, status="completed") if w["id"] == 1 else w
                         for w in WORKOUTS], force=True)
    items2, log2 = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                          "action": "rest"}], ctx=ctx)
    assert items2 == []
    assert any("已完成" in x for x in log2)


# ---- 规则 8：容量上限 ----

def test_modify_to_I_distance_capped():
    items, _ = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                      "action": "modify", "changes": {"kind": "I", "distance_km": 12.0}}])
    # 0.08 × 40 = 3.2km 上限
    assert items[0]["changes"]["distance_km"] == 3.2


def test_modify_to_T_duration_capped():
    items, _ = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                      "action": "modify", "changes": {"kind": "T", "duration_min": 90.0}}])
    assert items[0]["changes"]["duration_min"] == 50.0


# ---- 其他 ----

def test_shift_to_occupied_date_dropped():
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                        "action": "shift", "changes": {"date": "2026-09-09"}}])
    assert items == []
    assert any("已有课" in x for x in log)


def test_shift_to_empty_date_accepted():
    items, _ = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                      "action": "shift", "changes": {"date": "2026-09-11"}}])
    assert len(items) == 1


def test_completed_workout_not_adjustable():
    ctx = _ctx(workouts=[dict(w, status="completed") if w["id"] == 1 else w
                         for w in WORKOUTS])
    items, log = _run([{"date": "2026-09-07", "planned_workout_id": 1,
                        "action": "modify", "changes": {"kind": "E"}}], ctx=ctx)
    assert items == []
    assert any("已完成" in x for x in log)


def test_all_dropped_returns_empty():
    items, log = _run([
        {"date": "2026-09-20", "planned_workout_id": 1, "action": "keep"},
        {"date": "2026-09-08", "planned_workout_id": None, "action": "add_easy"},
    ], extra=_extra())
    assert items == []
    assert len(log) == 2
