"""M3：计划服务端到端（向导上下文 → VDOT 选取 → 预览 → 落库 → 归档旧计划）。"""
from __future__ import annotations

from datetime import timedelta

from runtrainer.db.repos import activity_repo, goal_repo, plan_repo
from runtrainer.services import plan_service
from runtrainer.utils import dates


def _seed_race(days_ago: int, dist_m: int, duration_s: int, name: str = "5K 比赛",
               avg_hr: int = 178):
    d = dates.today() - timedelta(days=days_ago)
    activity_repo.upsert_activity({
        "source": "manual", "external_id": f"race-{d.isoformat()}", "name": name,
        "sport": "running", "start_ts": dates.date_to_ts(d), "tz_offset_min": 480,
        "duration_s": duration_s, "distance_m": dist_m,
        "avg_hr": avg_hr, "max_hr": 186, "has_samples": 0,
    })


def _params(race_in_days: int = 60, target_seconds=None, vdot=None, distance_m=5000,
            base_km=30.0):
    return {
        "goal": {
            "distance_m": distance_m,
            "race_date": (dates.today() + timedelta(days=race_in_days)).isoformat(),
            "target_seconds": target_seconds, "vdot": vdot, "name": "5K",
        },
        "plan": {"base_weekly_km": base_km},
    }


def test_wizard_context_picks_up_race():
    _seed_race(20, 5000, 22 * 60)
    ctx = plan_service.wizard_context()
    assert ctx["recent_vdot"] is not None
    assert ctx["recent_race"]["distance_m"] == 5000
    assert ctx["avg_weekly_km_4w"] > 0


def test_wizard_context_ignores_easy_runs():
    """低心率长距离不是比赛，不应影响 recent_vdot。"""
    _seed_race(20, 10000, 65 * 60, name="轻松慢跑", avg_hr=120)
    ctx = plan_service.wizard_context()
    assert ctx["recent_vdot"] is None


def test_create_plan_uses_race_result_vdot():
    _seed_race(30, 5000, 22 * 60)
    payload = plan_service.create_goal_and_plan(_params())
    # 水平预估已改为综合模型（ability），仅有一场比赛证据时等价于该成绩
    assert payload["vdot_source"] == "ability"
    assert payload["plan_id"] and payload["goal_id"]
    assert payload["total_weeks"] >= 8
    assert len(payload["workouts"]) >= payload["total_weeks"] * 5 - 1   # 比赛周少一练


def test_create_plan_persists_and_supersedes():
    _seed_race(30, 5000, 22 * 60)
    p1 = plan_service.create_goal_and_plan(_params())
    p2 = plan_service.create_goal_and_plan(_params())
    assert plan_repo.get_plan(p1["plan_id"])["status"] == "superseded"
    assert plan_repo.get_plan(p2["plan_id"])["status"] == "active"
    assert goal_repo.get_active_goal()["id"] == p2["goal_id"]
    ws = plan_repo.get_workouts(p2["plan_id"])
    assert any(w["kind"] == "RACE" and w["date"] == p2["race_date"] for w in ws)
    assert any(w["segments_json"] for w in ws)   # 结构化详情段落库
    # 目标成绩空缺 → target_seconds NULL，不影响
    assert goal_repo.get_active_goal()["target_seconds"] is None


def test_target_vdot_too_ambitious_warns():
    """有近期比赛时按当前水平配速；目标比当前高 2 以上要警告。"""
    _seed_race(30, 5000, 22 * 60)   # VDOT ≈ 44.5
    p = plan_service.preview_plan(_params(target_seconds=20 * 60))   # VDOT ≈ 50
    assert p["vdot_source"] == "ability"
    assert p["vdot"] < 50
    assert any("高 2" in w for w in p["warnings"])


def test_vo2max_fallback_and_plan_refresh():
    """无比赛时回退到手表 vo2max（经综合模型，单证据时等价读数）；
    同步后 VDOT 变化 → active 课表重建。"""
    from runtrainer.db.repos import profile_repo
    profile_repo.upsert_profile({"vo2max": 55.0})
    ctx = plan_service.wizard_context()
    assert ctx["recent_vdot"] == 55.0
    assert ctx["recent_vdot_source"] == "ability"
    assert ctx["ability"]["vdot"] == 55.0
    assert any(ev["source"] == "garmin_vo2max" for ev in ctx["ability"]["evidence"])

    p1 = plan_service.create_goal_and_plan(_params())
    assert p1["vdot"] == 55.0
    assert p1["vdot_source"] == "ability"
    assert plan_service.refresh_active_plan() is None   # 无变化不重建

    _seed_race(30, 5000, 22 * 60)   # 新比赛 → VDOT ≈ 44.5
    p2 = plan_service.refresh_active_plan()
    assert p2 is not None and p2["rebuilt"] is True
    assert plan_repo.get_plan(p1["plan_id"])["status"] == "superseded"
    assert plan_repo.get_plan(p2["plan_id"])["status"] == "active"


