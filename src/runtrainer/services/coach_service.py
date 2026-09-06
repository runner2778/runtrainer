"""AI 教练服务：采集上下文 → 提示词 → DeepSeek → 契约校验 → 护栏 → 落库 → 批准/拒绝。

当日已有建议直接返回缓存（防重复计费）；全被护栏拦截时回退原计划（记录一条 keep 审计）。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from pydantic import ValidationError

from ..ai.contracts import ChatOutput, CoachOutput
from ..ai.deepseek_client import (PROVIDERS, DeepSeekClient, MockClient,
                                  resolve_extra_body)
from ..ai import guardrails, prompt_builder
from ..db.repos import (adjustment_repo, activity_repo, chat_repo, goal_repo, health_repo,
                        kv_repo, plan_repo, profile_repo)
from ..domain import load_metrics, vdot as vd, workout_analysis
from ..domain.plan_engine import PHASE_ORDER
from ..utils import dates, jsonutil

log = logging.getLogger(__name__)

CACHE_PREFIX = "coach:"
ADJUSTED_STATUS = {"rest": "skipped", "skip": "skipped"}


def _make_client(extra_requested: bool):
    """按设置的服务商构造 AI 客户端：DeepSeek（付费）/ 智谱 GLM-4.7-Flash（免费）/ Ollama（本地免费）。"""
    from . import settings_service
    if not settings_service.is_mock_mode():
        provider = settings_service.get_ai_provider()
        info = PROVIDERS[provider]
        key = settings_service.get_ai_key(provider)
        if info.get("needs_key") and not key:
            raise RuntimeError(
                f"未配置 {info['label']} 的 API Key，请在设置页配置；"
                "或改选「智谱 GLM（免费）」/「Ollama 本地」免费方案")
        model = settings_service.get_ai_model()
        # 模型不在该服务商候选且服务商非自由输入 → 回落默认模型
        if not info.get("free_text") and model not in info["models"]:
            model = info["models"][0]
        return DeepSeekClient(key or "", model, base_url=info["base_url"],
                              max_tokens=info.get("max_tokens", 4096),
                              extra_body=resolve_extra_body(provider, model))
    if extra_requested:
        return MockClient("add_extra")
    return MockClient(["normal", "low_hrv", "overload"][dates.today().toordinal() % 3])


def _phase_for_week(plan: dict, week_index: int) -> str:
    pw = jsonutil.loads(plan["phase_weeks"]) or {}
    acc = 0
    for p in PHASE_ORDER:
        acc += int(pw.get(p) or 0)
        if week_index < acc:
            return p
    return PHASE_ORDER[-1]


def _gather(today: date, extra_requested: bool, user_note: str) -> dict | None:
    """收集上下文；无 active 计划返回 None。"""
    plan = plan_repo.get_active_plan()
    if not plan:
        return None
    goal = goal_repo.get_active_goal() or {}
    profile = profile_repo.get_profile() or {}
    vdot_val = float(plan["vdot"])
    paces = vd.pace_table(vdot_val)

    ws_start = today - timedelta(days=today.weekday())
    ws_end = ws_start + timedelta(days=6)
    horizon = today + timedelta(days=6)
    workouts = plan_repo.get_workouts(
        plan["id"], ws_start.isoformat(), max(ws_end, horizon).isoformat())
    for w in workouts:
        w["is_quality"] = guardrails.is_hard(w["kind"], w.get("pace_zone"))

    week_km = sum(float(w.get("distance_km") or 0) for w in workouts
                  if ws_start <= date.fromisoformat(w["date"]) <= ws_end)
    today_workouts = [w for w in workouts if w["date"] == today.isoformat()]
    today_workout = today_workouts[0] if today_workouts else None
    anchor = today_workout or next(
        (w for w in workouts if w["date"] >= today.isoformat()), workouts[-1] if workouts else None)
    current_week = int(anchor["week_index"]) if anchor else 0
    current_phase = anchor["phase"] if anchor else _phase_for_week(plan, current_week)

    acts = activity_repo.list_activities(
        (today - timedelta(days=180)).isoformat(), limit=2000)
    sessions = [
        {"date": dates.ts_to_date(a["start_ts"]).isoformat(),
         "distance_km": (a.get("distance_m") or 0) / 1000,
         "duration_min": (a.get("duration_s") or 0) / 60,
         "sport": a.get("sport")}
        for a in acts if a.get("distance_m")
    ]
    weekly = load_metrics.weekly_totals(sessions)[-8:]
    # 近 7 天计划执行覆盖：按计划日与实际跑步逐日配对、逐日封顶，窗口夹在
    # 课表生命周期 [start_date, race_date] 内——开课前课表没课可执行（覆盖
    # 为空），计划外的自由跑不撑执行率（AI 拿到的是诚实数字）
    run_sessions = [s for s in sessions if load_metrics.is_running(s.get("sport"))]
    runs_day = load_metrics.run_days_from_sessions(run_sessions)
    planned7 = [{"date": w["date"], "distance_km": w.get("distance_km")}
                for w in workouts
                if plan["start_date"] <= w["date"] <= plan["race_date"]
                and (today - timedelta(days=6)).isoformat() <= w["date"] <= today.isoformat()]
    recent = {
        "weekly_km": [round(w["km"], 1) for w in weekly],
        "acwr": load_metrics.acwr(sessions, today),
        "monotony": load_metrics.monotony(sessions, today),
        "strain": load_metrics.strain(sessions, today),
        "compliance_7d": load_metrics.plan_coverage(planned7, runs_day),
    }

    # 最近训练活动精确详情（聊天/自动分析回答训练问题的数据依据，最新在前）。
    # 窗口放宽到 30 天（上限 15 条）：训练者追问「上周三/十天前那次跑」时
    # AI 拿得到数据，不再只覆盖一周
    recent_acts = [a for a in activity_repo.list_activities(
        (today - timedelta(days=30)).isoformat(),
        (today + timedelta(days=1)).isoformat(), limit=15) if a.get("distance_m")]

    # 过去 7 天「计划 vs 实际」：按日期把计划课与当天实际训练配对（不依赖
    # 用户手动标记完成）——AI 回答「计划 22km 我只跑了 12km 要紧吗」这类
    # 完成度/质疑问题时，计划侧与实际侧的精确数字必须同时在场
    hist7 = plan_repo.get_workouts(
        plan["id"], (today - timedelta(days=7)).isoformat(), today.isoformat()) if plan else []
    pa_acts = activity_repo.list_activities(
        (today - timedelta(days=7)).isoformat(),
        (today + timedelta(days=1)).isoformat(), limit=200)
    acts_by_day: dict[str, list[dict]] = {}
    for a in pa_acts:
        if a.get("distance_m"):
            acts_by_day.setdefault(dates.ts_to_date(a["start_ts"]).isoformat(), []).append(a)
    plan_actual: list[dict] = []
    for w in sorted(hist7, key=lambda x: x["date"]):
        if not w.get("distance_km"):
            continue
        acts = acts_by_day.get(w["date"]) or []
        slot = int(w.get("slot") or 1)
        actual = (acts[slot - 1] if len(acts) >= slot
                  else (acts[0] if acts and slot == 1 else None))
        plan_actual.append({"w": w, "acts": [actual] if actual else []})

    # 配速-心率对照（AI 判断进步/退步的依据）：近 180 天同配速档心率趋势 + 近 8 周平均
    phr_start = today - timedelta(days=180)
    pace_hr_trend = (workout_analysis.pace_bin_hr(acts, phr_start, today)
                     .get("summary") or {})
    pace_hr_weekly = workout_analysis.weekly_pace_hr(acts, phr_start, today)[:8]

    health_rows = health_repo.get_health(
        (today - timedelta(days=13)).isoformat(), today.isoformat())
    health = [{
        "date": h["date"],
        "sleep_duration_h": round(h["sleep_duration_s"] / 3600, 1) if h.get("sleep_duration_s") else None,
        "sleep_score": h.get("sleep_score"),
        "hrv_avg_ms": h.get("hrv_avg_ms"),
        "hrv_status": h.get("hrv_status"),
        "resting_hr": h.get("resting_hr"),
        "stress_avg": h.get("stress_avg"),
        "body_battery_min": h.get("body_battery_min"),
    } for h in health_rows]

    # 数据参考（建议与理由必须引用具体数字；系统算好，AI 不得自造数值）：
    # 心率参考区间按与课程分类相同的口径（静息心率已知用储备心率 %HRR，
    # 否则 %HRmax，见 workout_analysis.HR_ZONE_KINDS）
    refs: dict = {}
    max_hr = profile.get("max_hr")
    rest_hr = profile.get("rest_hr")
    if max_hr:
        use_hrr = bool(rest_hr and max_hr > rest_hr)

        def _bpm(frac: float) -> int:
            return round(rest_hr + frac * (max_hr - rest_hr)) if use_hrr \
                else round(max_hr * frac)

        zones = []
        for name, lo, hi, label in workout_analysis.HR_ZONE_KINDS:
            if name == "recovery":
                zones.append(f"{label} <{_bpm(hi)} bpm")
            elif name == "anaerobic":
                zones.append(f"{label} ≥{_bpm(lo)} bpm")
            else:
                zones.append(f"{label} {_bpm(lo)}–{_bpm(hi)} bpm")
        refs["hr_zone_lines"] = zones
        refs["hr_zone_desc"] = (
            f"{'储备心率法（静息 {rest_hr}）' if use_hrr else '% 最大心率'}，最大 {max_hr} bpm")
    hrv_vals = [h["hrv_avg_ms"] for h in health if h.get("hrv_avg_ms")]
    if hrv_vals:
        refs["hrv_base"] = {"avg": round(sum(hrv_vals) / len(hrv_vals), 1),
                            "min": min(hrv_vals), "max": max(hrv_vals), "n": len(hrv_vals)}
    cad_list: list[float] = []
    stride_list: list[float] = []
    for a in pa_acts:
        cad = a.get("avg_cadence")
        if cad and 120 <= cad <= 240:
            cad_list.append(cad)
            dur = a.get("duration_s") or 0
            dist = a.get("distance_m") or 0
            if dur > 60 and dist > 0:
                v = dist / dur  # m/s
                stride = v * 60 / cad  # m/步
                if 0.2 <= stride <= 2.5:
                    stride_list.append(stride)
    if cad_list:
        refs["cadence_stride"] = {
            "cadence": round(sum(cad_list) / len(cad_list)),
            "stride": round(sum(stride_list) / len(stride_list), 2) if stride_list else None,
            "n": len(cad_list)}

    applied = adjustment_repo.list_adjustments(plan["id"], status="applied", limit=500)
    add_count = sum(
        1 for a in applied if a.get("action") == "add_easy" and a.get("applies_date")
        and ws_start <= date.fromisoformat(a["applies_date"]) <= ws_end)

    race_date = date.fromisoformat(plan["race_date"])
    return {
        "today": today.isoformat(),
        "athlete": profile,
        "goal": {"name": goal.get("name"), "distance_m": goal.get("distance_m"),
                 "race_date": plan["race_date"],
                 "target_seconds": goal.get("target_seconds"), "vdot": vdot_val},
        "vdot": vdot_val,
        "race_in_days": (race_date - today).days,
        "plan": {"start_date": plan["start_date"], "race_date": plan["race_date"],
                 "total_weeks": plan["total_weeks"], "current_week": current_week + 1,
                 "current_phase": current_phase, "week_km": round(week_km, 1)},
        "paces": paces,
        "today_workout": today_workout,
        "today_workouts": today_workouts,
        "week_workouts": workouts,
        "recent": recent,
        "recent_acts": recent_acts,
        "plan_actual": plan_actual,
        "health": health,
        "refs": refs,
        "pace_hr_trend": pace_hr_trend,
        "pace_hr_weekly": pace_hr_weekly,
        "extra_requested": extra_requested,
        "user_note": user_note,
        # 护栏上下文
        "guard": {
            "today": today, "race_date": race_date,
            "week_start": ws_start, "week_end": ws_end, "week_km": week_km,
            "workouts": workouts, "paces": paces,
            "add_extra_count_this_week": add_count,
            "extra_requested": extra_requested,
        },
    }


def _persist_batch(ctx: dict, output: CoachOutput, items: list[dict], guard_log: list[str],
                   model: str, prompt: dict, extra_requested: bool) -> list[int]:
    """调整项逐条落库（pending），全被护栏丢弃时记录一条 keep 回退。返回 ids。"""
    plan = plan_repo.get_active_plan()
    plan_id = plan["id"]
    today = ctx["today"]
    output_json = output.model_dump(mode="json")
    input_json = {"system": prompt["system"], "user": prompt["user"]}
    ids: list[int] = []
    rows = []
    for it in items:
        rows.append({
            "plan_id": plan_id, "workout_id": it.get("planned_workout_id"),
            "applies_date": it["date"], "action": it["action"],
            "changes_json": it.get("changes"), "reason": it["reason"],
            "ai_model": model, "ai_input_json": input_json,
            "ai_output_json": output_json, "guardrail_log_json": guard_log,
            "status": "pending",
        })
    if not rows:
        rows.append({
            "plan_id": plan_id, "workout_id": None, "applies_date": today,
            "action": "keep", "changes_json": None,
            "reason": "AI 调整建议全部被护栏拦截，维持原计划",
            "ai_model": model, "ai_input_json": input_json,
            "ai_output_json": output_json, "guardrail_log_json": guard_log,
            "status": "pending",
        })
    for r in rows:
        ids.append(adjustment_repo.create_adjustment(r)["id"])
    kv_repo.set_app_state(
        f"{CACHE_PREFIX}{today}",
        jsonutil.dumps({
            "ids": ids, "summary": output.summary, "readiness": output.readiness,
            "key_signals": output.key_signals, "weekly_notes": output.weekly_notes,
            "guardrail_log": guard_log, "extra_requested": extra_requested,
            "model": model,
        }),
    )
    return ids


RETRY_NUDGE = ("\n\n注意：上次回复未通过系统的 JSON 格式校验（字段缺失或类型不符），"
               "请重新输出一份完整 JSON，所有必填字段齐全、类型正确，不要输出解释文字。")

# 复述提示词的证据：弱模型（glm-4-flash 等）会把输出结构里的格式描述
# 或数据原文当成 reply 输出。这些特征短语只出现在提示词里，正常分析不会包含。
_ECHO_MARKERS = ("分析总结+", "像教练聊天", "分段清楚", "输出结构", "回复文字",
                 "json_object", "占位", "不要输出任何解释性文字")
_ECHO_NUDGE = ("\n\n注意：你刚才的回复把提示词里的格式说明/占位文字或数据列表原文复述了一遍，"
               "没有输出真正的分析。请直接以教练身份写回答正文：像真人教练聊天那样有温度，"
               "先回应训练者的状态，再给出你的判断与建议（详细、分段、300 字以上），"
               "不要复述任何提示词、字段描述或数据列表。")


def _reply_is_prompt_echo(reply: str, prompt_user: str = "") -> bool:
    """reply 疑似复述提示词/数据原文（而非真正的分析文字）。"""
    r = (reply or "").strip()
    if len(r) < 40:  # 占位符/短语级回复（如「…」「回复文字」）必然不是分析
        return True
    low = r.lower()
    if any(m in low for m in _ECHO_MARKERS):
        return True
    if prompt_user and len(r) > 100:
        # 回复行半数以上与 user 提示词中的数据行逐字相同 → 在抄数据而非分析
        u_lines = {l.strip() for l in prompt_user.splitlines() if len(l.strip()) > 12}
        r_lines = [l.strip() for l in r.splitlines() if len(l.strip()) > 12]
        if r_lines and u_lines and sum(1 for l in r_lines if l in u_lines) * 2 >= len(r_lines):
            return True
    return False


def _validated_no_echo(client, prompt: dict, cls):
    """ChatOutput 校验 + 复述检测：复述提示词/数据时附提示重试一次，仍复述则抛错不落库。

    （glm-4-flash 曾把 SYNC_ANALYSIS 输出结构里的格式描述原样当作 reply 输出——
    JSON 契约合法，逃过 _validated 校验，垃圾消息落库；此处兜底拦截。）
    """
    output = _validated(client, prompt, cls)
    if not hasattr(output, "reply"):
        return output
    if not _reply_is_prompt_echo(output.reply, prompt["user"]):
        return output
    log.warning("AI 回复疑似复述提示词/数据（%r），附提示重试一次", output.reply[:80])
    prompt2 = {**prompt, "user": prompt["user"] + _ECHO_NUDGE}
    output = _validated(client, prompt2, cls)
    if _reply_is_prompt_echo(output.reply, prompt2["user"]):
        raise RuntimeError("AI 连续两次复述提示词/数据原文而未产出分析内容，"
                           "已拦截该条消息（可稍后重试或更换模型）")
    return output


def _validated(client, prompt: dict, cls) -> "CoachOutput | ChatOutput":
    """调用 AI 并校验输出契约；格式不符时附提示重试一次，仍失败抛中文错误。"""
    def _call(user: str):
        return cls.model_validate(client.chat_json(prompt["system"], user, prompt["data"]))

    try:
        return _call(prompt["user"])
    except ValidationError as e:
        log.warning("AI 输出未通过 %s 校验（%s），附提示重试一次", cls.__name__, e.errors()[:2])
        try:
            return _call(prompt["user"] + RETRY_NUDGE)
        except ValidationError as e2:
            raise RuntimeError(
                f"AI 输出不符合契约，附格式提示重试后仍失败：{e2.errors()[:2]}") from e2


def request_advice(extra_requested: bool = False, user_note: str = "") -> dict:
    """触发一次 AI 建议（当日已有则直接返回缓存）。"""
    today = dates.today()
    cached = kv_repo.get_app_state(f"{CACHE_PREFIX}{today.isoformat()}")
    if cached:
        return get_snapshot()

    ctx = _gather(today, extra_requested, user_note)
    if ctx is None:
        raise RuntimeError("尚未创建训练计划，请先到“训练目标”页生成课表")

    client = _make_client(extra_requested)
    prompt = prompt_builder.build(ctx)
    output = _validated(client, prompt, CoachOutput)

    # 规则 9：请求加练但缺 add_extra_advice → 附提示重试一次
    if extra_requested and output.add_extra_advice is None:
        prompt2 = {**prompt, "user": prompt["user"]
                   + "\n\n注意：用户今天请求了加练，输出必须包含 add_extra_advice 字段。"}
        output = _validated(client, prompt2, CoachOutput)

    g = ctx["guard"]
    items, guard_log = guardrails.validate(output, guardrails.GuardContext(**g))
    model = getattr(client, "model", "mock")
    _persist_batch(ctx, output, items, guard_log, model, prompt, extra_requested)
    return get_snapshot()


def get_snapshot() -> dict:
    """教练页数据：今日建议（含缓存）+ 历史。"""
    today = dates.today().isoformat()
    plan = plan_repo.get_active_plan()
    advice = None
    cached = kv_repo.get_app_state(f"{CACHE_PREFIX}{today}")
    if cached:
        data = jsonutil.loads(cached)
        rows = [adjustment_repo.get_adjustment(i) for i in data.get("ids", [])]
        advice = {
            "summary": data.get("summary"), "readiness": data.get("readiness"),
            "key_signals": data.get("key_signals") or [],
            "weekly_notes": data.get("weekly_notes") or "",
            "guardrail_log": data.get("guardrail_log") or [],
            "extra_requested": bool(data.get("extra_requested")),
            "model": data.get("model"),
            "adjustments": [_row_view(r) for r in rows if r],
        }
    return {
        "today": today,
        "has_active_plan": bool(plan),
        "advice": advice,
        "history": [_row_view(r) for r in adjustment_repo.list_adjustments(
            plan["id"] if plan else None, limit=30)],
    }


def _row_view(r: dict) -> dict:
    w = plan_repo.get_workout(r["workout_id"]) if r.get("workout_id") else None
    return {
        "id": r["id"], "applies_date": r.get("applies_date"), "action": r["action"],
        "changes": jsonutil.loads(r.get("changes_json")),
        "reason": r.get("reason"), "ai_model": r.get("ai_model"),
        "guardrail_log": jsonutil.loads(r.get("guardrail_log_json")),
        "status": r["status"], "created_at": r.get("created_at"),
        "decided_at": r.get("decided_at"), "workout_id": r.get("workout_id"),
        "workout": {"kind": w["kind"], "title": w["title"],
                    "distance_km": w.get("distance_km"),
                    "duration_min": w.get("duration_min"),
                    "pace_zone": w.get("pace_zone")} if w else None,
    }


def decide_advice(approve: bool) -> dict:
    """决定今日建议批次的 pending 项；批准则逐条应用到课表。

    只处理今日缓存批次（聊天提出的调整按消息单独决定，不在此扫入）。
    """
    plan = plan_repo.get_active_plan()
    if not plan:
        raise RuntimeError("没有活动计划")
    cached = kv_repo.get_app_state(f"{CACHE_PREFIX}{dates.today().isoformat()}")
    ids = jsonutil.loads(cached).get("ids", []) if cached else None
    if ids is None:  # 旧数据兼容：无缓存时取全部 pending
        rows = adjustment_repo.list_adjustments(plan["id"], status="pending", limit=200)
    else:
        rows = [r for i in ids if (r := adjustment_repo.get_adjustment(i))
                and r["status"] == "pending"]
    if not rows:
        raise RuntimeError("没有待处理的建议")
    applied = 0
    errors: list[str] = []
    for r in rows:
        if approve:
            try:
                _apply_row(plan, r)
                adjustment_repo.set_applied(r["id"])
                applied += 1
            except Exception as e:
                log.exception("应用调整 %s 失败", r["id"])
                errors.append(f"#{r['id']} {r['action']}: {e}")
        else:
            adjustment_repo.decide_adjustment(r["id"], "rejected")
    return {"applied": applied, "rejected": 0 if approve else len(rows),
            "errors": errors}


# 质量课特征（用于识别"标题/分段仍是旧质量课内容"的残留）
_QUALITY_TEXT_MARKERS = ("间歇", "阈值", "亚阈", "重复跑", "冲刺", "跨步",
                        "马拉松配速", "配速跑", "测试", "比赛")
_QUALITY_SEGMENT_TYPES = {"tempo", "reps", "strides"}


def _align_workout_content(w: dict, r: dict) -> None:
    """AI 把课改成轻松类（LR/E/RECOVERY）后，若标题/描述/分段还停留在原质量课
    内容上（如原「间歇 4×1200m」只改了 kind），自动对齐：
    标题换成长距离/轻松跑/放松跑，描述改为调整原因，旧分段清空。
    """
    kind = w.get("kind")
    if kind not in ("LR", "E", "RECOVERY"):
        return
    title = (w.get("title") or "").strip()
    if kind == "LR":
        need = "长距离" not in title
    else:
        desc = w.get("description") or ""
        text = title + " " + desc
        segs = w.get("segments_json")
        try:
            seg_list = jsonutil.loads(segs) if isinstance(segs, str) else (segs or [])
        except Exception:
            seg_list = []
        need = any(m in text for m in _QUALITY_TEXT_MARKERS) or bool(
            {s.get("type") for s in seg_list if isinstance(s, dict)} & _QUALITY_SEGMENT_TYPES)
    if not need:
        return
    dist = float(w.get("distance_km") or 0)
    dur = float(w.get("duration_min") or 0)
    if kind == "LR":
        w["title"] = f"长距离 {dist:g}km" if dist else f"长距离 {dur:g} 分钟"
    elif kind == "RECOVERY":
        w["title"] = f"放松跑 {dur:g} 分钟" if dur else "放松跑"
    else:
        w["title"] = f"轻松跑 {dur:g} 分钟" if dur else "轻松跑"
    w["description"] = f"教练按你的要求调整：{r.get('reason') or '轻松跑'}"[:300]
    w["segments_json"] = None


def _apply_row(plan: dict, r: dict) -> None:
    action = r["action"]
    changes = jsonutil.loads(r.get("changes_json")) or {}
    if action == "keep":
        return
    if action in ("rest", "skip"):
        if r.get("workout_id"):
            plan_repo.set_workout_status(r["workout_id"], ADJUSTED_STATUS[action], None)
        return
    if action in ("modify", "decrease"):
        w = plan_repo.get_workout(r["workout_id"])
        if not w:
            raise RuntimeError("课表不存在")
        for k, v in changes.items():
            if k in w:
                w[k] = v
        if action == "modify":
            _align_workout_content(w, r)   # 类型改轻松类后清理残留的质量课标题/分段
        w["source"] = "ai"
        w["adjustment_id"] = r["id"]
        plan_repo.update_workout(r["workout_id"], w)
        return
    if action == "shift":
        w = plan_repo.get_workout(r["workout_id"])
        if not w:
            raise RuntimeError("课表不存在")
        nd = date.fromisoformat(changes["date"])
        w["date"] = nd.isoformat()
        w["week_index"] = (nd - date.fromisoformat(plan["start_date"])).days // 7
        w["source"] = "ai"
        w["adjustment_id"] = r["id"]
        plan_repo.update_workout(r["workout_id"], w)
        return
    if action == "add_easy":
        d = date.fromisoformat(r["applies_date"])
        kind = changes.get("kind") or "E"
        dur = changes.get("duration_min") or 30.0
        dist = changes.get("distance_km")
        if not dist:
            paces = vd.pace_table(float(plan["vdot"]))
            e_pace = paces["E"]["slow_s_km"]
            dist = round(dur * 60 / e_pace, 1)
        slot = int(changes.get("slot") or 1)
        title = "加练 · 放松晚跑（二练）" if slot == 2 else f"加练 · {kind}"
        w = {
            "plan_id": plan["id"], "date": r["applies_date"], "slot": slot,
            "week_index": (d - date.fromisoformat(plan["start_date"])).days // 7,
            "phase": _phase_for_week(plan, (d - date.fromisoformat(plan["start_date"])).days // 7),
            "kind": kind, "title": title, "description": r["reason"],
            "distance_km": dist, "duration_min": dur,
            "pace_zone": changes.get("pace_zone") or ("E" if kind != "CROSS" else None),
            "pace_slow_s_km": None, "pace_fast_s_km": None, "target_hr_zone": None,
            "source": "ai", "adjustment_id": r["id"], "status": "planned",
            "completed_activity_id": None, "segments_json": None,
        }
        plan_repo.upsert_workout(w)
        return
    raise RuntimeError(f"未知动作 {action}")


# ---------------- 教练聊天 ----------------

def _persist_chat_items(ctx: dict, items: list[dict], guard_log: list[str],
                        model: str, prompt: dict, output: ChatOutput,
                        auto_apply: bool = False) -> tuple[list[int], int]:
    """聊天提出的调整逐条落库（pending，不写今日缓存）。

    auto_apply=True（用户强制要求改课）时逐条直接应用到课表并置 applied——
    失败的行保持 pending（可在聊天里再批准）。返回 (ids, 自动应用失败条数)。
    """
    plan = plan_repo.get_active_plan()
    input_json = {"system": prompt["system"], "user": prompt["user"]}
    output_json = output.model_dump(mode="json")
    ids = []
    failed = 0
    for it in items:
        r = adjustment_repo.create_adjustment({
            "plan_id": plan["id"], "workout_id": it.get("planned_workout_id"),
            "applies_date": it["date"], "action": it["action"],
            "changes_json": it.get("changes"), "reason": it["reason"],
            "ai_model": model, "ai_input_json": input_json,
            "ai_output_json": output_json, "guardrail_log_json": guard_log,
            "status": "pending",
        })
        ids.append(r["id"])
        if auto_apply:
            try:
                _apply_row(plan, r)
                adjustment_repo.set_applied(r["id"])
            except Exception as e:
                failed += 1
                log.warning("强制调整 #%s 自动应用失败，保留待批准: %s", r["id"], e)
    return ids, failed


def _apply_profile_updates(updates: dict) -> dict:
    """聊天提出的档案更新：仅允许已知键 + 取值范围钳制。返回实际应用的更新。"""
    from ..ai.contracts import PROFILE_UPDATE_KEYS, PROFILE_UPDATE_RANGES
    clean: dict = {}
    for k in PROFILE_UPDATE_KEYS:
        if k not in updates or updates[k] is None:
            continue
        v = updates[k]
        if k == "sex":  # 枚举校验：库内只存 male/female
            if v not in ("male", "female"):
                continue
            clean[k] = v
            continue
        rng = PROFILE_UPDATE_RANGES.get(k)
        if rng:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            lo, hi = rng
            if not (lo <= v <= hi):
                continue
            v = int(v) if k == "birth_year" else round(v, 1)
        else:
            v = str(v).strip()[:100]
            if not v:
                continue
        clean[k] = v
    if clean:
        profile_repo.upsert_profile(clean)
    return clean


def _chat_message_view(m: dict) -> dict:
    ids = jsonutil.loads(m.get("adjustment_ids_json")) or []
    rows = [_row_view(r) for i in ids if (r := adjustment_repo.get_adjustment(i))]
    return {
        "id": m["id"], "role": m["role"], "content": m["content"],
        "created_at": m.get("created_at"), "model": m.get("model"),
        # chat=普通对话；sync_analysis=同步后自动分析（前端展示标记不同）
        "kind": m.get("kind") or "chat",
        "adjustment_ids": ids,
        "adjustments": rows,
        # 强制要求的调整已自动生效（全部 applied）→ 前端不再显示批准按钮、文案改为已执行
        "auto_applied": bool(rows) and all(r["status"] == "applied" for r in rows),
        "profile_updates": jsonutil.loads(m.get("profile_updates_json")) or {},
    }


def get_chat_history(limit: int = 100) -> list[dict]:
    """聊天记录（时间正序）；只含未被「清空对话」隐藏的消息。"""
    return [_chat_message_view(m) for m in reversed(chat_repo.list_messages(limit, visible_only=True))]


def clear_chat_history() -> dict:
    """清空聊天显示并开启新对话（保留记忆）：消息软隐藏，AI 上下文仍读取全部消息。"""
    n = chat_repo.hide_all_messages()
    log.info("清空对话：隐藏 %d 条消息（AI 上下文记忆保留）", n)
    return {"hidden": n}


# ---- 聊天质量门控（第十四批） ----
# 真实用户反馈「每轮对话都有一点牛头不对马嘴」：模型常把「最近一次训练复盘」
# 当默认答案——用户问水平/成绩/改课，它却长篇分析今天那堂跑。对策：
# 1) 提示词「答题铁律」（prompt_builder CHAT_SYSTEM_PROMPT）按意图分类作答；
# 2) 这里做服务端意图判定，回复明显没接住问题时附针对性提示重试一次。
_ESTIMATE_WORDS = ("能跑多少", "预估", "估计", "水平", "成绩", "预测", "等效",
                   "现在跑", "跑多少", "评估", "vdot", "VDOT")
_PLAN_WORDS = ("调整", "改成", "换成", "挪", "移到", "推到", "提前到", "延后",
               "取消", "删除", "删掉", "去掉", "加练", "多跑", "不跑", "休息一天",
               "别排", "暂停", "这周不跑", "课表安排", "力量训练", "长距离", "放掉")
_QUESTION_WORDS = ("怎么", "如何", "什么", "为什么", "需要注意")
_KNOWLEDGE_WORDS = ("为什么", "怎么", "如何", "什么", "营养", "碳水", "蛋白质",
                    "伤病", "受伤", "酸痛", "恢复期", "恢复多久", "睡多久", "多久",
                    "标准", "研究", "最新", "备赛", "减量", "能量胶", "补给",
                    "拉伸", "跑姿", "热身", "比赛策略", "心率区", "阈值", "间歇",
                    "配速训练", "该怎么", "该不该", "能不能", "吃多少")


def _chat_intent(text: str) -> set[str]:
    """用户消息粗意图（供门控与联网检索决策；不追求精确，宁缺勿乱）。"""
    intent: set[str] = set()
    if any(w in text for w in _ESTIMATE_WORDS):
        intent.add("estimate")
    if any(w in text for w in _PLAN_WORDS):
        intent.add("plan")
    if any(w in text for w in _KNOWLEDGE_WORDS):
        intent.add("knowledge")
    return intent


def _chat_answer_nudge(text: str, output: ChatOutput) -> str | None:
    """回复没接住问题的针对性提示；返回 None 表示接住了。"""
    intent = _chat_intent(text)
    reply = output.reply or ""
    # 问水平/成绩：reply 必须给具体成绩/VDOT/明确说数据不足，否则是跑题复盘
    if "estimate" in intent:
        import re as _re
        has_number = bool(_re.search(r"\d{1,2}[:：]\d{2}", reply)) or "VDOT" in reply
        has_decline = any(w in reply for w in ("不足", "无法", "缺少", "样本"))
        if not (has_number or has_decline):
            return ("\n\n注意：训练者刚才问的是他现在的成绩/水平（如「5K 能跑多少、"
                    "各项成绩水平、预估 VDOT」）。请直接引用【当前水平预估】与"
                    "【近一年各距离最佳成绩】里的具体数字作答（先给结论，例如"
                    "「你现在 5K 大约 20:30，等效 VDOT 50」），依据一两句即可；"
                    "不要再写当天训练复盘，也不要给模糊话。若数据确实不足，明说"
                    "缺什么、跑几次后能补。")
    # 改课请求（非「怎么/如何」疑问句）：必须 user_requested=true 且有落地动作
    if ("plan" in intent and not any(w in text for w in _QUESTION_WORDS)
            and not (output.user_requested and output.adjustments)):
        return ("\n\n注意：训练者这条消息看起来是在要求调整课表/训练安排"
                "（改/挪/取消/增减课或休息等）。请按改课请求处理：user_requested=true"
                " 并给出具体 adjustments（含每条 reason）；做不到的部分用 reason 说明"
                "并给替代安排。若你判断他其实是在咨询而非要求改课，就直接回答咨询"
                "本身、user_requested=false。两种情况都不许写与问题无关的训练复盘。")
    return None


def _maybe_web_search(text: str) -> list[dict] | None:
    """知识类问题且用户开启联网检索（智谱 search_std ≈0.01 元/次）→ 检索。

    失败返回 None 并记日志（降级为纯知识回答，不阻断对话）。
    """
    from . import settings_service
    if not settings_service.get_ai_web_search():
        return None
    if settings_service.is_mock_mode():
        return None
    if settings_service.get_ai_provider() != "zhipu":
        return None
    intent = _chat_intent(text)
    if "knowledge" not in intent:
        return None
    try:
        from ..ai.web_search import zhipu_search
        results = zhipu_search(text, settings_service.get_ai_key("zhipu") or "")
        log.info("联网检索「%s」→ %d 条结果", text[:50], len(results))
        return results or None
    except Exception as e:
        log.warning("联网检索失败，降级为知识回答：%s", e)
        return None


def chat(message: str) -> dict:
    """教练聊天：主观意愿/健康数据 → 回复 + 可批准的调整 + 档案更新。"""
    from . import settings_service
    from ..services import plan_service
    message = (message or "").strip()
    if not message:
        raise RuntimeError("消息不能为空")
    today = dates.today()
    ctx = _gather(today, False, message)
    if ctx is None:
        raise RuntimeError("尚未创建训练计划，请先到“训练目标”页生成课表")
    ctx["ability"] = (plan_service.wizard_context() or {}).get("ability") or {}

    web = _maybe_web_search(message)
    prompt = prompt_builder.build_chat(ctx, chat_repo.list_messages(limit=50), web=web)
    if settings_service.is_mock_mode():
        user_row = chat_repo.create_message("user", message)
        coach_row = chat_repo.create_message(
            "coach", "（模拟模式）收到！真实模式下我会结合你的健康数据与课表来回答。")
        return {"user_message": user_row, "reply": _chat_message_view(coach_row)}

    client = _make_client(False)
    log.info("教练聊天调用开始（模型 %s，消息 %d 字%s）",
             getattr(client, "model", "?"), len(message),
             "，联网检索已开启" if web else "")
    t0 = time.monotonic()
    output = _validated_no_echo(client, prompt, ChatOutput)
    # 质量门控：没接住问题（答成跑题复盘）→ 针对性提示重试一次
    nudge = _chat_answer_nudge(message, output)
    if nudge:
        log.warning("聊天回复疑似跑题（意图门控），附提示重试一次")
        output = _validated_no_echo(
            client, {**prompt, "user": prompt["user"] + nudge}, ChatOutput)
    log.info("教练聊天调用返回，耗时 %.1fs", time.monotonic() - t0)

    # 调整建议走与日常建议相同的护栏；用户明确要求改课（user_requested）时
    # 进入强制模式：不丢弃，降强度/调课表落地（见 guardrails.force）
    fake = CoachOutput(summary="chat", readiness="ok", key_signals=[],
                       adjustments=output.adjustments, add_extra_advice=None, weekly_notes="")
    items, guard_log = guardrails.validate(
        fake, guardrails.GuardContext(**ctx["guard"], force=bool(output.user_requested)))
    model = getattr(client, "model", "mock")
    forced = bool(output.user_requested)
    ids, auto_failed = _persist_chat_items(
        ctx, items, guard_log, model, prompt, output, auto_apply=forced)

    profile_applied = _apply_profile_updates(output.profile_updates or {})
    rebuild_info = None
    if output.rebuild_plan:  # 档案更新或配速-心率趋势均可触发重估（refresh 自带 |Δ|≥0.2 门槛）
        try:
            refreshed = plan_service.refresh_active_plan()
            if refreshed:
                rebuild_info = {"vdot": refreshed["vdot"], "source": refreshed["vdot_source"]}
        except Exception as e:  # 重建失败不阻断聊天
            log.warning("聊天触发课表重建失败: %s", e)

    reply = output.reply
    if web:
        reply += "\n\n（部分内容基于联网检索的最新信息，来源见正文 [编号]。联网检索按次计费，设置里可关。）"
    dropped = len(output.adjustments) - len(items)
    if dropped:
        reply += f"\n\n⚠️ 其中 {dropped} 条调整未通过安全护栏被忽略。"
    if forced:
        applied_n = len(items) - auto_failed
        if applied_n > 0:
            reply += (f"\n\n✅ 已按你的要求直接改到课表（{applied_n} 项），去日历即可看到变化。"
                      + ("其余调整未执行成功，可点下方「批准」重试。" if auto_failed else ""))
        elif auto_failed:
            reply += "\n\n⚠️ 本次调整未能自动执行，请点下方「批准」手动应用到课表。"
    user_row = chat_repo.create_message("user", message)
    coach_row = chat_repo.create_message(
        "coach", reply, adjustment_ids=ids, profile_updates=profile_applied, model=model)
    return {"user_message": user_row, "reply": _chat_message_view(coach_row),
            "guardrail_log": guard_log, "profile_updates": profile_applied,
            "rebuild": rebuild_info}


def auto_analyze_new_activities(new_acts: list[tuple[str, int]],
                                client=None) -> dict | None:
    """同步后有新训练数据 → 自动生成分析总结 + 未来几天建议（教练消息）。

    new_acts: [(external_id, start_ts)]（sync_service 本轮 upsert 的新活动）。
    去重游标 last_analysis_act_ts 存 sync_state meta：已分析过的活动不再重复分析
    （同步多次不重复计费）。mock 模式不触发。返回 None 表示无需分析；
    AI 失败抛异常，由调用方降级（同步本身不受影响）。
    """
    from ..db.repos import sync_repo
    from . import settings_service
    if settings_service.is_mock_mode():
        return None
    state = sync_repo.get_sync_state("garmin")
    meta = jsonutil.loads(state["meta_json"]) if state["meta_json"] else {}
    last_ts = int(meta.get("last_analysis_act_ts") or 0)
    fresh = [(eid, ts) for eid, ts in new_acts if int(ts) > last_ts]
    if not fresh:
        return None
    today = dates.today()
    ctx = _gather(today, False, "")
    if ctx is None:
        return None
    # 一次同步（如首次回溯）可能进来几百条：只把最近 30 条喂给 AI，
    # 游标推进到全部新活动，历史条目不逐条分析
    fresh_sorted = sorted(fresh, key=lambda x: int(x[1]), reverse=True)
    to_analyze = {eid for eid, _ in fresh_sorted[:30]}
    new_rows = sorted(
        (a for a in activity_repo.list_activities(limit=500)
         if a.get("source") == "garmin" and a.get("external_id") in to_analyze),
        key=lambda a: a["start_ts"], reverse=True)
    if not new_rows:
        return None
    from ..services import plan_service
    ctx["ability"] = (plan_service.wizard_context() or {}).get("ability") or {}
    prompt = prompt_builder.build_sync_analysis(ctx, new_rows)
    client = client or _make_client(False)
    output = _validated_no_echo(client, prompt, ChatOutput)
    # 建议走与日常建议相同的护栏（非强制：训练者没有要求改课，违规项照常钳制/丢弃）
    fake = CoachOutput(summary="sync-analysis", readiness="ok", key_signals=[],
                       adjustments=output.adjustments, add_extra_advice=None, weekly_notes="")
    items, guard_log = guardrails.validate(
        fake, guardrails.GuardContext(**ctx["guard"], force=False))
    model = getattr(client, "model", "mock")
    ids, _ = _persist_chat_items(ctx, items, guard_log, model, prompt, output,
                                 auto_apply=False)
    coach_row = chat_repo.create_message(
        "coach", output.reply, adjustment_ids=ids, model=model, kind="sync_analysis")
    meta["last_analysis_act_ts"] = max(int(ts) for _, ts in fresh)
    sync_repo.set_sync_state("garmin", meta=meta)
    log.info("同步后自动分析完成：分析 %d 条新活动，%d 条调整建议，消息 #%s",
             len(new_rows), len(items), coach_row["id"])
    return {"message_id": coach_row["id"], "activities_analyzed": len(new_rows),
            "adjustment_count": len(items)}


def decide_chat_adjustments(message_id: int, approve: bool) -> dict:
    """决定某条教练消息提出的调整；批准则逐条应用到课表。"""
    m = chat_repo.get_message(message_id)
    if not m or m["role"] != "coach":
        raise RuntimeError("聊天消息不存在")
    plan = plan_repo.get_active_plan()
    if not plan:
        raise RuntimeError("没有活动计划")
    ids = jsonutil.loads(m.get("adjustment_ids_json")) or []
    applied = 0
    errors: list[str] = []
    for i in ids:
        r = adjustment_repo.get_adjustment(i)
        if not r or r["status"] != "pending":
            continue
        if approve:
            try:
                _apply_row(plan, r)
                adjustment_repo.set_applied(i)
                applied += 1
            except Exception as e:
                log.exception("应用聊天调整 %s 失败", i)
                errors.append(f"#{i} {r['action']}: {e}")
        else:
            adjustment_repo.decide_adjustment(i, "rejected")
    return {"applied": applied, "rejected": 0 if approve else len(ids),
            "errors": errors}
