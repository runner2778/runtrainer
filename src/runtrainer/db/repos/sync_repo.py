"""同步状态读写（断点续传 + 状态页展示）。"""
from __future__ import annotations

from datetime import datetime, timezone

from ...utils import jsonutil
from ..database import get_conn, row_to_dict


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def get_sync_state(source: str) -> dict:
    with get_conn() as conn:
        row = row_to_dict(conn.execute("SELECT * FROM sync_state WHERE source = ?", (source,)).fetchone())
        if row is None:
            return {"source": source, "last_sync_ts": None, "last_error": None, "meta_json": None}
        return row


def set_sync_state(source: str, meta: dict | None = None, error: str | None = None) -> None:
    """成功时记录断点：meta 存游标/上次成功日期等（JSON 序列化）。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_state (source, last_sync_ts, last_error, meta_json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET last_sync_ts = excluded.last_sync_ts, "
            "last_error = excluded.last_error, meta_json = excluded.meta_json",
            (source, _now_ts(), error, jsonutil.dumps(meta) if meta is not None else None),
        )


def record_sync_error(source: str, error: str) -> None:
    """失败时只记错误并刷新尝试时间，保留 meta_json 断点。

    失败不得推进增量游标（否则下次同步会跳过失败窗口），也不得清空
    last_health_date（否则健康数据会被重新回溯拉取）。
    """
    cur = get_sync_state(source)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_state (source, last_sync_ts, last_error, meta_json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET last_sync_ts = excluded.last_sync_ts, "
            "last_error = excluded.last_error, meta_json = excluded.meta_json",
            (source, _now_ts(), error, cur["meta_json"]),
        )


def list_sync_states() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM sync_state")]
