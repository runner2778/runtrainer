"""活动录入与编辑的纯函数层：归一化主观感受、补全派生量、校验。

桌面「活动 → 手动录入 / 截图识别」与「活动详情改主观感受」都走这里，
保证入库口径唯一（与佳明同步来的行同一套列与同一套归一规则）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from math import floor

# 佳明 Connect 的两套主观量表（API 里都是 0-100 的整数）
#   directWorkoutFeel: 0/25/50/75/100 → 很轻松…非常吃力（1-5）
#   directWorkoutRpe : 0…100          → RPE 1-10
FEEL_MAX = 5
RPE_MAX = 10

_SPORT_ALIASES = {
    "run": "running", "running": "running", "跑步": "running", "跑": "running",
    "trail": "trail_running", "越野": "trail_running",
    "treadmill": "treadmill_running", "跑步机": "treadmill_running",
    "walk": "walking", "走": "walking", "步行": "walking",
    "cycle": "cycling", "bike": "cycling", "骑行": "cycling",
}
TREADMILL_SPORTS = ("treadmill_running", "indoor_running")
_ALIAS_KEYS_BY_LEN = sorted(_SPORT_ALIASES, key=len, reverse=True)


def norm_sport(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "running"
    if s in _SPORT_ALIASES:
        return _SPORT_ALIASES[s]
    # 按别名长度从长到短匹配：短别名是长别名的子串（"跑" ⊂ "越野跑"），
    # 先匹短的会把「越野跑」判成普通跑步
    for k in _ALIAS_KEYS_BY_LEN:
        if k in s:
            return _SPORT_ALIASES[k]
    return "running"


def _int_or_none(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _float_or_none(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _scale_round(v: float, full: int) -> int:
    """0-100 量纲 → 1-full 量纲，四舍五入用「逢半进一」。

    Python 内建 round() 是银行家舍入（round(2.5)==2），量表换算里会把中档
    系统性压半格（佳明 feel=50 会算成 2 而不是 3）。"""
    return int(floor(v * full / 100.0 + 0.5))


def norm_feel(v) -> int | None:
    """主观感受 → 1-5。

    同时接受佳明的 0-100 与用户直接填的 1-5；0 与 None 都视为未填
    （佳明把「没打标签」也写成 0）。
    """
    n = _int_or_none(v)
    if n is None or n <= 0:
        return None
    if n > FEEL_MAX:
        n = _scale_round(n, FEEL_MAX)
    return max(1, min(FEEL_MAX, n))


def norm_rpe(v) -> int | None:
    """用力程度 → 1-10。接受佳明的 0-100 与用户填的 1-10。"""
    n = _int_or_none(v)
    if n is None or n <= 0:
        return None
    if n > RPE_MAX:
        n = _scale_round(n, RPE_MAX)
    return max(1, min(RPE_MAX, n))


def derive_pace(distance_m, duration_s) -> float | None:
    """由距离与用时推平均配速（s/km）。"""
    d = _float_or_none(distance_m)
    t = _float_or_none(duration_s)
    if not d or not t or d <= 0 or t <= 0:
        return None
    return round(t / (d / 1000.0), 1)


def derive_stride(pace_s_km, cadence_spm) -> float | None:
    """由配速与步频推步幅（米）。步频按「总步频」（每分钟两步）口径，
    与佳明 averageRunningCadenceInStepsPerMinute 一致。"""
    p = _float_or_none(pace_s_km)
    c = _float_or_none(cadence_spm)
    if not p or not c or p <= 0 or c <= 0:
        return None
    speed_mps = 1000.0 / p           # 配速 → m/s
    return round(speed_mps / (c / 60.0), 3)


def norm_local_datetime(text: str | None) -> tuple[int, int] | None:
    """把「2026-10-06 07:12」/「2026-10-06T07:12」解析成 (unix秒, tz偏移分钟)。

    无时区信息时按本机时区处理——用户填的是他看到的手表时间。
    """
    s = (text or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
                 r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else 0
    mi = int(m.group(5)) if m.group(5) else 0
    ss = int(m.group(6)) if m.group(6) else 0
    try:
        dt = datetime(y, mo, d, hh, mi, ss).astimezone()
    except ValueError:
        return None
    return int(dt.timestamp()), int(dt.utcoffset().total_seconds() // 60)


def build_row(payload: dict, *, source: str = "manual") -> dict:
    """把界面/OCR 的松散输入整理成 activities 行。

    规则：距离+用时齐了才推配速；配速+步频齐了才推步幅；用户已填的派生量
    不覆盖（尊重手填）。缺距离或缺用时的活动仍可入库（如力量课只填时长）。
    """
    p = dict(payload)
    name = (p.get("name") or "").strip() or "手动录入"
    sport = norm_sport(p.get("sport"))
    distance_m = _float_or_none(p.get("distance_m"))
    duration_s = _float_or_none(p.get("duration_s"))
    pace = _float_or_none(p.get("avg_pace_s_km")) or derive_pace(distance_m, duration_s)
    cadence = _float_or_none(p.get("avg_cadence"))
    stride = _float_or_none(p.get("stride_length_m")) or derive_stride(pace, cadence)
    start_ts = _int_or_none(p.get("start_ts"))
    tz = _int_or_none(p.get("tz_offset_min"))
    if start_ts is None:
        parsed = norm_local_datetime(p.get("start_local"))
        if parsed:
            start_ts, tz = parsed
    if start_ts is None or tz is None:
        now = datetime.now().astimezone()
        start_ts = start_ts if start_ts is not None else int(now.timestamp())
        tz = tz if tz is not None else int(now.utcoffset().total_seconds() // 60)
    return {
        "source": source,
        "external_id": (p.get("external_id") or "").strip(),
        "name": name,
        "sport": sport,
        "start_ts": start_ts,
        "tz_offset_min": tz,
        "duration_s": duration_s,
        "distance_m": distance_m,
        "avg_pace_s_km": pace,
        "avg_hr": _float_or_none(p.get("avg_hr")),
        "max_hr": _float_or_none(p.get("max_hr")),
        "avg_cadence": cadence,
        "max_cadence": _float_or_none(p.get("max_cadence")),
        "stride_length_m": stride,
        "elevation_gain_m": _float_or_none(p.get("elevation_gain_m")),
        "elevation_loss_m": _float_or_none(p.get("elevation_loss_m")),
        "calories": _int_or_none(p.get("calories")),
        "feel": norm_feel(p.get("feel")),
        "rpe": norm_rpe(p.get("rpe")),
        "note": (p.get("note") or "").strip() or None,
        "manual": 1 if source == "manual" else _int_or_none(p.get("manual")) or 0,
        "has_samples": 0,
    }


def edit_fields(fields: dict) -> dict:
    """活动详情的部分字段编辑：只取白名单并归一，配速随距离/用时自动重算
    （调用方负责把当前行一起传进来做推导）。"""
    out: dict = {}
    for k in ("name", "sport", "distance_m", "duration_s", "avg_hr", "max_hr",
              "calories", "avg_cadence", "stride_length_m", "note",
              "elevation_gain_m"):
        if k in fields:
            out[k] = fields[k]
    if "feel" in fields:
        out["feel"] = norm_feel(fields["feel"])
    if "rpe" in fields:
        out["rpe"] = norm_rpe(fields["rpe"])
    for k in ("distance_m", "duration_s", "avg_hr", "max_hr", "avg_cadence",
              "stride_length_m", "elevation_gain_m"):
        if k in out:
            out[k] = _float_or_none(out[k])
    for k in ("calories",):
        if k in out:
            out[k] = _int_or_none(out[k])
    if "sport" in out:
        out["sport"] = norm_sport(out["sport"])
    if "name" in out:
        out["name"] = (out["name"] or "").strip() or None
    if "note" in out:
        out["note"] = (out["note"] or "").strip() or None
    return out


def merge_edit(current: dict, edits: dict) -> dict:
    """把编辑合并到当前行，并保持派生量自洽：距离或用时变了就重算配速；
    配速或步频变了且用户没手填步幅就重算步幅。"""
    row = dict(current)
    row.update(edit_fields(edits))
    if "distance_m" in edits or "duration_s" in edits:
        pace = derive_pace(row.get("distance_m"), row.get("duration_s"))
        if pace:
            row["avg_pace_s_km"] = pace
    if "stride_length_m" not in edits:
        stride = derive_stride(row.get("avg_pace_s_km"), row.get("avg_cadence"))
        if stride:
            row["stride_length_m"] = stride
    return {k: row.get(k) for k in
            ("name", "sport", "distance_m", "duration_s", "avg_pace_s_km", "avg_hr",
             "max_hr", "calories", "avg_cadence", "stride_length_m", "feel", "rpe",
             "note", "manual", "elevation_gain_m")}
