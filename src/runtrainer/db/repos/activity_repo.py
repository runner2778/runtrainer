"""活动与采样数据读写。"""
from __future__ import annotations

from datetime import datetime, timezone

from ..database import get_conn, row_to_dict

ACTIVITY_FIELDS = (
    "source", "external_id", "file_path", "name", "sport", "start_ts", "tz_offset_min",
    "duration_s", "distance_m", "avg_pace_s_km", "avg_hr", "max_hr", "avg_cadence",
    "max_cadence", "stride_length_m", "aerobic_te", "anaerobic_te", "exercise_load",
    "elevation_gain_m", "elevation_loss_m", "calories", "laps_json", "structure_json",
    "has_samples", "created_at",
)


def upsert_activity(a: dict) -> tuple[int, bool]:
    """插入或更新活动（UNIQUE(source, external_id) 去重）。返回 (id, 是否新建)。

    注意：不使用 ON CONFLICT DO UPDATE——其 lastrowid/rowcount 在部分 SQLite
    版本上不可靠（3.14 捆绑版首插返回 0），改为显式 SELECT 再写。
    """
    data = dict(a)
    data.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    placeholders = ", ".join("?" for _ in ACTIVITY_FIELDS)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM activities WHERE source = ? AND external_id = ?",
            (data["source"], data["external_id"]),
        ).fetchone()
        if existing:
            aid = existing["id"]
            # 列表概要缺的字段不覆盖已存的好数据（详情回填的 structure_json/
            # has_samples/采样曲线曾被列表同步的 None 冲掉——全库详情被清空）
            for f in ACTIVITY_FIELDS:
                if data.get(f) is None and f != "created_at":
                    data[f] = existing[f]
            updates = ", ".join(f"{f} = ?" for f in ACTIVITY_FIELDS if f not in ("source", "external_id"))
            conn.execute(f"UPDATE activities SET {updates} WHERE id = ?",
                         tuple(data.get(f) for f in ACTIVITY_FIELDS if f not in ("source", "external_id")) + (aid,))
            return aid, False
        cur = conn.execute(
            f"INSERT INTO activities ({', '.join(ACTIVITY_FIELDS)}) VALUES ({placeholders})",
            tuple(data.get(f) for f in ACTIVITY_FIELDS),
        )
        return cur.lastrowid, True


def list_activities(start_date: str | None = None, end_date: str | None = None,
                    source: str | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
    sql = "SELECT * FROM activities WHERE 1=1"
    args: list = []
    if start_date:
        sql += " AND date(start_ts, 'unixepoch', 'localtime') >= date(?)"
        args.append(start_date)
    if end_date:
        sql += " AND date(start_ts, 'unixepoch', 'localtime') <= date(?)"
        args.append(end_date)
    if source:
        sql += " AND source = ?"
        args.append(source)
    sql += " ORDER BY start_ts DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def list_pace_hr_rows(start_date: str | None = None, end_date: str | None = None,
                      limit: int = 5000) -> list[dict]:
    """配速-心率分析用轻量行（避开 laps_json/structure_json 大字段）。

    weekly_pace_hr / pace_bin_hr 只消费 start_ts/distance_m/avg_pace_s_km/
    avg_hr/max_hr 五列；此前用 list_activities 的 SELECT * 会把整行详情
    从 SQLite 搬到内存（再解析大 JSON 字段），窗口拉大后明显拖慢。
    """
    sql = ("SELECT start_ts, distance_m, avg_pace_s_km, avg_hr, max_hr"
           " FROM activities WHERE 1=1")
    args: list = []
    if start_date:
        sql += " AND date(start_ts, 'unixepoch', 'localtime') >= date(?)"
        args.append(start_date)
    if end_date:
        sql += " AND date(start_ts, 'unixepoch', 'localtime') <= date(?)"
        args.append(end_date)
    sql += " ORDER BY start_ts DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def get_activity(activity_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone())


def update_activity(activity_id: int, fields: dict) -> None:
    allowed = {"name", "sport", "distance_m", "duration_s", "avg_hr", "max_hr", "calories"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    with get_conn() as conn:
        conn.execute(f"UPDATE activities SET {sets} WHERE id = ?", (*data.values(), activity_id))


def save_samples(activity_id: int, rows: list[tuple]) -> None:
    """rows: [(t_offset_s, hr, speed_mps, cadence, altitude_m), ...] 全量覆盖。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM activity_samples WHERE activity_id = ?", (activity_id,))
        conn.executemany(
            "INSERT INTO activity_samples (activity_id, seq, t_offset_s, hr, speed_mps, cadence, altitude_m) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(activity_id, i, *row) for i, row in enumerate(rows)],
        )
        conn.execute("UPDATE activities SET has_samples = ? WHERE id = ?", (1 if rows else 0, activity_id))


def get_samples(activity_id: int) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM activity_samples WHERE activity_id = ? ORDER BY seq", (activity_id,))]


def list_sample_peak_hr(source: str | None = None, limit: int = 5000) -> list[float]:
    """每条活动的采样最大心率（max_hr 数据推断用）。按活动倒序，limit 防爆。"""
    sql = ("SELECT MAX(s.hr) FROM activity_samples s JOIN activities a ON a.id = s.activity_id"
           " WHERE s.hr IS NOT NULL")
    args: list = []
    if source:
        sql += " AND a.source = ?"
        args.append(source)
    sql += (" GROUP BY a.id ORDER BY MAX(a.start_ts) DESC LIMIT ?")
    args.append(limit)
    with get_conn() as conn:
        return [r[0] for r in conn.execute(sql, args)]


def count_activities() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]


def delete_demo_activities() -> int:
    """删除演示数据（mock 同步 external_id 以 mock_ 开头 + seed 的 source=demo），
    真实同步成功后清理，避免假数据混入真实记录。返回删除数。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM activities WHERE external_id LIKE 'mock_%' OR source = 'demo'")
        return cur.rowcount
