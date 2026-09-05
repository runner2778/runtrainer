"""AI 教练聊天记录读写。"""
from __future__ import annotations

from datetime import datetime, timezone

from ...utils import jsonutil
from ..database import get_conn, row_to_dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_message(role: str, content: str, *, adjustment_ids: list[int] | None = None,
                   profile_updates: dict | None = None, model: str | None = None) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (role, content, adjustment_ids_json, "
            "profile_updates_json, model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (role, content,
             jsonutil.dumps(adjustment_ids) if adjustment_ids else None,
             jsonutil.dumps(profile_updates) if profile_updates else None,
             model, _now()),
        )
        return row_to_dict(conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_message(message_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone())


def list_messages(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,))]
