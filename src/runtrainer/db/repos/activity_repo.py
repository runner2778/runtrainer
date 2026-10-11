"""活动与采样数据读写。"""
from __future__ import annotations

from datetime import datetime, timezone

from ..database import get_conn, row_to_dict

ACTIVITY_FIELDS = (
    "source", "external_id", "file_path", "name", "sport", "start_ts", "tz_offset_min",
    "duration_s", "distance_m", "avg_pace_s_km", "avg_hr", "max_hr", "avg_cadence",
    "max_cadence", "stride_length_m", "aerobic_te", "anaerobic_te", "exercise_load",
    "elevation_gain_m", "elevation_loss_m", "calories", "laps_json", "structure_json",
    "has_samples", "created_at", "feel", "rpe", "note", "manual",
)

# activities 表里 NOT NULL 且带 DEFAULT 的列（值缺失时按 DEFAULT 兜底）。
# 迁移脚本里的 DEFAULT 只在「列未出现在 INSERT 列表」时生效，本函数的
# INSERT/UPDATE 列全写，所以必须在这里显式补。
ACTIVITY_NOT_NULL_DEFAULTS = {"tz_offset_min": 0, "has_samples": 0, "manual": 0}

# 可手动编辑的字段（桌面「活动详情」与手动录入用）。距离/用时/配速互相关联，
# 改了要重算派生量，见 domain.activity_edit.merge_edit。配速本身也在列：截图
# 识别可能只读到配速、读不到距离/用时，得让用户把它填得进去。
EDITABLE_FIELDS = (
    "name", "sport", "distance_m", "duration_s", "avg_pace_s_km",
    "avg_hr", "max_hr", "calories",
    "avg_cadence", "stride_length_m", "feel", "rpe", "note",
)


def upsert_activity(a: dict) -> tuple[int, bool]:
    """插入或更新活动（UNIQUE(source, external_id) 去重）。返回 (id, 是否新建)。

    注意：不使用 ON CONFLICT DO UPDATE——其 lastrowid/rowcount 在部分 SQLite
    版本上不可靠（3.14 捆绑版首插返回 0），改为显式 SELECT 再写。
    """
    data = dict(a)
    data.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # NOT NULL DEFAULT 列要在 INSERT 里显式给值：本函数把 ACTIVITY_FIELDS 全列
    # 都绑进语句，None 会直接撞 NOT NULL，列的 DEFAULT 根本轮不到生效
    # （0013 加了 manual NOT NULL DEFAULT 0 后，佳明同步插新活动就整批失败）。
    for col, default in ACTIVITY_NOT_NULL_DEFAULTS.items():
        if data.get(col) is None:
            data[col] = default
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


def get_by_external(source: str, external_id: str) -> dict | None:
    """按 (source, external_id) 取整行。

    详情回填保留已存字段用——此前每活动 list_activities(limit=1000) 全表
    线性扫描找行，N 活动 O(N×M)；此列有 UNIQUE 索引，O(N) 直达。
    """
    with get_conn() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM activities WHERE source = ? AND external_id = ?",
            (source, external_id)).fetchone())


def mark_detail_attempted(activity_id: int) -> None:
    """详情回填成功标记：无采样曲线的活动不再被每轮同步重复拉取。"""
    with get_conn() as conn:
        conn.execute("UPDATE activities SET detail_attempted = 1 WHERE id = ?", (activity_id,))


def list_detail_missing(cutoff_ts: int, source: str, limit: int = 3000) -> list[dict]:
    """缺详情回填的活动（轻量投影，避开 laps/structure 大 JSON 字段）。

    条件：窗口内 + has_samples=0 + 从未成功回填过（detail_attempted=0）。
    返回 [{external_id, start_ts}]，按时间倒序。
    """
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT external_id, start_ts FROM activities"
            " WHERE source = ? AND start_ts >= ? AND has_samples = 0 AND detail_attempted = 0"
            " ORDER BY start_ts DESC LIMIT ?", (source, cutoff_ts, limit))]


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


def list_subjective_rows(start_date: str | None = None,
                         end_date: str | None = None,
                         limit: int = 5000) -> list[dict]:
    """主观感受/用力程度的轻量行：只取周聚合需要的四列。"""
    sql = ("SELECT start_ts, feel, rpe, distance_m FROM activities WHERE 1=1")
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


def list_structure_rows(start_date: str | None = None, end_date: str | None = None,
                        limit: int = 5000) -> list[dict]:
    """「次内分段」配速-心率分析：配速-心率五列 + structure_json。

    与 list_pace_hr_rows 的区别只在多带 structure_json——整场平均配速会把
    间歇课抹平（3:20/km 的重复跑整场看是 4:30），段级 pace_s_km/avg_hr 才能
    让快配速档有样本。仍刻意不带 laps_json / samples 两个大字段。
    无结构的活动（CSV/FIT 手动导入不生成结构）由调用方回退整场口径。
    """
    sql = ("SELECT start_ts, distance_m, avg_pace_s_km, avg_hr, max_hr, structure_json"
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
    data = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    with get_conn() as conn:
        conn.execute(f"UPDATE activities SET {sets} WHERE id = ?", (*data.values(), activity_id))


def delete_activity(activity_id: int) -> bool:
    """删除活动（含采样）。手动补录的错录需要能删。返回是否真删到行。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        conn.execute("DELETE FROM activity_samples WHERE activity_id = ?", (activity_id,))
        return cur.rowcount > 0


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
