"""M0：数据库迁移与 repos 读写测试。"""
import pytest

from runtrainer.db import database
from runtrainer.db.repos import (
    activity_repo, adjustment_repo, goal_repo, health_repo, kv_repo,
    plan_repo, profile_repo, sync_repo,
)


def _activity(**kw):
    base = {
        "source": "fit", "external_id": "abc123", "name": "晨跑", "sport": "running",
        "start_ts": 1725400000, "tz_offset_min": 480, "duration_s": 3000,
        "distance_m": 10000, "avg_pace_s_km": 300, "avg_hr": 145, "max_hr": 172,
        "has_samples": 0,
    }
    base.update(kw)
    return base


class TestMigration:
    def test_migrate_idempotent(self):
        database.migrate()  # fixture 已执行过一次
        with database.get_conn() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in ("profile", "goals", "training_plans", "planned_workouts",
                  "activities", "activity_samples", "daily_health", "adjustments",
                  "sync_state", "settings", "app_state"):
            assert t in tables


class TestProfile:
    def test_upsert_and_get(self):
        p = profile_repo.upsert_profile({"nickname": "小明", "birth_year": 1992, "max_hr": 186})
        assert p["nickname"] == "小明"
        p2 = profile_repo.upsert_profile({"weight_kg": 68.5})
        assert p2["nickname"] == "小明"  # 未提供的字段保留
        assert p2["weight_kg"] == 68.5

    def test_unknown_field_ignored(self):
        p = profile_repo.upsert_profile({"nickname": "x", "hacker_field": 1})
        assert "hacker_field" not in p


class TestGoal:
    def test_active_goal_archives_previous(self):
        g1 = goal_repo.create_goal({"distance_m": 21097, "race_date": "2026-11-15"})
        g2 = goal_repo.create_goal({"distance_m": 42195, "race_date": "2027-03-01"})
        assert goal_repo.get_goal(g1["id"])["status"] == "archived"
        assert goal_repo.get_active_goal()["id"] == g2["id"]


class TestPlan:
    def _mk_plan(self):
        plan = plan_repo.create_plan({
            "goal_id": 1, "start_date": "2026-09-04", "race_date": "2026-11-15",
            "total_weeks": 12, "phase_weeks": {"base": 4, "early": 3, "transition": 2,
                                               "final": 2, "taper": 1},
            "vdot": 48.0, "base_weekly_km": 40, "peak_weekly_km": 70,
            "run_days": 6, "long_run_weekday": 6, "engine_version": "1.0.0",
        }, [
            {"date": "2026-09-04", "week_index": 0, "phase": "base", "kind": "E",
             "title": "轻松跑", "distance_km": 8},
            {"date": "2026-09-05", "week_index": 0, "phase": "base", "kind": "T",
             "title": "阈值跑", "distance_km": 12, "duration_min": 60},
        ])
        return plan

    def test_create_and_read(self):
        goal_repo.create_goal({"distance_m": 42195, "race_date": "2026-11-15"})
        plan = self._mk_plan()
        assert plan["id"] == 1
        ws = plan_repo.get_workouts(plan["id"])
        assert len(ws) == 2
        assert ws[0]["kind"] == "E"

    def test_unique_plan_date(self):
        """同一计划内 (plan_id, date) 唯一：直接 INSERT 应触发约束。"""
        goal_repo.create_goal({"distance_m": 42195, "race_date": "2026-11-15"})
        plan = self._mk_plan()
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            with database.get_conn() as conn:
                conn.execute(
                    "INSERT INTO planned_workouts (plan_id, date, week_index, phase, kind, title) "
                    "VALUES (?, ?, 0, 'base', 'E', 'x')",
                    (plan["id"], "2026-09-04"),
                )

    def test_upsert_workout_overwrites(self):
        goal_repo.create_goal({"distance_m": 42195, "race_date": "2026-11-15"})
        plan = self._mk_plan()
        wid = plan_repo.upsert_workout({
            "plan_id": plan["id"], "date": "2026-09-04", "week_index": 0,
            "phase": "base", "kind": "RECOVERY", "title": "AI 改为恢复跑",
            "source": "ai", "adjustment_id": 1,
        })
        ws = plan_repo.get_workouts(plan["id"])
        assert len(ws) == 2  # 覆盖而非新增
        w = plan_repo.get_workout_by_date(plan["id"], "2026-09-04")
        assert w["kind"] == "RECOVERY"
        assert w["source"] == "ai"

    def test_workout_status(self):
        goal_repo.create_goal({"distance_m": 42195, "race_date": "2026-11-15"})
        plan = self._mk_plan()
        w = plan_repo.get_workout_by_date(plan["id"], "2026-09-04")
        plan_repo.set_workout_status(w["id"], "completed", 9)
        w2 = plan_repo.get_workout(w["id"])
        assert w2["status"] == "completed"
        assert w2["completed_activity_id"] == 9


