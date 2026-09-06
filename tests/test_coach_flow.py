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
    """切换服务商后旧模型名不在候选列表 → 回落该服务商默认模型（glm-4.7-flash 快档）。"""
    from runtrainer.services import settings_service
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(settings_service, "get_ai_provider", lambda: "zhipu")
    monkeypatch.setattr(settings_service, "get_ai_key", lambda provider: "fake-key")
    monkeypatch.setattr(settings_service, "get_ai_model", lambda: "deepseek-v4-pro")
    client = coach_service._make_client(False)
    assert client.model == "glm-4.7-flash"  # 默认快档：显式关思考、回复快（速度优先）
    assert "bigmodel.cn" in str(client._client.base_url)
    # 4.7-flash 必须按模型级配置关闭思考（否则深度思考 1~2 分钟）
    assert client.extra_body == {"thinking": {"type": "disabled"}}
    # 手动切 GLM-5.3-flash：始终思考、不允许关闭（error 1210）→ 不带 extra_body
    monkeypatch.setattr(settings_service, "get_ai_model", lambda: "glm-5.3-flash")
    client = coach_service._make_client(False)
    assert client.model == "glm-5.3-flash"
    assert client.extra_body is None


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
            return {"reply": "好的，我已按你的要求把强度课改轻松：换成轻松跑、配速按 E 区执行，"
                    "时长保持原样，记得充分热身和放松。", "adjustments": [], "profile_updates": {},
                    "rebuild_plan": False}

    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(coach_service, "_make_client", lambda extra=False: _Flaky())
    res = coach_service.chat("帮我把强度课改轻松")
    assert len(calls) == 2, "首次校验失败应重试一次"
    assert "JSON 格式校验" in calls[1]
    assert "强度课改轻松" in res["reply"]["content"]


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
        "reply": "好的，我已把那天的课改成轻松跑：按 E 区配速执行，时长保持原样，"
                 "强度降下来更利于你恢复和积累有氧基础。",
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
        "reply": "好的，已按你的要求执行：那天改轻松跑、配速按 E 区，其余课保持不动，"
                 "注意赛前恢复。",
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
        "reply": "好的，已按你的要求执行：那天改轻松跑、配速按 E 区，其余课保持不动，"
                 "注意赛前恢复。",
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


