"""非敏感 KV：settings（主题、AI 模型选择等）与 app_state（今日 AI 建议缓存）。"""
from __future__ import annotations

from datetime import datetime, timezone

from ..database import get_conn, row_to_dict


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def get_app_state(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def set_app_state(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def delete_app_state(key: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