class TestActivity:
    def test_dedupe_upsert(self):
        aid1, created1 = activity_repo.upsert_activity(_activity())
        assert created1 is True
        aid2, created2 = activity_repo.upsert_activity(_activity(name="改名"))
        assert created2 is False
        assert aid1 == aid2
        assert activity_repo.count_activities() == 1
        assert activity_repo.get_activity(aid1)["name"] == "改名"

    def test_samples_save_and_get(self):
        aid, _ = activity_repo.upsert_activity(_activity())
        activity_repo.save_samples(aid, [(0, 100, 2.5, 170, 10), (1, 105, 2.6, 171, 11)])
        rows = activity_repo.get_samples(aid)
        assert len(rows) == 2
        assert rows[0]["seq"] == 0 and rows[1]["seq"] == 1
        assert activity_repo.get_activity(aid)["has_samples"] == 1
        # 全量覆盖
        activity_repo.save_samples(aid, [(0, 99, 2.0, 165, 9)])
        assert len(activity_repo.get_samples(aid)) == 1


class TestHealth:
    def test_merge_upsert_preserves_existing(self):
        health_repo.upsert_daily_health("2026-09-01", {"sleep_duration_s": 25200, "hrv_avg_ms": 55})
        health_repo.upsert_daily_health("2026-09-01", {"sleep_duration_s": 26000, "resting_hr": 52})
        h = health_repo.get_health_for_date("2026-09-01")
        assert h["sleep_duration_s"] == 26000  # 新值覆盖
        assert h["hrv_avg_ms"] == 55  # 旧值保留
        assert h["resting_hr"] == 52

    def test_range_query(self):
        for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
            health_repo.upsert_daily_health(d, {"steps": 8000})
        assert len(health_repo.get_health("2026-09-01", "2026-09-02")) == 2


class TestAdjustment:
    def test_create_decide(self):
        goal_repo.create_goal({"distance_m": 42195, "race_date": "2026-11-15"})
        plan = plan_repo.create_plan({
            "goal_id": 1, "start_date": "2026-09-04", "race_date": "2026-11-15",
            "total_weeks": 12, "phase_weeks": {}, "vdot": 48.0, "base_weekly_km": 40,
            "peak_weekly_km": 70, "run_days": 6, "long_run_weekday": 6,
            "engine_version": "1.0.0",
        }, [])
        adj = adjustment_repo.create_adjustment({
            "plan_id": plan["id"], "applies_date": "2026-09-05", "action": "rest",
            "reason": "HRV 偏低", "changes_json": {"kind": "RECOVERY"},
            "ai_output_json": {"summary": "休息"},
        })
        assert adj["status"] == "pending"
        assert adjustment_repo.get_adjustment(adj["id"])["reason"] == "HRV 偏低"
        d = adjustment_repo.decide_adjustment(adj["id"], "approved")
        assert d["status"] == "approved" and d["decided_at"] is not None
        assert len(adjustment_repo.list_adjustments(plan_id=plan["id"])) == 1


class TestSyncAndKv:
    def test_sync_state(self):
        sync_repo.set_sync_state("garmin", {"since": 12345})
        s = sync_repo.get_sync_state("garmin")
        assert s["meta_json"] == '{"since": 12345}'
        sync_repo.set_sync_state("garmin", error="超时")
        assert sync_repo.get_sync_state("garmin")["last_error"] == "超时"
        # 新错误覆盖旧 meta
        assert sync_repo.get_sync_state("garmin")["meta_json"] is None

    def test_record_sync_error_preserves_meta(self):
        sync_repo.set_sync_state("garmin", {"cursor_ts": 111})
        ts = sync_repo.get_sync_state("garmin")["last_sync_ts"]
        sync_repo.record_sync_error("garmin", "登录失败")
        s = sync_repo.get_sync_state("garmin")
        assert s["last_error"] == "登录失败"
        assert s["meta_json"] == '{"cursor_ts": 111}'  # 失败不推进游标
        assert s["last_sync_ts"] >= ts  # 尝试时间刷新

    def test_kv(self):
        assert kv_repo.get_setting("theme") is None
        kv_repo.set_setting("theme", "dark")
        assert kv_repo.get_setting("theme") == "dark"
        kv_repo.set_app_state("cache_key", "x")
        assert kv_repo.get_app_state("cache_key") == "x"
        kv_repo.delete_app_state("cache_key")
        assert kv_repo.get_app_state("cache_key") is None
