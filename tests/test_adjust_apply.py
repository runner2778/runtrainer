"""AI 调整落库的内容一致性（批21 + 批23）：kind 变更时标题/描述/分段必须跟着换，
配速字段必须与新类型/zone 一致。

回归场景：聊天里批准「下午 LT2 改为短间歇」后日历颜色/强度带变了，但格子
文字与详情页仍显示旧「双阈值·下（LT2 乳酸阈 5×5′）」（AI 只给了 kind 字段，
_apply_row 不重建内容）；批23 补上同一处缺漏——结构变了而落库配速还是旧强度
课的（#38 实证：短间歇 I 课弹窗「目标配速」标 T 的 3:55）。
"""
import json

import pytest

from runtrainer.domain import vdot as vd
from runtrainer.services import coach_service
from runtrainer.db.repos import plan_repo


def _mk_workout(kind="T", title="双阈值·下（LT2 乳酸阈 5×5′）",
                desc="挪威双阈值法下午段：LT2 乳酸阈 5×5 分钟 T（组间慢跑 1 分钟）。",
                dist=8.8, dur=50.0, segments=None):
    return {
        "id": 3710, "plan_id": 49, "date": "2026-09-09", "slot": 2,
        "week_index": 1, "phase": "transition", "kind": kind,
        "title": title, "description": desc, "distance_km": dist,
        "duration_min": dur, "pace_zone": "T", "pace_slow_s_km": 250.0,
        "pace_fast_s_km": 240.0, "target_hr_zone": None,
        "segments_json": json.dumps(segments) if segments else None,
        "source": "engine", "adjustment_id": None, "status": "planned",
        "completed_activity_id": None,
    }


def _mk_row(workout_id=3710, action="modify", changes=None, reason="按你的要求：改成短间歇"):
    return {
        "id": 1, "plan_id": 49, "workout_id": workout_id,
        "applies_date": "2026-09-09", "action": action,
        "changes_json": json.dumps(changes or {"kind": "I", "pace_zone": "I",
                                               "distance_km": 8.8, "duration_min": 50.0}),
        "reason": reason, "ai_model": "mock", "status": "pending",
    }


@pytest.fixture
def apply_target(monkeypatch):
    """_apply_row 只经 repo 读写 → 打桩捕获 update 内容。"""
    captured = {}
    monkeypatch.setattr(plan_repo, "get_workout", lambda wid: _mk_workout())
    monkeypatch.setattr(plan_repo, "update_workout",
                        lambda wid, w: captured.update(w))
    return captured


def test_modify_kind_change_no_ai_text_rebuilds_content(apply_target):
    """kind T→I 而 AI 只给字段（无 title/description）→ 兜底规范标题+原因描述，清旧分段。"""
    plan = {"id": 49, "vdot": 55.5}
    coach_service._apply_row(plan, _mk_row())
    w = apply_target
    assert w["kind"] == "I"
    assert w["pace_zone"] == "I"
    # 标题不再是旧「双阈值·下…5×5′」，描述不再是旧 LT2 巡航文案
    assert "双阈值·下" not in w["title"]
    assert "间歇" in w["title"] or "教练调整" in w["title"]
    assert "按你的要求" in w["description"]
    assert w["segments_json"] is None  # 旧 5×5′ 巡航分段必须清掉
    assert w["source"] == "ai" and w["adjustment_id"] == 1
    # 批23：落库配速与 I 单值对齐（原 235.0=T 配速 → 215.0），弹窗不再标 3:55
    exp = vd.pace_table(55.5)["I"]
    assert w["pace_slow_s_km"] == exp and w["pace_fast_s_km"] == exp


def test_modify_kind_change_keeps_ai_title_and_description(apply_target):
    """AI 给了 title/description（新契约）→ 原样使用，旧分段仍清空。"""
    plan = {"id": 49, "vdot": 55.5}
    row = _mk_row(changes={
        "kind": "I", "pace_zone": "I", "distance_km": 8.8, "duration_min": 50.0,
        "title": "短间歇 10×400m",
        "description": "热身 2km + 10×400m（I 配速，组间慢跑 200m）+ 冷身 1km"})
    coach_service._apply_row(plan, row)
    w = apply_target
    assert w["title"] == "短间歇 10×400m"
    assert "10×400m" in w["description"]
    assert w["segments_json"] is None


