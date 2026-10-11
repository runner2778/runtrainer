"""预估 VDOT → 课表配速的同步：只动配速列、不重排课表。"""
from __future__ import annotations

from datetime import timedelta

from runtrainer.db.repos import activity_repo, goal_repo, plan_repo
from runtrainer.domain import vdot as vd
from runtrainer.services import plan_service
from runtrainer.utils import dates


def _seed_race(days_ago: int = 20, dist_m: int = 5000, duration_s: int = 22 * 60):
    d = dates.today() - timedelta(days=days_ago)
    activity_repo.upsert_activity({
        "source": "manual", "external_id": f"race-{d.isoformat()}", "name": "5K 比赛",
        "sport": "running", "start_ts": dates.date_to_ts(d), "tz_offset_min": 480,
        "duration_s": duration_s, "distance_m": dist_m,
        "avg_hr": 178, "max_hr": 186, "has_samples": 0,
    })


def _make_plan():
    _seed_race()
    return plan_service.create_goal_and_plan({
        "goal": {"distance_m": 5000,
                 "race_date": (dates.today() + timedelta(days=70)).isoformat(),
                 "name": "5K"},
        "plan": {"base_weekly_km": 30.0},
    })


def _plan_workouts(plan_id):
    return [w for w in plan_repo.get_workouts(plan_id)
            if w["status"] not in ("done", "skipped") and w.get("pace_zone")]


def test_status_reports_no_plan_without_active_plan():
    st = plan_service.pace_sync_status(target_vdot=50.0)
    assert st["available"] is False


def test_status_detects_drift_and_lists_samples():
    payload = _make_plan()
    plan_vdot = payload["vdot"]
    ws = _plan_workouts(payload["plan_id"])
    assert ws, "刚生成的课表应当有带配速区的课"

    same = plan_service.pace_sync_status(target_vdot=plan_vdot)
    assert same["available"] is True
    assert same["delta"] == 0.0
    assert same["needs_sync"] is False
    assert same["stale_workouts"] == 0
    assert same["total_workouts"] == len(ws)

    # 目标水平明显上调 → 未执行课的配速应当被判定为过时
    higher = plan_service.pace_sync_status(target_vdot=plan_vdot + 3.0)
    assert higher["needs_sync"] is True
    assert higher["stale_workouts"] > 0
    assert higher["delta"] == 3.0
    for s in higher["samples"]:
        assert s["new_slow"] < s["old_slow"]      # 水平更高 → 配速更快


def test_status_skips_ai_and_done_workouts():
    payload = _make_plan()
    pid = payload["plan_id"]
    ws = _plan_workouts(pid)
    assert len(ws) >= 2
    plan_repo.set_workout_status(ws[0]["id"], "done")
    ai_row = dict(ws[1]); ai_row["source"] = "ai"
    plan_repo.update_workout(ws[1]["id"], ai_row)

    st = plan_service.pace_sync_status(target_vdot=payload["vdot"] + 3.0)
    assert st["total_workouts"] == len(ws) - 2


def test_sync_rewrites_pace_columns_only():
    payload = _make_plan()
    pid = payload["plan_id"]
    before = {w["id"]: dict(w) for w in _plan_workouts(pid)}

    res = plan_service.sync_plan_paces(target_vdot=payload["vdot"] + 3.0)
    assert res["changed"] is True
    assert res["reason"] == "synced"
    assert res["repaced"] > 0

    pm = plan_service._pace_map(payload["vdot"] + 3.0)
    after = {w["id"]: dict(w) for w in _plan_workouts(pid)}
    assert set(before) == set(after), "课表行数不该变"
    touched = 0
    for wid, b in before.items():
        a = after[wid]
        # 结构性字段一律未动
        for k in ("date", "slot", "title", "description", "distance_km",
                  "duration_min", "pace_zone", "kind", "status", "segments_json"):
            assert a[k] == b[k], f"{k} 被改动了"
        tgt = pm.get(a["pace_zone"])
        if tgt:
            assert a["pace_slow_s_km"] == tgt[0]
            assert a["pace_fast_s_km"] == tgt[1]
            if b["pace_slow_s_km"] != tgt[0]:
                touched += 1
    assert touched > 0


def test_sync_updates_plan_and_goal_vdot():
    payload = _make_plan()
    target = payload["vdot"] + 3.0
    plan_service.sync_plan_paces(target_vdot=target)
    assert plan_repo.get_plan(payload["plan_id"])["vdot"] == round(target, 2)
    goal = goal_repo.get_goal(payload["goal_id"])
    assert goal["vdot"] == round(target, 2)
    assert goal["vdot_source"] == "ability"


def test_sync_is_idempotent_when_in_sync():
    payload = _make_plan()
    target = payload["vdot"] + 3.0
    first = plan_service.sync_plan_paces(target_vdot=target)
    assert first["changed"] is True
    again = plan_service.sync_plan_paces(target_vdot=target)
    assert again["changed"] is False
    assert again["reason"] == "in_sync"


def test_sync_refuses_when_goal_vdot_is_locked():
    payload = _make_plan()
    goal_repo.update_goal(payload["goal_id"], {"vdot_source": "manual",
                                               "vdot": payload["vdot"]})
    res = plan_service.sync_plan_paces(target_vdot=payload["vdot"] + 3.0)
    assert res["changed"] is False and res["reason"] == "locked"
    # 显式 force 才覆盖
    forced = plan_service.sync_plan_paces(target_vdot=payload["vdot"] + 3.0, force=True)
    assert forced["changed"] is True


def test_sync_reports_no_plan_and_no_estimate():
    assert plan_service.sync_plan_paces()["reason"] == "no_plan"
    _make_plan()
    # 无证据可依、也无显式目标 → 无法定目标水平
    res = plan_service.sync_plan_paces()
    assert res["changed"] is False
    assert res["reason"] in ("no_estimate", "in_sync", "locked")


def test_sync_include_past_reaches_past_workouts():
    payload = _make_plan()
    pid = payload["plan_id"]
    past = [w for w in _plan_workouts(pid) if w["date"] < dates.today().isoformat()]
    if not past:
        return          # 计划还没开始跑，此场景不适用
    target = payload["vdot"] + 3.0
    plan_service.sync_plan_paces(target_vdot=target, include_past=True)
    w = plan_repo.get_workout(past[0]["id"])
    pm = plan_service._pace_map(target)
    assert w["pace_slow_s_km"] == pm[w["pace_zone"]][0]


def test_pace_map_matches_vdot_table():
    pm = plan_service._pace_map(50.0)
    table = vd.pace_table(50.0)
    for zone, (slow, fast) in pm.items():
        # slow 是「慢端」，秒数更大（越慢每公里用时越长）
        assert slow >= fast, f"{zone} 慢端应不慢于快端（秒数应更大）"
        assert zone in table
        cell = table[zone]
        if isinstance(cell, dict):
            assert (slow, fast) == (cell["slow_s_km"], cell["fast_s_km"])
        else:                      # 单值档（恢复/热身等）两端同速
            assert slow == fast == cell
