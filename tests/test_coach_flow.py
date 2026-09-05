"""M4：AI 教练全流程（MockClient 4 场景：正常/低HRV/超负荷/加练）。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from runtrainer.ai.deepseek_client import MockClient
from runtrainer.db.repos import adjustment_repo, health_repo, kv_repo, plan_repo
from runtrainer.services import coach_service, plan_service
from runtrainer.utils import dates

REAL_TODAY = dates.today()


@pytest.fixture()
def plan():
    """生成一个 12 周半马计划（从明天起），返回 (plan, workouts)。"""
    race = REAL_TODAY + timedelta(days=84)
    payload = plan_service.create_goal_and_plan({
        "goal": {"distance_m": 21097, "race_date": race.isoformat(),
                 "target_seconds": None, "vdot": 45.0, "name": "半马"},
        "plan": {"base_weekly_km": 40.0},
    })
    p = plan_repo.get_plan(payload["plan_id"])
    return p, plan_repo.get_workouts(p["id"])


def _patch_today(monkeypatch, d):
    monkeypatch.setattr("runtrainer.utils.dates.today", lambda: d)


def _hard_date(workouts, weeks_from_start=2):
    """计划中部找一个强度课日期（避开 taper 与赛前窗口）。"""
    for w in workouts:
        if w["kind"] in ("T", "I") and weeks_from_start <= int(w["week_index"]) <= 8:
            return w["date"]
    raise RuntimeError("计划中未找到强度课")


def _make(mocker_or_monkeypatch, scenario):
    return mocker_or_monkeypatch.setattr(
        coach_service, "_make_client", lambda extra=False: MockClient(scenario))


def test_normal_scenario_end_to_end(monkeypatch, plan):
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    _make(monkeypatch, "normal")
    snap = coach_service.request_advice()
    assert snap["advice"] is not None
    assert snap["advice"]["readiness"] == "good"
    # keep 项落库为 pending
    rows = adjustment_repo.list_adjustments(p["id"], status="pending")
    assert rows and all(r["status"] == "pending" for r in rows)


def test_low_hrv_modifies_hard_session(monkeypatch, plan):
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    _make(monkeypatch, "low_hrv")
    coach_service.request_advice()
    rows = adjustment_repo.list_adjustments(p["id"], status="pending")
    mods = [r for r in rows if r["action"] == "modify"]
    assert mods, "低 HRV 场景应将强度课改为轻松课"
    # 批准 → 课表变 E
    res = coach_service.decide_advice(True)
    assert res["errors"] == []
    w = plan_repo.get_workout(mods[0]["workout_id"])
    assert w["kind"] == "E" and w["pace_zone"] == "E"
    assert w["source"] == "ai" and w["adjustment_id"] == mods[0]["id"]
    assert adjustment_repo.get_adjustment(mods[0]["id"])["status"] == "applied"


def test_overload_rests_hard_session(monkeypatch, plan):
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    _make(monkeypatch, "overload")
    coach_service.request_advice()
    rows = adjustment_repo.list_adjustments(p["id"], status="pending")
    rests = [r for r in rows if r["action"] == "rest"]
    assert rests, "超负荷场景应建议休息"
    coach_service.decide_advice(True)
    w = plan_repo.get_workout(rests[0]["workout_id"])
    assert w["status"] == "skipped"


def test_add_extra_flow(monkeypatch, plan):
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    _make(monkeypatch, "add_extra")
    snap = coach_service.request_advice(extra_requested=True, user_note="今天想多跑一点")
    assert snap["advice"]["extra_requested"] is True
    adds = [r for r in adjustment_repo.list_adjustments(p["id"], status="pending")
            if r["action"] == "add_easy"]
    assert adds, "加练场景应生成 add_easy"
    adds_view = [a for a in snap["advice"]["adjustments"] if a["action"] == "add_easy"]
    assert adds_view and adds_view[0]["changes"]["kind"] == "E"
    coach_service.decide_advice(True)
    new_w = plan_repo.get_workout_by_date(p["id"], adds[0]["applies_date"])
    assert new_w and new_w["source"] == "ai" and new_w["kind"] == "E"
    assert new_w["duration_min"] <= 45


def test_cache_prevents_second_call(monkeypatch, plan):
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    client = MockClient("normal")
    monkeypatch.setattr(coach_service, "_make_client", lambda extra=False: client)
    coach_service.request_advice()
    coach_service.request_advice()
    assert len(client.calls) == 1, "当日已有建议应走缓存，不重复调用"


def test_reject_keeps_plan(monkeypatch, plan):
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    _make(monkeypatch, "low_hrv")
    coach_service.request_advice()
    mods = [r for r in adjustment_repo.list_adjustments(p["id"], status="pending")
            if r["action"] == "modify"]
    coach_service.decide_advice(False)
    assert all(adjustment_repo.get_adjustment(m["id"])["status"] == "rejected"
               for m in mods)
    w = plan_repo.get_workout(mods[0]["workout_id"])
    assert w["kind"] != "E" or w["source"] == "engine"


def test_prompt_includes_health_and_plan(monkeypatch, plan):
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    for i in range(3):
        hd = (dates.date.fromisoformat(d) - timedelta(days=i)).isoformat()
        health_repo.upsert_daily_health(hd, {
            "sleep_duration_s": 7.5 * 3600, "sleep_score": 80,
            "hrv_avg_ms": 55.0, "hrv_status": "balanced",
            "resting_hr": 50, "stress_avg": 30.0,
        })
    client = MockClient("normal")
    monkeypatch.setattr(coach_service, "_make_client", lambda extra=False: client)
    coach_service.request_advice()
    call = client.calls[0]
    assert "近 14 天健康" in call["user"]
    assert "HRV" in call["user"]
    assert "配速表" in call["user"]
    assert "add_extra_advice" in call["system"]


def test_no_plan_raises(monkeypatch):
    _make(monkeypatch, "normal")
    with pytest.raises(RuntimeError, match="尚未创建训练计划"):
        coach_service.request_advice()


def test_no_key_real_mode_raises(monkeypatch, plan):
    from runtrainer.services import settings_service
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(settings_service, "get_ai_provider", lambda: "deepseek")
    monkeypatch.setattr(settings_service, "get_ai_key", lambda provider: None)
    with pytest.raises(RuntimeError, match="API Key"):
        coach_service.request_advice()


def test_provider_ollama_no_key_allowed(monkeypatch, plan):
    """Ollama 本地服务商无需 Key：不 raise 且 client 指向本地端点。"""
    from runtrainer.services import settings_service
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(settings_service, "get_ai_provider", lambda: "ollama")
    monkeypatch.setattr(settings_service, "get_ai_key", lambda provider: None)
    monkeypatch.setattr(settings_service, "get_ai_model", lambda: "qwen2.5:7b")
    client = coach_service._make_client(False)
    assert client.model == "qwen2.5:7b"
    assert "11434" in str(client._client.base_url)


def test_provider_zhipu_fallback_model(monkeypatch, plan):
    """切换服务商后旧模型名不在候选列表 → 回落该服务商默认模型。"""
    from runtrainer.services import settings_service
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(settings_service, "get_ai_provider", lambda: "zhipu")
    monkeypatch.setattr(settings_service, "get_ai_key", lambda provider: "fake-key")
    monkeypatch.setattr(settings_service, "get_ai_model", lambda: "deepseek-v4-pro")
    client = coach_service._make_client(False)
    assert client.model == "glm-4-flash"
    assert "bigmodel.cn" in str(client._client.base_url)


def test_validated_retries_once_then_succeeds(monkeypatch, plan):
    """真实模式 schema 校验失败 → 附提示重试一次后成功。"""
    from runtrainer.services import settings_service
    calls: list[str] = []

    class _Flaky:
        model = "fake"
        def chat_json(self, system, user, data=None):
            calls.append(user)
            if len(calls) == 1:
                return {"reply": 123, "adjustments": [], "profile_updates": {}, "rebuild_plan": False}
            return {"reply": "好的，已按你的要求处理。", "adjustments": [], "profile_updates": {},
                    "rebuild_plan": False}

    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(coach_service, "_make_client", lambda extra=False: _Flaky())
    res = coach_service.chat("帮我把强度课改轻松")
    assert len(calls) == 2, "首次校验失败应重试一次"
    assert "JSON 格式校验" in calls[1]
    assert res["reply"]["content"] == "好的，已按你的要求处理。"


def test_validated_both_fail_raises_friendly(monkeypatch, plan):
    """重试仍失败 → 抛中文可读错误而非裸 pydantic 长文。"""
    from runtrainer.services import settings_service
    calls = []

    class _Bad:
        model = "fake"
        def chat_json(self, system, user, data=None):
            calls.append(user)
            return {"nope": 1}

    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(coach_service, "_make_client", lambda extra=False: _Bad())
    with pytest.raises(RuntimeError, match="不符合契约"):
        coach_service.chat("你好")
    assert len(calls) == 2


# ---------------- 教练聊天 ----------------

class _FakeChatClient:
    """返回预设 ChatOutput 的假客户端。"""

    def __init__(self, output: dict):
        self.output = output
        self.calls: list[dict] = []
        self.model = "deepseek-v4-pro"

    def chat_json(self, system, user, data=None):
        self.calls.append({"system": system, "user": user, "data": data})
        return self.output


def _real_mode(monkeypatch, client):
    from runtrainer.services import settings_service
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(settings_service, "get_deepseek_key", lambda: "test-key")
    monkeypatch.setattr(coach_service, "_make_client", lambda extra=False: client)


def test_chat_mock_reply_stores_messages(monkeypatch, plan):
    from runtrainer.db.repos import chat_repo
    res = coach_service.chat("今天感觉不错")
    assert "模拟模式" in res["reply"]["content"]
    msgs = coach_service.get_chat_history()
    assert [m["role"] for m in msgs] == ["user", "coach"]
    assert msgs[0]["content"] == "今天感觉不错"
    assert len(chat_repo.list_messages()) == 2


def test_chat_adjustment_apply_flow(monkeypatch, plan):
    from runtrainer.db.repos import chat_repo
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    target = next(w for w in ws if w["date"] == d)
    client = _FakeChatClient({
        "reply": "好的，我把那天的课改轻松一点。",
        "adjustments": [{
            "date": d, "planned_workout_id": target["id"], "action": "modify",
            "changes": {"kind": "E", "pace_zone": "E"},
            "reason": "你要求调整",
        }],
        "profile_updates": {}, "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)
    res = coach_service.chat("把 d 那天的课改轻松")
    assert res["reply"]["adjustment_ids"], "聊天提出的调整应落库 pending"
    mid = res["reply"]["id"]
    hist = coach_service.get_chat_history()
    coach_msg = next(m for m in hist if m["id"] == mid)
    assert coach_msg["adjustments"][0]["action"] == "modify"
    # 批准该消息的调整 → 课表生效
    out = coach_service.decide_chat_adjustments(mid, True)
    assert out["applied"] == 1 and out["errors"] == []
    w = plan_repo.get_workout(target["id"])
    assert w["kind"] == "E" and w["source"] == "ai"
    assert adjustment_repo.get_adjustment(res["reply"]["adjustment_ids"][0])["status"] == "applied"
    # 用户消息 + 教练消息都已存
    assert len(chat_repo.list_messages()) == 2


def test_chat_forced_adjustment_auto_applies(monkeypatch, plan):
    """用户强制要求（user_requested=true）：调整直接生效到课表，无需再点批准。"""
    from runtrainer.db.repos import chat_repo
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    target = next(w for w in ws if w["date"] == d)
    client = _FakeChatClient({
        "reply": "好的，按你的要求改。",
        "user_requested": True,
        "adjustments": [{
            "date": d, "planned_workout_id": target["id"], "action": "modify",
            "changes": {"kind": "E", "pace_zone": "E"},
            "reason": "你要求调整",
        }],
        "profile_updates": {}, "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)
    res = coach_service.chat("把 d 那天的课改轻松，不用问我")
    # 回复明确告知已直接改到课表
    assert "直接改到课表" in res["reply"]["content"]
    # 课表已立即变化（无需 approve）
    w = plan_repo.get_workout(target["id"])
    assert w["kind"] == "E" and w["source"] == "ai"
    aid = res["reply"]["adjustment_ids"][0]
    assert adjustment_repo.get_adjustment(aid)["status"] == "applied"
    # 历史消息标记 auto_applied → 前端不再显示批准按钮
    hist = coach_service.get_chat_history()
    coach_msg = next(m for m in hist if m["id"] == res["reply"]["id"])
    assert coach_msg["auto_applied"] is True
    # 已生效的调整不能再被 decide 二次应用
    assert coach_service.decide_chat_adjustments(coach_msg["id"], True)["applied"] == 0
    assert len(chat_repo.list_messages()) == 2


def test_chat_forced_failed_apply_stays_pending(monkeypatch, plan):
    """强制调整自动应用失败（如底层课表已被改动）→ 行保持 pending 不丢审计，可手动批准。"""
    from runtrainer.db.repos import chat_repo
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    target = next(w for w in ws if w["date"] == d)
    client = _FakeChatClient({
        "reply": "好的，按你的要求改。",
        "user_requested": True,
        "adjustments": [{
            "date": d, "planned_workout_id": target["id"], "action": "modify",
            "changes": {"kind": "E", "pace_zone": "E"},
            "reason": "你要求调整",
        }],
        "profile_updates": {}, "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)

    def _boom(plan, r):
        raise RuntimeError("模拟应用失败")
    monkeypatch.setattr(coach_service, "_apply_row", _boom)

    res = coach_service.chat("把那天的课改轻松，不用问我")
    assert "未能自动执行" in res["reply"]["content"]
    aid = res["reply"]["adjustment_ids"][0]
    assert adjustment_repo.get_adjustment(aid)["status"] == "pending"
    hist = coach_service.get_chat_history()
    coach_msg = next(m for m in hist if m["id"] == res["reply"]["id"])
    assert coach_msg["auto_applied"] is False
    assert len(chat_repo.list_messages()) == 2


def test_chat_profile_updates_guarded(monkeypatch, plan):
    from runtrainer.db.repos import profile_repo
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    client = _FakeChatClient({
        "reply": "收到，已按你的说明更新档案。",
        "adjustments": [],
        "profile_updates": {"rest_hr": 45, "max_hr": 999, "unknown_key": 1},
        "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)
    res = coach_service.chat("我的静息心率是 45")
    prof = profile_repo.get_profile() or {}
    assert prof.get("rest_hr") == 45.0
    assert prof.get("max_hr") is None, "超范围 max_hr=999 应被钳制丢弃"
    assert res["profile_updates"] == {"rest_hr": 45.0}
    coach_msg = next(m for m in coach_service.get_chat_history() if m["role"] == "coach")
    assert coach_msg["profile_updates"] == {"rest_hr": 45.0}


def test_chat_empty_message_raises(monkeypatch, plan):
    with pytest.raises(RuntimeError, match="消息不能为空"):
        coach_service.chat("   ")
