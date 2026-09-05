"""训练目标读写。"""
from __future__ import annotations

from datetime import datetime, timezone

from ..database import get_conn, row_to_dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_goal(goal: dict) -> dict:
    """创建目标；若为 active，先归档其他 active 目标。"""
    with get_conn() as conn:
        if goal.get("status", "active") == "active":
            conn.execute("UPDATE goals SET status = 'archived' WHERE status = 'active'")
        cur = conn.execute(
            "INSERT INTO goals (distance_m, target_seconds, race_date, status, vdot, vdot_source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                goal["distance_m"],
                goal.get("target_seconds"),
                goal["race_date"],
                goal.get("status", "active"),
                goal.get("vdot"),
                goal.get("vdot_source"),
                _now(),
            ),
        )
        return row_to_dict(conn.execute("SELECT * FROM goals WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_active_goal() -> dict | None:
    with get_conn() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM goals WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone()
        )


def get_goal(goal_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())


def list_goals(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM goals ORDER BY id DESC LIMIT ?", (limit,))]


def update_goal(goal_id: int, fields: dict) -> dict | None:
    allowed = {"distance_m", "target_seconds", "race_date", "status", "vdot", "vdot_source"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return get_goal(goal_id)
    sets = ", ".join(f"{k} = ?" for k in data)
    with get_conn() as conn:
        conn.execute(f"UPDATE goals SET {sets} WHERE id = ?", (*data.values(), goal_id))
        return row_to_dict(conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())
