"""仪表盘聚合服务（dashboard_service.get_dashboard + bridge.get_dashboard）。"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from runtrainer.api.bridge import Api
from runtrainer.db.repos import (activity_repo, chat_repo, health_repo, kv_repo,
                                 plan_repo, sync_repo)
from runtrainer.services import dashboard_service, plan_service
from runtrainer.utils import dates

REAL_TODAY = dates.today()


@pytest.fixture()
def plan():
    """生成一个 12 周半马计划（从明天起），返回 (plan, workouts)。"""
    race = REAL_TODAY + timedelta(days=84)
    payload = plan_service.create_goal_and_plan({
        "goal": {"distance_m": 21097, "race_date": race.isoformat(),
                 "target_seconds": None, "vdot": 45.0, "name": "半马"},
        "plan": {"base_weekly_km": 40.0},
    })
    p = plan_repo.get_plan(payload["plan_id"])
    return p, plan_repo.get_workouts(p["id"])


def _patch_today(monkeypatch, d):
    monkeypatch.setattr("runtrainer.utils.dates.today", lambda: d)


def _insert_activity(day, km: float, external_id: str) -> None:
    activity_repo.upsert_activity({
        "source": "garmin", "external_id": external_id, "file_path": None,
        "name": "晨跑", "sport": "跑步",
        "start_ts": dates.date_to_ts(day) + 18 * 3600, "tz_offset_min": 0,
        "duration_s": int(km * 330), "distance_m": km * 1000,
        "avg_pace_s_km": 330.0, "avg_hr": 150.0, "laps_json": None,
        "has_samples": 0,
    })


def _insert_health(day, **fields) -> None:
    health_repo.upsert_daily_health(day.isoformat(), fields)


def test_empty_db_no_plan(monkeypatch):
    d = dashboard_service.get_dashboard()
    assert d["has_plan"] is False
    assert d["readiness"]["status"] == "unknown"
    assert d["readiness"]["note"]  # 引导同步 Garmin
    assert len(d["weekly_series"]) == 8
    assert all(w["done_km"] == 0 and w["planned_km"] == 0 for w in d["weekly_series"])
    assert d["week_load"] is None and d["race"] is None and d["kpis"] is None
    assert d["today_workouts"] == []
    assert d["coach"]["advice"] is None and d["coach"]["last_chat"] is None
    assert d["sync"]["last_sync_ts"] is None


def test_with_plan_and_data(monkeypatch, plan):
    p, ws = plan
    # 取计划内一个真实课日（有跑量的课）作为「今天」
    run_day = next(w for w in ws if (w.get("distance_km") or 0) > 0)
    today = dates.date.fromisoformat(run_day["date"])
    _patch_today(monkeypatch, today)

    _insert_health(today, sleep_score=85, hrv_avg_ms=62.0, hrv_status="balanced",
                   resting_hr=52, sleep_duration_s=8 * 3600)
    _insert_health(today - timedelta(days=1), sleep_score=70, hrv_avg_ms=55.0,
                   hrv_status="unbalanced", resting_hr=54)
    _insert_activity(today, 8.0, "act-today")
    _insert_activity(today - timedelta(days=3), 6.0, "act-3d")
    _insert_activity(today - timedelta(days=14), 10.0, "act-14d")
    chat_repo.create_message("coach", "今天状态不错，按计划执行！")
    sync_repo.set_sync_state("garmin", meta={"last_stats": {"activities": 1}})

    d = dashboard_service.get_dashboard()
    assert d["has_plan"] is True
    # 比赛倒计时
    assert d["race"]["days_left"] > 0
    assert d["race"]["name"] == "半马"
    assert 0 < d["race"]["progress_pct"] < 100
    # 恢复度：三指标全 good → good；日期为今天
    assert d["readiness"]["status"] == "good"
    assert d["readiness"]["date"] == today.isoformat()
    assert [i["key"] for i in d["readiness"]["items"]] == ["sleep", "hrv", "resting_hr"]
    # 本周负荷：按周一~周日窗口算期望（today-3d 是否同周取决于今天是周几）
    ws = today - timedelta(days=today.weekday())
    runs = [(today, 8.0), (today - timedelta(days=3), 6.0)]
    same_week_km = sum(km for day, km in runs if ws <= day <= ws + timedelta(days=6))
    assert d["week_load"]["done_km"] == round(same_week_km, 1)
    assert d["week_load"]["planned_km"] >= d["week_load"]["done_km"]
    assert d["week_load"]["pct"] is not None
    # 今日训练：今天有课
    assert d["today_workouts"], "今天应有一节计划课"
    assert d["today_workouts"][0]["kind"] == run_day["kind"]
    # KPI：ACWR 存在（今天+3 天前急性 vs 含 14 天前慢性）；完成度已算
    assert d["kpis"]["acwr"] is not None
    assert d["kpis"]["compliance_7d"]["planned_km"] > 0
    # 周序列：与服务同款的周桶公式算期望，逐桶核对
    start = ws - timedelta(days=7 * 7)
    expected = {7: 8.0}
    for day, km in [(today - timedelta(days=3), 6.0), (today - timedelta(days=14), 10.0)]:
        b = (day - start).days // 7
        expected[b] = expected.get(b, 0) + km
    assert d["weekly_series"][-1]["current"] is True
    for b, km in expected.items():
        assert d["weekly_series"][b]["done_km"] == round(km, 1)
    # 健康趋势与教练/同步块
    assert len(d["health_trend"]) == 2
    assert d["health_trend"][-1]["hrv"] == 62.0
    assert d["coach"]["last_chat"]["role"] == "coach"
    assert "状态不错" in d["coach"]["last_chat"]["content"]
    assert d["sync"]["last_stats"]["activities"] == 1
    assert d["sync"]["last_sync_ts"] is not None


def test_readiness_worst_item_wins(monkeypatch, plan):
    today = REAL_TODAY + timedelta(days=1)
    _patch_today(monkeypatch, today)
    # 睡眠好但 HRV 偏低 → 合成 low（取最差项）
    _insert_health(REAL_TODAY, sleep_score=90, hrv_avg_ms=30.0, hrv_status="low",
                   resting_hr=55)
    d = dashboard_service.get_dashboard()
    assert d["readiness"]["status"] == "low"
    assert d["readiness"]["label"] == "需要恢复"
    # 无 HRV 只有睡眠 → 按评分定（今天单独一行，不受昨天低 HRV 影响）
    _insert_health(today, sleep_score=50)
    d = dashboard_service.get_dashboard()
    assert d["readiness"]["status"] == "low"
    health_repo.upsert_daily_health(today.isoformat(), {"sleep_score": 95})
    d = dashboard_service.get_dashboard()
    assert d["readiness"]["status"] == "good"


def test_readiness_rhr_against_baseline(monkeypatch):
    _patch_today(monkeypatch, REAL_TODAY + timedelta(days=1))
    for i in range(5):
        _insert_health(REAL_TODAY - timedelta(days=i + 1), resting_hr=50)
    # 今天静息心率 +10（>基线+8）→ low
    _insert_health(REAL_TODAY, resting_hr=60)
    d = dashboard_service.get_dashboard()
    rhr_item = next(i for i in d["readiness"]["items"] if i["key"] == "resting_hr")
    assert rhr_item["status"] == "low"
    assert d["readiness"]["status"] == "low"
    # 回落到基线附近 → good
    health_repo.upsert_daily_health(REAL_TODAY.isoformat(), {"resting_hr": 51})
    d = dashboard_service.get_dashboard()
    assert next(i for i in d["readiness"]["items"] if i["key"] == "resting_hr")["status"] == "good"


def test_coach_advice_from_today_cache(monkeypatch):
    today = REAL_TODAY + timedelta(days=1)
    _patch_today(monkeypatch, today)
    kv_repo.set_app_state(f"coach:{today.isoformat()}", json.dumps({
        "summary": "状态良好，今日轻松跑 40 分钟。", "readiness": "good",
        "key_signals": ["睡眠充足", "HRV 平衡"], "ids": [],
    }))
    d = dashboard_service.get_dashboard()
    assert d["coach"]["advice"]["summary"] == "状态良好，今日轻松跑 40 分钟。"
    assert d["coach"]["advice"]["key_signals"] == ["睡眠充足", "HRV 平衡"]
    # 未缓存的日子 → None（避免每天空跑 AI）
    _patch_today(monkeypatch, today + timedelta(days=1))
    d = dashboard_service.get_dashboard()
    assert d["coach"]["advice"] is None


def test_ability_30d_from_profile_vo2max(monkeypatch):
    """档案有手表 VO2max + max_hr 但近 30 天无跑步 → 手表读数为唯一依据。"""
    _patch_today(monkeypatch, REAL_TODAY + timedelta(days=1))
    from runtrainer.db.repos import profile_repo
    profile_repo.upsert_profile({"vo2max": 50.0, "max_hr": 185})
    d = dashboard_service.get_dashboard()
    ab = d["ability_30d"]
    assert ab["window_days"] == 30
    assert ab["plan_vdot"] is None          # 无计划
    assert ab["vdot"] == 50.0
    assert ab["as_of"] == (REAL_TODAY + timedelta(days=1)).isoformat()
    assert ab["max_hr"] == 185
    assert ab["note"] is None
    assert ab["predictions"], "有 vdot 就必须有等效成绩"
    assert ab["predictions"]["5K"] > 0
    assert ab["evidence"][0]["source"] == "garmin_vo2max"
    assert ab["evidence"][0]["vdot"] == 50.0


def test_ability_30d_insufficient_data(monkeypatch):
    """空库（无档案/无活动/无健康）→ vdot None + 提示语。"""
    d = dashboard_service.get_dashboard()
    ab = d["ability_30d"]
    assert ab["vdot"] is None
    assert ab["predictions"] is None
    assert ab["evidence"] == []
    assert ab["note"] and "不足" in ab["note"]


def test_ability_30d_plan_vdot_ref(monkeypatch, plan):
    """有课表时带出对照用 plan_vdot（课表 VDOT 45），供前端做水平差提示。"""
    p, _ = plan
    _patch_today(monkeypatch, REAL_TODAY + timedelta(days=1))
    from runtrainer.db.repos import profile_repo
    profile_repo.upsert_profile({"vo2max": 48.0})
    d = dashboard_service.get_dashboard()
    ab = d["ability_30d"]
    assert d["has_plan"] is True
    assert ab["plan_vdot"] == 45.0
    assert ab["vdot"] == 48.0


def test_bridge_get_dashboard(monkeypatch, plan):
    _patch_today(monkeypatch, REAL_TODAY + timedelta(days=1))
    res = Api().get_dashboard()
    assert res["ok"] is True
    assert res["data"]["has_plan"] is True
    assert res["data"]["race"]["name"] == "半马"
