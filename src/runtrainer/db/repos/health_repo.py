"""每日健康指标读写（按日期一行，多来源合并 upsert）。"""
from __future__ import annotations

from datetime import datetime, timezone

from ..database import get_conn, row_to_dict

HEALTH_FIELDS = (
    "date", "source", "sleep_start_ts", "sleep_end_ts", "sleep_duration_s", "deep_s",
    "light_s", "rem_s", "awake_s", "sleep_score", "resting_hr", "avg_hr", "max_hr",
    "hrv_avg_ms", "hrv_status", "stress_avg", "body_battery_min", "body_battery_max",
    "steps", "raw_json", "updated_at",
)

_INSERTABLE = tuple(f for f in HEALTH_FIELDS if f != "date")


def upsert_daily_health(date: str, fields: dict) -> dict:
    """合并写入：已有行保留旧值，新值仅覆盖非 NULL 字段。"""
    with get_conn() as conn:
        existing = row_to_dict(
            conn.execute("SELECT * FROM daily_health WHERE date = ?", (date,)).fetchone()
        )
        merged = dict(existing) if existing else {"date": date}
        for k in _INSERTABLE:
            v = fields.get(k)
            if v is not None:
                merged[k] = v
        merged["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        placeholders = ", ".join("?" for _ in merged)
        updates = ", ".join(f"{k} = excluded.{k}" for k in merged if k != "date")
        conn.execute(
            f"INSERT INTO daily_health ({', '.join(merged.keys())}) VALUES ({placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {updates}",
            tuple(merged.values()),
        )
        return merged


def get_health(start_date: str, end_date: str | None = None) -> list[dict]:
    sql = "SELECT * FROM daily_health WHERE date >= ?"
    args: list = [start_date]
    if end_date:
        sql += " AND date <= ?"
        args.append(end_date)
    sql += " ORDER BY date"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def get_health_for_date(date: str) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM daily_health WHERE date = ?", (date,)).fetchone())


def purge_legacy_health() -> int:
    """删除无 raw 标记的旧健康行（mock/seed 演示时代产物）。

    真实同步行必带 raw 标志（raw_json 含 sleep/hrv/stress/summary 各数据源可用性），
    mock 与 seed 演示数据没有该字段 → raw_json 为 NULL，可安全区分。返回删除数。
    """
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM daily_health WHERE raw_json IS NULL")
        return cur.rowcount
