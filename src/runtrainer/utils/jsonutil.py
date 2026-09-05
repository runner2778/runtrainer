"""JSON 工具：dataclass/dict 安全转换。"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime


def dumps(obj) -> str:
    """序列化为 JSON 字符串（中文不转义，日期/时间转 iso）。"""
    return json.dumps(obj, ensure_ascii=False, default=_default)


def loads(s: str | None):
    """反序列化，空/None 返回 None。"""
    if not s:
        return None
    return json.loads(s)


def _default(o):
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"无法序列化类型 {type(o)}")