def test_modify_to_recovery_force_retitle(apply_target):
    """kind 变到恢复跑且 AI 无文案 → 即使旧标题不含质量课标记也换成「放松跑」。"""
    plan = {"id": 49, "vdot": 55.5}
    # 旧行是 E「轻松跑 60 分钟」，AI 改 kind=RECOVERY（连 pace_zone 都没给）
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(plan_repo, "get_workout",
                        lambda wid: _mk_workout(kind="E", title="轻松跑 60 分钟",
                                                desc="轻松有氧，放松完成。",
                                                dist=8.0, dur=60.0))
    monkeypatch.setattr(plan_repo, "update_workout",
                        lambda wid, w: apply_target.update(w))
    coach_service._apply_row(plan, _mk_row(changes={"kind": "RECOVERY"}))
    monkeypatch.undo()
    w = apply_target
    assert w["kind"] == "RECOVERY"
    assert w["title"].startswith("放松跑")
    assert w["segments_json"] is None
    # 批23：zone 归位 RECOVERY 且配速落恢复带两端，不留旧 T/E 单值
    assert w["pace_zone"] == "RECOVERY"
    b = vd.pace_table(55.5)["RECOVERY"]
    assert w["pace_slow_s_km"] == b["slow_s_km"]
    assert w["pace_fast_s_km"] == b["fast_s_km"]


def test_modify_kind_unchanged_refresh_title_number(apply_target):
    """kind 不变只改量（90→60 分钟）→ 引擎风格标题数字刷新（防「轻松跑 122 分钟」残留）。"""
    plan = {"id": 49, "vdot": 55.5}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(plan_repo, "get_workout",
                        lambda wid: _mk_workout(kind="E", title="轻松跑 122 分钟",
                                                desc="轻松有氧，放松完成。",
                                                dist=12.9, dur=122.0))
    monkeypatch.setattr(plan_repo, "update_workout",
                        lambda wid, w: apply_target.update(w))
    coach_service._apply_row(plan, _mk_row(changes={
        "kind": "E", "pace_zone": "E", "distance_km": 12.9, "duration_min": 60.0},
        reason="按你的要求：压缩日常有氧"))
    monkeypatch.undo()
    w = apply_target
    assert w["title"] == "轻松跑 60 分钟"
    # 批23：zone T→E 属于结构变化，配速落 E 带两端（防旧单值残留）
    assert w["pace_zone"] == "E"
    b = vd.pace_table(55.5)["E"]
    assert w["pace_slow_s_km"] == b["slow_s_km"]
    assert w["pace_fast_s_km"] == b["fast_s_km"]


def test_modify_kind_change_to_lr_keeps_m_zone(apply_target):
    """T 改 LR 且 AI 给了 zone M（final 期 LR-M 形态）→ M 保留；没给 zone → 归 E。"""
    plan = {"id": 49, "vdot": 55.5}
    w_m = _mk_workout(kind="T", title="阈值跑 5×5′")  # 落库配速 250/240 会被 resync 覆盖
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(plan_repo, "get_workout", lambda wid: dict(w_m))
    monkeypatch.setattr(plan_repo, "update_workout",
                        lambda wid, w: apply_target.update(w))
    coach_service._apply_row(plan, _mk_row(changes={"kind": "LR", "pace_zone": "M",
                                                    "distance_km": 16.0,
                                                    "duration_min": 90.0}))
    monkeypatch.undo()
    w = apply_target
    assert w["kind"] == "LR" and w["pace_zone"] == "M"
    exp = vd.pace_table(55.5)["M"]
    assert w["pace_slow_s_km"] == exp and w["pace_fast_s_km"] == exp
    assert w["title"].startswith("长距离")
    # 无 zone → LR 默认 E 带
    monkeypatch.setattr(plan_repo, "get_workout", lambda wid: dict(w_m))
    coach_service._apply_row(plan, _mk_row(changes={"kind": "LR", "distance_km": 16.0,
                                                    "duration_min": 90.0}))
    w = apply_target
    assert w["pace_zone"] == "E"
    b = vd.pace_table(55.5)["E"]
    assert w["pace_slow_s_km"] == b["slow_s_km"]
    assert w["pace_fast_s_km"] == b["fast_s_km"]


def test_add_easy_stores_zone_band_pace(monkeypatch):
    """加练（add_easy）也落 canonical 带配速而非 NULL → 日历弹窗不用现算 fallback。"""
    captured = {}
    plan = {"id": 49, "start_date": "2026-09-07", "vdot": 55.5}
    monkeypatch.setattr(plan_repo, "upsert_workout", lambda w: captured.update(w))
    monkeypatch.setattr(coach_service, "_phase_for_week",
                        lambda plan, wk: "transition")
    row = _mk_row(workout_id=None, action="add_easy",
                  changes={"duration_min": 30.0},
                  reason="按你的要求：今天想加练")
    coach_service._apply_row(plan, row)
    w = captured
    assert w["kind"] == "E" and w["pace_zone"] == "E"
    b = vd.pace_table(55.5)["E"]
    assert w["pace_slow_s_km"] == b["slow_s_km"]
    assert w["pace_fast_s_km"] == b["fast_s_km"]
    assert w["distance_km"] == round(30 * 60 / b["slow_s_km"], 1)
    assert w["title"] == "加练 · E"
