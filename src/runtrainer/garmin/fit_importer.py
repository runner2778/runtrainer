"""FIT 文件解析：record/lap/session 消息 → RawActivity（含采样与单圈）。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fitparse import FitFile

from ..utils import dates
from .adapter import RawActivity

log = logging.getLogger(__name__)


def _to_num(v):
    """fitparse 返回值规整：字节串/异常值 → None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _val(vals: dict, *names):
    """从消息值字典中取第一个非空字段（注意：DataMessage 不支持 `in` 字段名）。"""
    for n in names:
        v = _to_num(vals.get(n))
        if v is not None:
            return v
    return None


def _ts_from_dt(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # FIT 时间戳为 UTC
    return int(dt.timestamp())


def _tz_offset_min(ts: int) -> int:
    """该时刻本地时区偏移（分钟）。"""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    off = dates.local_tz().utcoffset(dt)
    return int(off.total_seconds() // 60) if off else 0


def parse_fit(path: Path | str) -> RawActivity:
    path = Path(path)
    fit = FitFile(str(path))

    records: list[dict] = []
    laps: list[dict] = []
    session: dict = {}
    first_ts = last_ts = None

    for msg in fit.messages:
        name = msg.name
        if name not in ("record", "lap", "session"):
            continue
        vals = msg.get_values()
        if name == "record":
            ts = _ts_from_dt(vals.get("timestamp"))
            if ts is None:
                continue
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            records.append({
                "t_offset_s": None,  # 待补
                "ts": ts,
                "hr": _val(vals, "heart_rate"),
                "speed_mps": _val(vals, "enhanced_speed", "speed"),
                "cadence": _val(vals, "enhanced_cadence", "cadence"),
                "altitude_m": _val(vals, "enhanced_altitude", "altitude"),
            })
        elif name == "lap":
            laps.append({
                "start_ts": _ts_from_dt(vals.get("start_time")),
                "elapsed_s": _val(vals, "total_elapsed_time"),
                "distance_m": _val(vals, "total_distance"),
                "avg_hr": _val(vals, "avg_heart_rate"),
                "max_hr": _val(vals, "max_heart_rate"),
                "avg_cadence": _val(vals, "avg_cadence"),
            })
        else:  # session
            session = {
                "sport": vals.get("sport") or "running",
                "start_ts": _ts_from_dt(vals.get("start_time")),
                "duration_s": _val(vals, "total_elapsed_time"),
                "distance_m": _val(vals, "total_distance"),
                "avg_hr": _val(vals, "avg_heart_rate"),
                "max_hr": _val(vals, "max_heart_rate"),
                "avg_cadence": _val(vals, "avg_cadence"),
                "max_cadence": _val(vals, "max_cadence"),
                "calories": _val(vals, "total_calories"),
                "elevation_gain_m": _val(vals, "total_ascent"),
                "elevation_loss_m": _val(vals, "total_descent"),
            }

    start_ts = session.get("start_ts") or first_ts
    if start_ts is None:
        raise ValueError("FIT 文件无有效时间戳：无法导入")
    duration_s = session.get("duration_s") or (last_ts - first_ts if first_ts and last_ts else 0)
    distance_m = session.get("distance_m")
    if distance_m is None and records:
        # 由采样速度积分兜底（无 session 的异常文件）
        distance_m = 0.0
        prev = None
        for r in records:
            if prev is not None and r["speed_mps"] is not None and r["ts"] > prev:
                distance_m += r["speed_mps"] * (r["ts"] - prev)
            prev = r["ts"]
        distance_m = round(distance_m, 1) or None

    avg_pace = None
    if duration_s and distance_m:
        avg_pace = duration_s / (distance_m / 1000.0)

    samples = []
    for r in records:
        r["t_offset_s"] = r["ts"] - start_ts
        samples.append({k: r[k] for k in ("t_offset_s", "hr", "speed_mps", "cadence", "altitude_m")})

    sport = str(session.get("sport") or "running")
    name = path.stem
    return RawActivity(
        external_id="",  # 由 import_service 按文件哈希生成
        name=name,
        sport=sport,
        start_ts=start_ts,
        tz_offset_min=_tz_offset_min(start_ts),
        duration_s=duration_s,
        distance_m=distance_m,
        avg_pace_s_km=avg_pace,
        avg_hr=session.get("avg_hr"),
        max_hr=session.get("max_hr"),
        avg_cadence=session.get("avg_cadence"),
        max_cadence=session.get("max_cadence"),
        elevation_gain_m=session.get("elevation_gain_m"),
        elevation_loss_m=session.get("elevation_loss_m"),
        calories=int(session["calories"]) if session.get("calories") else None,
        laps=laps,
        samples=samples,
    )
