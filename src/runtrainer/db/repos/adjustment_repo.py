"""AI 调整建议审计记录读写。"""
from __future__ import annotations

from datetime import datetime, timezone

from ...utils import jsonutil
from ..database import get_conn, row_to_dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_adjustment(adj: dict) -> dict:
    """写入一条调整建议（status=pending）。JSON 字段自动序列化。"""
    data = dict(adj)
    for k in ("changes_json", "ai_input_json", "ai_output_json", "guardrail_log_json"):
        if k in data and not isinstance(data[k], str):
            data[k] = jsonutil.dumps(data[k])
    data.setdefault("status", "pending")
    data.setdefault("created_at", _now())
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO adjustments ({cols}) VALUES ({placeholders})", tuple(data.values())
        )
        return row_to_dict(conn.execute("SELECT * FROM adjustments WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_adjustment(adj_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM adjustments WHERE id = ?", (adj_id,)).fetchone())


def list_adjustments(plan_id: int | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM adjustments WHERE 1=1"
    args: list = []
    if plan_id is not None:
        sql += " AND plan_id = ?"
        args.append(plan_id)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def decide_adjustment(adj_id: int, status: str) -> dict | None:
    """approved / rejected，记录决定时间。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE adjustments SET status = ?, decided_at = ? WHERE id = ?",
            (status, _now(), adj_id),
        )
        return row_to_dict(conn.execute("SELECT * FROM adjustments WHERE id = ?", (adj_id,)).fetchone())


def set_applied(adj_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE adjustments SET status = 'applied', decided_at = ? WHERE id = ?", (_now(), adj_id))
