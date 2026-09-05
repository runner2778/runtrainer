"""训练计划与课表读写。"""
from __future__ import annotations

from datetime import datetime, timezone

from ...utils import jsonutil
from ..database import get_conn, row_to_dict

WORKOUT_FIELDS = (
    "plan_id", "date", "slot", "week_index", "phase", "kind", "title", "description",
    "distance_km", "duration_min", "pace_zone", "pace_slow_s_km", "pace_fast_s_km",
    "target_hr_zone", "source", "adjustment_id", "status", "completed_activity_id",
    "segments_json",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _workout_to_row(w: dict) -> tuple:
    return tuple(w.get(f) for f in WORKOUT_FIELDS)


def create_plan(plan: dict, workouts: list[dict]) -> dict:
    """事务内创建计划 + 批量写入课表。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO training_plans (goal_id, start_date, race_date, total_weeks, phase_weeks, vdot, "
            "base_weekly_km, peak_weekly_km, run_days, long_run_weekday, engine_version, start_phase, "
            "double_days, double_mode, strength_days, pro_mode, "
            "status, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan["goal_id"], plan["start_date"], plan["race_date"], plan["total_weeks"],
                jsonutil.dumps(plan["phase_weeks"]), plan["vdot"], plan["base_weekly_km"],
                plan["peak_weekly_km"], plan["run_days"], plan["long_run_weekday"],
                plan["engine_version"], plan.get("start_phase"),
                plan.get("double_days") or 0, plan.get("double_mode") or "auto",
                plan.get("strength_days") or 0, int(plan.get("pro_mode") or 0),
                "active", _now(),
            ),
        )
        plan_id = cur.lastrowid
        placeholders = ", ".join("?" for _ in WORKOUT_FIELDS)
        rows = []
        for w in workouts:
            w = dict(w)
            w["plan_id"] = plan_id
            w.setdefault("slot", 1)
            w.setdefault("source", "engine")
            w.setdefault("status", "planned")
            rows.append(_workout_to_row(w))
        conn.executemany(f"INSERT INTO planned_workouts ({', '.join(WORKOUT_FIELDS)}) VALUES ({placeholders})", rows)
        return dict(conn.execute("SELECT * FROM training_plans WHERE id = ?", (plan_id,)).fetchone())


def get_plan(plan_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM training_plans WHERE id = ?", (plan_id,)).fetchone())


def get_active_plan() -> dict | None:
    with get_conn() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM training_plans WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone()
        )


def list_plans(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM training_plans ORDER BY id DESC LIMIT ?", (limit,))]


def set_plan_status(plan_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE training_plans SET status = ? WHERE id = ?", (status, plan_id))


def get_workouts(plan_id: int, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    sql = "SELECT * FROM planned_workouts WHERE plan_id = ?"
    args: list = [plan_id]
    if start_date:
        sql += " AND date >= ?"
        args.append(start_date)
    if end_date:
        sql += " AND date <= ?"
        args.append(end_date)
    sql += " ORDER BY date, slot"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def get_workout(workout_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM planned_workouts WHERE id = ?", (workout_id,)).fetchone())


def get_workout_by_date(plan_id: int, date: str) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM planned_workouts WHERE plan_id = ? AND date = ? ORDER BY slot LIMIT 1",
                (plan_id, date),
            ).fetchone()
        )


def upsert_workout(w: dict) -> int:
    """AI 调整/手动编辑后写入；同 (plan_id, date, slot) 覆盖。返回 id。

    与 activity upsert 同理，不用 ON CONFLICT，显式 SELECT 再写。
    """
    w = dict(w)
    w.setdefault("slot", 1)
    w.setdefault("source", "ai")
    w.setdefault("status", "planned")
    placeholders = ", ".join("?" for _ in WORKOUT_FIELDS)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM planned_workouts WHERE plan_id = ? AND date = ? AND slot = ?",
            (w["plan_id"], w["date"], w["slot"]),
        ).fetchone()
        if existing:
            wid = existing[0]
            updates = ", ".join(f"{f} = ?" for f in WORKOUT_FIELDS
                                if f not in ("plan_id", "date", "slot"))
            conn.execute(
                f"UPDATE planned_workouts SET {updates} WHERE id = ?",
                tuple(w.get(f) for f in WORKOUT_FIELDS if f not in ("plan_id", "date", "slot")) + (wid,),
            )
            return wid
        cur = conn.execute(
            f"INSERT INTO planned_workouts ({', '.join(WORKOUT_FIELDS)}) VALUES ({placeholders})",
            _workout_to_row(w),
        )
        return cur.lastrowid


def set_workout_status(workout_id: int, status: str, completed_activity_id: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE planned_workouts SET status = ?, completed_activity_id = ? WHERE id = ?",
            (status, completed_activity_id, workout_id),
        )


def update_workout(workout_id: int, w: dict) -> None:
    """按 id 覆盖课表字段（AI shift 挪日期等场景）。"""
    fields = [f for f in WORKOUT_FIELDS if f not in ("plan_id",)]
    updates = ", ".join(f"{f} = ?" for f in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE planned_workouts SET {updates} WHERE id = ?",
            tuple(w.get(f) for f in fields) + (workout_id,),
        )


def get_phase_weeks(plan: dict) -> dict:
    """phase_weeks JSON 列 → dict。"""
    return jsonutil.loads(plan["phase_weeks"]) or {}
