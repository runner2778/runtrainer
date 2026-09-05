"""CSV 模板导入：每行一条活动，容错解析。

模板列（中英文表头均可）：
date/日期, start_time/开始时间, duration_s/时长秒, distance_m/距离米,
avg_hr/平均心率, max_hr/最大心率, avg_cadence/步频, elevation_gain_m/爬升米,
name/名称, sport/运动类型
"""
from __future__ import annotations

import csv
from datetime import datetime, time
from pathlib import Path

from ..utils import dates
from .adapter import RawActivity

_ALIASES = {
    "date": ("date", "日期"),
    "start_time": ("start_time", "开始时间"),
    "duration_s": ("duration_s", "时长秒", "时长(秒)"),
    "distance_m": ("distance_m", "距离米", "距离(米)"),
    "avg_hr": ("avg_hr", "平均心率"),
    "max_hr": ("max_hr", "最大心率"),
    "avg_cadence": ("avg_cadence", "步频"),
    "elevation_gain_m": ("elevation_gain_m", "爬升米", "爬升(米)"),
    "name": ("name", "名称"),
    "sport": ("sport", "运动类型"),
}


def _resolve_header(header: list[str]) -> dict[str, str]:
    """列名 → 规范字段名。"""
    mapping: dict[str, str] = {}
    for idx, col in enumerate(header):
        col_norm = col.strip().lower()
        for canon, aliases in _ALIASES.items():
            if col_norm in aliases or col.strip() in aliases:
                mapping[col] = canon
                break
    return mapping


def _num(v, cast=float):
    if v is None or str(v).strip() == "":
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def parse_csv(path: Path | str) -> list[RawActivity]:
    path = Path(path)
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        mapping = _resolve_header(list(reader.fieldnames))
        results: list[RawActivity] = []
        for row in reader:
            row = {mapping.get(k, k): v for k, v in row.items()}
            date_s = (row.get("date") or "").strip()
            time_s = (row.get("start_time") or "").strip()
            if not date_s:
                continue
            try:
                d = datetime.strptime(date_s, "%Y-%m-%d").date()
            except ValueError:
                continue
            t = None
            if time_s:
                for fmt in ("%H:%M:%S", "%H:%M"):
                    try:
                        t = datetime.strptime(time_s, fmt).time()
                        break
                    except ValueError:
                        pass
            if t is None:
                t = time(6, 0)  # 缺省 06:00
            start_ts = dates.date_to_ts(d) + t.hour * 3600 + t.minute * 60 + t.second
            duration_s = _num(row.get("duration_s"))
            distance_m = _num(row.get("distance_m"))
            avg_pace = None
            if duration_s and distance_m:
                avg_pace = duration_s / (distance_m / 1000.0)
            results.append(RawActivity(
                external_id="",
                name=(row.get("name") or "").strip() or f"{date_s} 活动",
                sport=(row.get("sport") or "running").strip() or "running",
                start_ts=start_ts,
                tz_offset_min=dates.local_tz().utcoffset(datetime(d.year, d.month, d.day)).total_seconds() // 60
                or 0,
                duration_s=duration_s,
                distance_m=distance_m,
                avg_pace_s_km=avg_pace,
                avg_hr=_num(row.get("avg_hr")),
                max_hr=_num(row.get("max_hr")),
                avg_cadence=_num(row.get("avg_cadence")),
                elevation_gain_m=_num(row.get("elevation_gain_m")),
                calories=None,
                laps=[],
                samples=[],
            ))
        return results
