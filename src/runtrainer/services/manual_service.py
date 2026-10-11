"""手动补录活动：把界面/截图识别的松散输入变成一条正常活动，并保持下游一致。

手动活动的 external_id 用 `manual-<时间戳>-<随机>`，与手表活动（数字 id）
天然不冲突，佳明同步不会覆盖它。入库后与自动同步的活动完全同权：课程分类、
能力预估证据、健康图表、仪表盘都一视同仁。
"""
from __future__ import annotations

import logging
import time
import uuid

from ..db.repos import activity_repo, plan_repo
from ..domain import activity_edit

log = logging.getLogger(__name__)

SOURCE = "manual"


class ManualError(ValueError):
    """手动录入校验失败（消息直接展示给用户）。"""


def _new_external_id() -> str:
    return f"manual-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _validate(row: dict) -> None:
    d, t = row.get("distance_m"), row.get("duration_s")
    if d is None and t is None:
        raise ManualError("距离和用时至少要填一个")
    if d is not None and d <= 0:
        raise ManualError("距离必须大于 0")
    if t is not None and t <= 0:
        raise ManualError("用时必须大于 0")
    hr, mx = row.get("avg_hr"), row.get("max_hr")
    if hr is not None and not (40 <= hr <= 240):
        raise ManualError("平均心率超出 40–240 的合理范围")
    if mx is not None and not (40 <= mx <= 250):
        raise ManualError("最大心率超出 40–250 的合理范围")
    if hr is not None and mx is not None and mx < hr:
        raise ManualError("最大心率不能低于平均心率")
    cad = row.get("avg_cadence")
    if cad is not None and not (40 <= cad <= 260):
        raise ManualError("步频超出 40–260 步/分的合理范围")
    if d and t and row.get("avg_pace_s_km"):
        pace = row["avg_pace_s_km"]
        if not (100 <= pace <= 1200):
            raise ManualError("按距离与用时算出的配速不合常理（1:40–20:00 /km）")


def add_activity(payload: dict) -> dict:
    """新增一条手动活动。返回 {id, is_new, activity}。"""
    row = activity_edit.build_row({**payload, "source": SOURCE,
                                   "external_id": _new_external_id()})
    _validate(row)
    aid, is_new = activity_repo.upsert_activity(row)
    log.info("手动补录活动 #%s：%s %s", aid, row["name"], row.get("distance_m"))
    return {"id": aid, "is_new": is_new,
            "activity": activity_repo.get_activity(aid)}


def edit_activity(activity_id: int, fields: dict) -> dict:
    """改活动字段（含主观感受）。距离/用时改了会自动重算配速与步幅。"""
    cur = activity_repo.get_activity(activity_id)
    if not cur:
        raise ManualError("活动不存在")
    merged = activity_edit.merge_edit(cur, fields)
    _validate({**cur, **merged})
    activity_repo.update_activity(activity_id, merged)
    return activity_repo.get_activity(activity_id)


def delete_activity(activity_id: int) -> bool:
    cur = activity_repo.get_activity(activity_id)
    if not cur:
        raise ManualError("活动不存在")
    return activity_repo.delete_activity(activity_id)


def refresh_plan_after_entry() -> dict | None:
    """补录后重估水平并同步课表配速（与同步/导入同一条联动链）。

    只在确实有活动课表时动作；任何失败都不该让「录入」本身失败。
    """
    if not plan_repo.get_active_plan():
        return None
    try:
        from . import plan_service
        return plan_service.refresh_active_plan()
    except Exception:  # noqa: BLE001
        log.exception("补录后刷新课表失败（不影响录入本身）")
        return None