def test_manual_vdot_wins():
    p = plan_service.preview_plan(_params(vdot=52.0))
    assert p["vdot_source"] == "manual"
    assert p["vdot"] == 52.0


def test_marathon_min_weeks_warning():
    p = plan_service.preview_plan(_params(race_in_days=30, distance_m=42195, vdot=45))
    assert any("建议" in w for w in p["warnings"])


# ---------- 训练时期截断 ----------

INTERVAL_SEGS = [
    {"type": "recovery", "distance_m": 1600, "duration_s": 600, "pace_s_km": 375, "avg_hr": 130},
    {"type": "work", "distance_m": 1000, "duration_s": 250, "pace_s_km": 250, "avg_hr": 175},
    {"type": "rest", "distance_m": 400, "duration_s": 200, "pace_s_km": 500, "avg_hr": 140},
    {"type": "work", "distance_m": 1000, "duration_s": 250, "pace_s_km": 250, "avg_hr": 178},
    {"type": "rest", "distance_m": 400, "duration_s": 200, "pace_s_km": 500, "avg_hr": 145},
    {"type": "work", "distance_m": 1000, "duration_s": 250, "pace_s_km": 250, "avg_hr": 180},
    {"type": "recovery", "distance_m": 1600, "duration_s": 600, "pace_s_km": 375, "avg_hr": 135},
]


def _seed_interval(days_ago: int):
    import json
    d = dates.today() - timedelta(days=days_ago)
    activity_repo.upsert_activity({
        "source": "manual", "external_id": f"interval-{d.isoformat()}", "name": "间歇课",
        "sport": "running", "start_ts": dates.date_to_ts(d), "tz_offset_min": 480,
        "duration_s": 2100, "distance_m": 6000, "avg_pace_s_km": 300,
        "avg_hr": 158, "max_hr": 186, "has_samples": 1,
        "structure_json": json.dumps(INTERVAL_SEGS, ensure_ascii=False),
    })


def test_create_plan_with_manual_start_phase_truncates():
    _seed_race(30, 5000, 22 * 60)
    params = _params()
    params["plan"]["start_phase"] = "transition"
    payload = plan_service.create_goal_and_plan(params)
    assert payload["start_phase"] == "transition"
    assert payload["phase_weeks"]["base"] == 0 and payload["phase_weeks"]["early"] == 0
    plan = plan_repo.get_plan(payload["plan_id"])
    assert plan["start_phase"] == "transition"
    assert all(w["phase"] != "base" for w in plan_repo.get_workouts(payload["plan_id"]))


def test_create_plan_auto_phase_uses_recent_training():
    """近 4 周两节间歇课 → auto 智能判断为非基础期。"""
    _seed_race(30, 5000, 22 * 60)
    _seed_interval(10)
    _seed_interval(3)
    params = _params()
    params["plan"]["start_phase"] = "auto"
    payload = plan_service.create_goal_and_plan(params)
    assert payload["start_phase"] in ("transition", "final")
    assert any("智能判断" in w for w in payload["warnings"])


def test_invalid_start_phase_falls_back_full_cycle():
    _seed_race(30, 5000, 22 * 60)
    params = _params()
    params["plan"]["start_phase"] = "bogus"
    payload = plan_service.create_goal_and_plan(params)
    assert payload["start_phase"] is None
    assert payload["phase_weeks"]["base"] >= 1


def test_refresh_preserves_start_phase():
    _seed_race(30, 5000, 22 * 60)
    params = _params()
    params["plan"]["start_phase"] = "transition"
    p1 = plan_service.create_goal_and_plan(params)
    # 新比赛 → 水平变化 → 重建；时期起点应保留
    _seed_race(5, 5000, 21 * 60)
    p2 = plan_service.refresh_active_plan()
    assert p2 is not None
    assert p2["start_phase"] == "transition"
    assert plan_repo.get_plan(p2["plan_id"])["start_phase"] == "transition"


def test_wizard_context_has_phase_suggestion():
    ctx = plan_service.wizard_context()
    assert ctx["phase_suggestion"]["phase"] in ("base", "early", "transition", "final", "taper")
    assert isinstance(ctx["phase_suggestion"]["reasons"], list)
