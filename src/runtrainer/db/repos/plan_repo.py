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


def repair_recovery_paces(plan_id: int, slow_s_km: float, fast_s_km: float) -> int:
    """把计划内 kind=RECOVERY 的课原地对齐到恢复带（50–59%VDOT）。

    旧引擎把恢复课按 E 带（59–74%）落库导致配速过快；kind 是强度语义的
    权威，此处只更动这些行的配速字段（pace_zone/慢快两端），不触碰距离/
    时长/分段/状态/AI 调整。幂等：已对齐的行不重复写。返回修复行数。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id FROM planned_workouts WHERE plan_id = ? AND kind = 'RECOVERY'"
            " AND (pace_zone != 'RECOVERY' OR pace_slow_s_km != ? OR pace_fast_s_km != ?)",
            (plan_id, slow_s_km, fast_s_km))
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            marks = ", ".join("?" * len(ids))
            conn.execute(
                f"UPDATE planned_workouts SET pace_zone = 'RECOVERY',"
                f" pace_slow_s_km = ?, pace_fast_s_km = ? WHERE id IN ({marks})",
                (slow_s_km, fast_s_km, *ids))
    return len(ids)


def repace_workouts(plan_id: int, pace_map: dict, since_date: str | None = None,
                    include_ai: bool = False) -> list[dict]:
    """把课表里「引擎生成、尚未执行」的课的配速列对齐到新的 VDOT。

    走这条路径而不是整表重建：VDOT 微调不该把已排好的课日期/内容/分段洗牌。
    只动 pace_zone/pace_slow_s_km/pace_fast_s_km 三列，距离/时长/分段/状态
    一律不碰。AI 改过的课（source='ai'）默认跳过——那是教练按当天情况特意
    定的配速，不该被批量覆盖。返回被改动的行摘要（date/slot/zone/新旧配速）。
    """
    if not pace_map:
        return []
    changed: list[dict] = []
    with get_conn() as conn:
        sql = ("SELECT id, date, slot, title, pace_zone, pace_slow_s_km, pace_fast_s_km,"
               " source FROM planned_workouts WHERE plan_id = ?"
               " AND status NOT IN ('done', 'skipped') AND pace_zone IS NOT NULL")
        args: list = [plan_id]
        if since_date:
            sql += " AND date >= ?"
            args.append(since_date)
        if not include_ai:
            sql += " AND (source IS NULL OR source != 'ai')"
        for r in conn.execute(sql, args).fetchall():
            target = pace_map.get(r["pace_zone"])
            if not target:
                continue
            slow, fast = target
            if (r["pace_slow_s_km"] is not None
                    and abs(r["pace_slow_s_km"] - slow) < 1.0
                    and r["pace_fast_s_km"] is not None
                    and abs(r["pace_fast_s_km"] - fast) < 1.0):
                continue
            conn.execute(
                "UPDATE planned_workouts SET pace_slow_s_km = ?, pace_fast_s_km = ?"
                " WHERE id = ?", (slow, fast, r["id"]))
            changed.append({"id": r["id"], "date": r["date"], "slot": r["slot"],
                            "zone": r["pace_zone"], "title": r["title"],
                            "old_slow": r["pace_slow_s_km"], "new_slow": slow})
    return changed


def get_phase_weeks(plan: dict) -> dict:
    """phase_weeks JSON 列 → dict。"""
    return jsonutil.loads(plan["phase_weeks"]) or {}