def test_modify_to_long_run_aligns_title_and_segments(monkeypatch, plan):
    """改成轻松/长距离后，不能残留原质量课的标题与分段（真实 bug：
    kind=LR 但标题仍是「间歇 4×1200m」，日历看起来没变）。"""
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    target = next(w for w in ws if w["date"] == d)
    assert target["kind"] in ("T", "I", "R"), "fixture 质量课"
    client = _FakeChatClient({
        "reply": "好的，我已把那天的课改成长距离：距离保持不变，配速按 E 区慢摇，"
                 "前 10 分钟充分热身再进入主题。",
        "adjustments": [{
            "date": d, "planned_workout_id": target["id"], "action": "modify",
            "changes": {"kind": "LR", "pace_zone": "E"},
            "reason": "你要求改成轻松长距离",
        }],
        "profile_updates": {}, "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)
    res = coach_service.chat("把那天的课改成长距离")
    out = coach_service.decide_chat_adjustments(res["reply"]["id"], True)
    assert out["applied"] == 1
    w = plan_repo.get_workout(target["id"])
    assert w["kind"] == "LR"
    assert "长距离" in w["title"], f"标题应改为长距离，实际: {w['title']}"
    assert "间歇" not in (w["title"] or "") and "阈值" not in (w["title"] or ""), \
        f"不得残留质量课标题: {w['title']}"
    assert w["segments_json"] is None, "旧的分段（间歇组/阈值段）应被清空"
    assert "轻松长距离" in (w["description"] or ""), "描述应替换为调整原因"


def test_chat_profile_updates_guarded(monkeypatch, plan):
    from runtrainer.db.repos import profile_repo
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    client = _FakeChatClient({
        "reply": "收到，我已按你的说明更新了档案数据：后续训练配速与强度区间"
                 "会按新的数值重新计算。",
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


# ---------------- 第四批：同步后自动分析 + 对话读取训练数据 ----------------

def _insert_activity(ts: int, external_id: str = "act-new-1",
                     distance_m: float = 8000.0) -> int:
    from runtrainer.db.repos import activity_repo
    aid, is_new = activity_repo.upsert_activity({
        "source": "garmin", "external_id": external_id, "file_path": None,
        "name": "晨跑", "sport": "跑步", "start_ts": ts, "tz_offset_min": 0,
        "duration_s": 2700, "distance_m": distance_m, "avg_pace_s_km": 337.5,
        "avg_hr": 145.0, "max_hr": 172.0, "avg_cadence": 178.0,
        "aerobic_te": 3.2, "anaerobic_te": 0.5, "exercise_load": 120.0,
        "laps_json": None, "has_samples": 0,
    })
    assert is_new
    return aid


def test_sync_analysis_creates_coach_message(monkeypatch, plan):
    """同步带来新训练 → 自动生成分析消息（kind=sync_analysis）+ 可批准的调整建议；
    游标推进后重复同步不再重复分析。"""
    from runtrainer.db.repos import chat_repo, sync_repo
    from runtrainer.utils import jsonutil
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    target = next(w for w in ws if w["date"] == d)
    ts = dates.date_to_ts(dates.date.fromisoformat(d)) + 8 * 3600
    _insert_activity(ts)
    client = _FakeChatClient({
        "reply": "今天的训练执行得不错：8km 配速 5:38/km，心率 145 稳定在合理区间。"
                 "未来几天保持轻松跑节奏，注意补足睡眠与碳水，周四的强度课按课表正常执行。",
        "adjustments": [{
            "date": d, "planned_workout_id": target["id"], "action": "modify",
            "changes": {"kind": "E", "pace_zone": "E"},
            "reason": "新数据负荷偏高，建议把今天的强度课改轻松",
        }],
        "profile_updates": {}, "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)
    res = coach_service.auto_analyze_new_activities([("act-new-1", ts)], client=client)
    assert res and res["adjustment_count"] == 1
    # 教练消息 kind=sync_analysis，调整落 pending（训练者没要求改课，不自动生效）
    msgs = chat_repo.list_messages()
    assert len(msgs) == 1 and msgs[0]["role"] == "coach"
    assert msgs[0]["kind"] == "sync_analysis"
    view = coach_service.get_chat_history()[0]
    assert view["kind"] == "sync_analysis" and view["auto_applied"] is False
    assert view["adjustments"][0]["status"] == "pending"
    # 提示词包含新增活动精确数据
    assert "本次同步新增的训练数据" in client.calls[0]["user"]
    assert "晨跑" in client.calls[0]["user"] and "配速5:38/km" in client.calls[0]["user"]
    # 游标推进 → 同一批活动再同步不重复分析
    meta = jsonutil.loads(sync_repo.get_sync_state("garmin")["meta_json"])
    assert meta["last_analysis_act_ts"] == ts
    assert coach_service.auto_analyze_new_activities([("act-new-1", ts)], client=client) is None
    # 批准建议 → 课表生效
    out = coach_service.decide_chat_adjustments(view["id"], True)
    assert out["applied"] == 1
    assert plan_repo.get_workout(target["id"])["kind"] == "E"


def test_sync_analysis_no_adjustments_still_posts(monkeypatch, plan):
    """新数据无需调整时：只发分析总结消息，不给空调整。"""
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    ts = dates.date_to_ts(dates.date.fromisoformat(d)) + 8 * 3600
    _insert_activity(ts)
    client = _FakeChatClient({
        "reply": "这次跑得很好：配速与心率都在有氧区间，训练效果符合预期。"
                 "接下来几天继续保持轻松节奏，注意睡眠恢复，下节课正常执行即可。",
        "adjustments": [],
        "profile_updates": {}, "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)
    res = coach_service.auto_analyze_new_activities([("act-new-1", ts)], client=client)
    assert res and res["adjustment_count"] == 0
    view = coach_service.get_chat_history()[0]
    assert view["kind"] == "sync_analysis" and "这次跑得很好" in view["content"]
    assert view["adjustments"] == []


def test_sync_analysis_skips_mock_mode(monkeypatch, plan):
    """mock 模式不同步触发自动分析（不产生假消息）。"""
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    ts = dates.date_to_ts(dates.date.fromisoformat(d)) + 8 * 3600
    _insert_activity(ts)
    assert coach_service.auto_analyze_new_activities([("act-new-1", ts)]) is None


def test_chat_context_includes_recent_activity_details(monkeypatch, plan):
    """对话时教练上下文包含近 7 天训练活动精确数据（距离/配速/心率/步频/TE），
    用户问「今天跑得怎么样」教练能引用真实数字。"""
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    ts = dates.date_to_ts(dates.date.fromisoformat(d)) + 7 * 3600
    _insert_activity(ts, distance_m=12345.0)
    client = _FakeChatClient({
        "reply": "你今天完成了 12.3 公里，配速 5:38/km，心率 145 处于有氧区间，"
                 "整体执行质量不错，注意拉伸与补水。",
        "adjustments": [],
        "profile_updates": {}, "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)
    coach_service.chat("我今天跑得怎么样？")
    user = client.calls[0]["user"]
    assert "最近训练活动详情" in user
    assert "12.3" in user and "km" in user and "配速5:38/km" in user
    assert "心率145" in user and "步频178" in user and "训练效果3.2/0.5" in user
    assert "负荷120" in user


def test_chat_context_plan_vs_actual_athlete_and_refs(monkeypatch, plan):
    """第十三批：上下文含计划 vs 实际完成对照（计划课当天只跑 12km 也能
    在同一行看到两侧数字）、档案身高体重、系统算好的数据参考块
    （心率区/步频步幅；无健康数据时不虚构 HRV 基线行）。"""
    from runtrainer.db.repos import profile_repo
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    ts = dates.date_to_ts(dates.date.fromisoformat(d)) + 8 * 3600
    _insert_activity(ts, distance_m=12000.0)   # 「计划 22km 只跑了 12km」场景
    profile_repo.upsert_profile({"weight_kg": 65.0, "height_cm": 172.0,
                                 "max_hr": 190, "rest_hr": 50})
    client = _FakeChatClient({
        "reply": "今天说好的长距离只跑了一部分，别背包袱——从完成量看强度已经积累得差不多，"
                 "明天按轻松跑恢复就好，缺口我们留到下周长距离课再补。",
        "adjustments": [], "profile_updates": {}, "rebuild_plan": False})
    _real_mode(monkeypatch, client)
    coach_service.chat("今天说好的长距离我只跑了 12km，要紧吗？")
    user = client.calls[0]["user"]
    assert "过去 7 天计划完成情况" in user
    assert "→ 实际" in user and "12.0km" in user   # 实际完成与计划同窗对照
    assert "身高 172cm" in user and "体重 65kg" in user   # 营养/健康问题的换算依据
    assert "【数据参考" in user
    assert "储备心率法" in user and "恢复跑 <" in user and "轻松跑 " in user
    assert "平均步频 178 spm" in user and "平均步幅 1.5 m" in user
    assert "HRV 基线" not in user   # 该场景无健康数据，不虚构基线数字


# ---------------- 第十一批：复述防护 + 清空对话（保留记忆） ----------------

class _SequenceChatClient:
    """按顺序返回预设输出的假客户端（复述重试场景）。"""

    def __init__(self, outputs: list[dict]):
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        self.model = "glm-4-flash"

    def chat_json(self, system, user, data=None):
        self.calls.append({"system": system, "user": user, "data": data})
        return self.outputs.pop(0)


_ECHO_REPLY = {"reply": "分析总结+未来几天建议（中文，像教练聊天，分段清楚）",
               "adjustments": [], "profile_updates": {}, "rebuild_plan": False}


def test_sync_analysis_prompt_echo_retried_once(monkeypatch, plan):
    """glm-4-flash 复述提示词格式描述 → 附反复述提示重试一次 → 正常回复落库。"""
    from runtrainer.db.repos import chat_repo
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    ts = dates.date_to_ts(dates.date.fromisoformat(d)) + 8 * 3600
    _insert_activity(ts)
    client = _SequenceChatClient([
        _ECHO_REPLY,
        {"reply": "今天完成 8km 轻松跑，配速 5:38/km，心率 145 处于有氧区间，执行到位。"
                  "近 8 周负荷平稳，接下来几天保持轻松跑节奏，把睡眠补足到 7 小时以上。",
         "adjustments": [], "profile_updates": {}, "rebuild_plan": False},
    ])
    _real_mode(monkeypatch, client)
    res = coach_service.auto_analyze_new_activities([("act-new-1", ts)], client=client)
    assert res and res["message_id"]
    assert len(client.calls) == 2
    assert "不要复述" in client.calls[1]["user"]   # 重试时附了反复述提示
    view = coach_service.get_chat_history()[0]
    assert "5:38" in view["content"]
    assert chat_repo.list_messages()[0]["kind"] == "sync_analysis"


def test_sync_analysis_echo_twice_blocks_message(monkeypatch, plan):
    """连续两次复述提示词 → 拦截不落库（垃圾消息不再出现），游标不推进可重试。"""
    from runtrainer.db.repos import chat_repo, sync_repo
    from runtrainer.utils import jsonutil
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    ts = dates.date_to_ts(dates.date.fromisoformat(d)) + 8 * 3600
    _insert_activity(ts)
    client = _SequenceChatClient([_ECHO_REPLY, _ECHO_REPLY])
    _real_mode(monkeypatch, client)
    with pytest.raises(RuntimeError, match="复述"):
        coach_service.auto_analyze_new_activities([("act-new-1", ts)], client=client)
    assert len(client.calls) == 2
    assert coach_service.get_chat_history() == []   # 垃圾消息不落库
    state = sync_repo.get_sync_state("garmin") or {}
    meta = jsonutil.loads(state.get("meta_json")) if state.get("meta_json") else {}
    assert meta.get("last_analysis_act_ts") is None  # 游标不推进，下次同步重试


def test_chat_prompt_echo_blocked(monkeypatch, plan):
    """聊天里复述占位文字（如「回复文字」）→ 重试后仍复述 → 拦截，不产生对话记录。"""
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    echo = {"reply": "回复文字", "user_requested": False, "adjustments": [],
            "profile_updates": {}, "rebuild_plan": False}
    client = _SequenceChatClient([echo, echo])
    _real_mode(monkeypatch, client)
    with pytest.raises(RuntimeError, match="复述"):
        coach_service.chat("我今天跑得怎么样？")
    assert coach_service.get_chat_history() == []


def test_clear_chat_history_hides_ui_keeps_memory(monkeypatch, plan):
    """清空对话：UI 不再显示旧消息，但 AI 上下文仍读取（保留教练记忆）。"""
    from runtrainer.db.repos import chat_repo
    p, ws = plan
    d = _hard_date(ws)
    _patch_today(monkeypatch, dates.date.fromisoformat(d))
    client = _FakeChatClient({
        "reply": "这是一条比较详细的教练回复，包含了对今天训练的分析以及未来几天的建议，"
                 "内容足够长，请继续保持。",
        "adjustments": [], "profile_updates": {}, "rebuild_plan": False,
    })
    _real_mode(monkeypatch, client)
    coach_service.chat("你好，我昨天跑得有点累")
    assert len(coach_service.get_chat_history()) == 2
    assert coach_service.clear_chat_history()["hidden"] == 2
    assert coach_service.get_chat_history() == []          # UI 不再显示
    assert all(m["hidden"] == 1 for m in chat_repo.list_messages())  # 消息仍在
    # 清空后继续对话：教练上下文仍包含此前交流（记忆保留）
    coach_service.chat("还记得我之前说的吗？")
    assert len(client.calls) == 2
    assert "跑得有点累" in client.calls[1]["user"]
