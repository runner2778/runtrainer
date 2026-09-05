"""日期工具：业务日期一律本地时区 yyyy-mm-dd，时间戳一律 epoch 秒（UTC）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import tzlocal

_LOCAL_TZ = ZoneInfo(tzlocal.get_localzone_name())


def local_tz() -> ZoneInfo:
    return _LOCAL_TZ


def today() -> date:
    return datetime.now(_LOCAL_TZ).date()


def today_str() -> str:
    return today().isoformat()


def date_to_ts(d: date) -> int:
    """本地日期的 0 点对应的 epoch 秒（UTC）。"""
    return int(datetime(d.year, d.month, d.day, tzinfo=_LOCAL_TZ).timestamp())


def ts_to_date(ts: float | int) -> date:
    return datetime.fromtimestamp(ts, _LOCAL_TZ).date()


def ts_to_datetime(ts: float | int) -> datetime:
    return datetime.fromtimestamp(ts, _LOCAL_TZ)


def fmt_time(seconds: int | float | None) -> str:
    """秒 → h:mm:ss 或 m:ss。"""
    if seconds is None:
        return "--"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_pace(s_km: float | None) -> str:
    """配速 s/km → m:ss/km。"""
    if s_km is None:
        return "--"
    m, s = divmod(int(round(s_km)), 60)
    return f"{m}:{s:02d}"


def week_range(ref: date) -> tuple[date, date]:
    """ref 所在周（周一~周日）。"""
    monday = ref - timedelta(days=ref.weekday())
    return monday, monday + timedelta(days=6)


def days_between(a: date, b: date) -> int:
    return (b - a).days
