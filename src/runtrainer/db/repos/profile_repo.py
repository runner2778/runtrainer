"""profile 单行表读写。"""
from __future__ import annotations

from datetime import datetime, timezone

from ..database import get_conn, row_to_dict

PROFILE_ID = 1


def get_profile() -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM profile WHERE id = ?", (PROFILE_ID,)).fetchone())


def upsert_profile(fields: dict) -> dict:
    """写入/更新档案字段（仅允许白名单字段）。"""
    allowed = {"nickname", "sex", "birth_year", "height_cm", "weight_kg",
               "max_hr", "rest_hr", "hr_source", "run_experience", "vo2max"}
    data = {k: v for k, v in fields.items() if k in allowed}
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    updates = ", ".join(f"{k} = excluded.{k}" for k in data)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO profile (id, {cols}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            (PROFILE_ID, *data.values()),
        )
        return row_to_dict(conn.execute("SELECT * FROM profile WHERE id = ?", (PROFILE_ID,)).fetchone())
