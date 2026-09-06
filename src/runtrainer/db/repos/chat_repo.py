"""AI 教练聊天记录读写。"""
from __future__ import annotations

from datetime import datetime, timezone

from ...utils import jsonutil
from ..database import get_conn, row_to_dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_message(role: str, content: str, *, adjustment_ids: list[int] | None = None,
                   profile_updates: dict | None = None, model: str | None = None,
                   kind: str = "chat") -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (role, content, adjustment_ids_json, "
            "profile_updates_json, model, kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (role, content,
             jsonutil.dumps(adjustment_ids) if adjustment_ids else None,
             jsonutil.dumps(profile_updates) if profile_updates else None,
             model, kind, _now()),
        )
        return row_to_dict(conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_message(message_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone())


def list_messages(limit: int = 100, visible_only: bool = False) -> list[dict]:
    """消息列表（id 倒序）。

    visible_only=True：只取未被「清空对话」隐藏的消息（UI 展示用）；
    visible_only=False：全部消息（AI 上下文用——清空对话后教练仍记得之前的交流）。
    """
    where = " WHERE hidden = 0" if visible_only else ""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM chat_messages{where} ORDER BY id DESC LIMIT ?", (limit,))]


def hide_all_messages() -> int:
    """清空对话（保留记忆）：全部消息软隐藏，AI 上下文仍可读取。返回隐藏条数。"""
    with get_conn() as conn:
        cur = conn.execute("UPDATE chat_messages SET hidden = 1 WHERE hidden = 0")
        return cur.rowcount
