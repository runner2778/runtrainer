"""仪表盘聚合服务：一次返回首页所需全部数据。

恢复度/比赛倒计时/本周负荷/KPI（ACWR 等）/趋势序列/今日训练/AI 教练/同步状态
全部在本地计算，前端不再逐个调 bridge（少请求、逻辑可测）。
"""
from __future__ import annotations

from datetime import date, timedelta

from ..db.repos import (activity_repo, chat_repo, goal_repo, health_repo, kv_repo,
                        plan_repo, profile_repo, sync_repo)
from ..domain import load_metrics
from ..utils import dates, jsonutil

CACHE_PREFIX = "coach:"  # 与 coach_service 共用今日建议缓存键

READINESS = {
    "good": {"label": "恢复良好", "cls": "good"},
    "ok": {"label": "恢复一般", "cls": "ok"},
    "low": {"label": "需要恢复", "cls": "low"},
    "unknown": {"label": "数据不足", "cls": "unknown"},
}
# 前端把语义状态映射到现有状态类（health 页同款）
READINESS_CLS = {"good": "st-good", "ok": "st-warning", "low": "st-critical", "unknown": ""}
READINESS_NOTES = {
    "good": "睡眠与恢复指标都不错，可以按计划执行今天的训练。",
    "ok": "部分恢复指标欠佳，今天的强度课可适当保守。",
    "low": "恢复指标偏弱，建议降强度或安排轻松跑/休息，睡够 7 小时。",
    "unknown": "还没有健康数据：先在设置页同步 Garmin，AI 教练才有恢复判断依据。",
}


def _status_order(st: str) -> int:
    return {"good": 3, "ok": 2, "low": 1, "unknown": 0}.get(st, 0)


def _readiness(h: dict, profile: dict, baseline_hr: float | None) -> dict:
    """今日恢复度合成：睡眠评分/HRV 状态/静息心率三指标，取最差项为合成状态。"""
    items: list[dict] = []
    # 睡眠（优先评分，无评分时按时长粗判）
    if h.get("sleep_score") is not None:
        s = h["sleep_score"]
        items.append({"key": "sleep", "label": "睡眠评分", "value": str(round(s)),
                      "status": "good" if s >= 80 else ("ok" if s >= 60 else "low")})
    elif h.get("sleep_duration_s"):
        dur_h = h["sleep_duration_s"] / 3600
        items.append({"key": "sleep", "label": "睡眠时长", "value": f"{dur_h:.1f}h",
                      "status": "good" if dur_h >= 7 else ("ok" if dur_h >= 6 else "low")})
    # HRV
    if h.get("hrv_avg_ms") is not None:
        st = {"balanced": "good", "unbalanced": "ok", "low": "low"}.get(h.get("hrv_status"), "ok")
        items.append({"key": "hrv", "label": "HRV", "value": f"{h['hrv_avg_ms']:.0f}ms",
                      "status": st})
    # 静息心率（对照档案值或近 7 天均值）
    if h.get("resting_hr") is not None:
        rhr = h["resting_hr"]
        base = profile.get("rest_hr") or baseline_hr
        if base:
            st = "good" if rhr <= base + 3 else ("ok" if rhr <= base + 8 else "low")
        else:
            st = "ok"
        items.append({"key": "resting_hr", "label": "静息心率", "value": f"{rhr:.0f} bpm",
                      "status": st})
    status = min(items, key=lambda i: _status_order(i["status"]))["status"] if items else "unknown"
    return {
        "date": h.get("date") or dates.today().isoformat(),
        "status": status,
        **{k: v for k, v in READINESS[status].items()},
        "note": READINESS_NOTES[status],
        "items": items,
    }


def _week_buckets(rows: list[dict], key: str, value_key: str,
                  start: date, weeks: int) -> dict[str, float]:
    """把按天记录聚合到周桶（周一为一周起点）。"""
    buckets = {w: 0.0 for w in range(weeks)}
    for r in rows:
        d = date.fromisoformat(r[key])
        off = (d - start).days // 7
        if 0 <= off < weeks:
            buckets[off] += float(r.get(value_key) or 0)
    return buckets


def _plan_rows(rows: list[dict], plan: dict | None) -> list[dict]:
    """计划侧聚合前按课表生命周期裁剪：只认 [start_date, race_date] 内的课。

    开课前（如 9/7 开课、今天 9/6）课表还没有任何课可以执行，计划指标
    应为空而不是把旧课表残留/预备课算进本周；比赛日后计划行不再参与。
    """
    if not plan:
        return []
    lo, hi = plan["start_date"], plan["race_date"]
    return [w for w in rows if lo <= w["date"] <= hi]


def _weekly_series(today: date, plan_id: int | None,
                   sessions: list[dict], plan: dict | None = None,
                   runs_day: dict[str, float] | None = None) -> list[dict]:
    """近 8 周计划跑量 vs 实际跑量（含本周）+ 每周覆盖比。

    coverage: 该周计划日跑量被实际跑步覆盖的比例（plan_coverage，∈[0,1]；
    计划跑量 0 → None）。计划行按课表生命周期裁剪，开课前/比赛后的周为 0。
    """
    weeks = 8
    ws = today - timedelta(days=today.weekday())
    start = ws - timedelta(days=7 * (weeks - 1))
    planned: dict[str, float] = {}
    ws_rows: list[dict] = []
    if plan_id is not None:
        ws_rows = _plan_rows(plan_repo.get_workouts(
            plan_id, start.isoformat(), (ws + timedelta(days=6)).isoformat()), plan)
        planned = _week_buckets(ws_rows, "date", "distance_km", start, weeks)
    done = _week_buckets(sessions, "date", "distance_km", start, weeks)
    out = []
    for i in range(weeks):
        d = start + timedelta(days=7 * i)
        coverage = None
        if ws_rows and runs_day:
            wk_rows = [w for w in ws_rows
                       if d.isoformat() <= w["date"] <= (d + timedelta(days=6)).isoformat()]
            if wk_rows:
                coverage = load_metrics.plan_coverage(wk_rows, runs_day)["ratio"]
        out.append({
            "week_start": d.isoformat(),
            "label": d.strftime("%m-%d"),
            "planned_km": round(planned.get(i, 0.0), 1),
            "done_km": round(done.get(i, 0.0), 1),
            "coverage": coverage,
            "current": d <= today <= d + timedelta(days=6),
        })
    return out


def _decode_activity(a: dict) -> dict:
    """list_activities 行 → ability 输入：structure_json 解出分段结构。"""
    a = dict(a)
    raw = a.get("structure_json")
    a["structure"] = jsonutil.loads(raw) if raw else []
    return a


def _ability_30d(profile: dict, today: date, plan_vdot=None) -> dict:
    """成绩水平预估（仪表盘卡）——与目标页/AI 教练走 plan_service 的
    ability_assessment() 统一入口：180 天同源同口径（含近一年 PB/保持度、
    课表完成度自动判定、比赛心率对照档案最大心率）。每次 get_dashboard /
    同步完成后端重算，天然随每一次同步即时刷新；不再有第二套 30 天口径
    造成仪表盘与向导/AI 数字不一致（曾出现 50.6 vs 55.0 的分裂）。
    """
    from .plan_service import ability_assessment
    a = ability_assessment(today)
    return {
        "window_days": a.get("window_days", 180),
        "plan_vdot": plan_vdot,  # 对照用：课表训练按目标页定的 VDOT 配速
        **{k: a.get(k) for k in ("vdot", "predictions", "zones", "evidence",
                                 "max_hr", "as_of")},
        "year_bests": a.get("year_bests") or [],
        "consistency": a.get("consistency"),
        "note": ("近 180 天跑步数据不足，无法综合预估；跑几次后会自动更新。"
                 if not a.get("vdot") else None),
    }


def get_dashboard() -> dict:
    """首页聚合数据。无计划时除 today/has_plan 外各块为空结构，前端只显引导。"""
    today = dates.today()
    plan = plan_repo.get_active_plan()
    if plan:
        # 恢复课配速带自愈（旧引擎误按 E 带落库 → 50–59% 恢复带）：
        # 读侧即对齐，日历/仪表盘每次加载即时生效，无需等课表重建
        from .plan_service import ensure_recovery_pace
        ensure_recovery_pace(plan)
    profile = profile_repo.get_profile() or {}
    ws = today - timedelta(days=today.weekday())
    out: dict = {"today": today.isoformat(), "has_plan": bool(plan)}

    # 恢复度：最近一天健康 + 静息心率对照基线（近 7 天均值，不含当天）
    health_rows = health_repo.get_health((today - timedelta(days=29)).isoformat(), today.isoformat())
    health_rows.sort(key=lambda r: r["date"])  # 时间正序（前端图表直接消费）
    recent_hrs = [h["resting_hr"] for h in health_rows[-8:-1] if h.get("resting_hr") is not None]
    baseline_hr = sum(recent_hrs) / len(recent_hrs) if recent_hrs else None
    out["readiness"] = _readiness(health_rows[-1] if health_rows else {}, profile, baseline_hr)
    out["health_trend"] = [{
        "date": h["date"],
        "hrv": round(h["hrv_avg_ms"], 1) if h.get("hrv_avg_ms") is not None else None,
        "hrv_status": h.get("hrv_status"),
        "resting_hr": h.get("resting_hr"),
        "sleep_score": h.get("sleep_score"),
    } for h in health_rows]

    # 训练数据（周负荷/KPI/系列共用）：近 10 周活动 + 课表。
    # 执行率/完成进度等计划侧指标只按「跑步」配对：跨训练不是执行课表。
    acts = activity_repo.list_activities(
        (today - timedelta(days=70)).isoformat(), limit=2000)
    sessions = [
        {"date": dates.ts_to_date(a["start_ts"]).isoformat(),
         "distance_km": (a.get("distance_m") or 0) / 1000,
         "duration_min": (a.get("duration_s") or 0) / 60,
         "sport": a.get("sport")}
        for a in acts if a.get("distance_m")
    ]
    run_sessions = [s for s in sessions if load_metrics.is_running(s.get("sport"))]
    runs_day = load_metrics.run_days_from_sessions(run_sessions)
    if plan:
        plan_id = plan["id"]
        # 本周负荷：计划课（周窗 ∩ 课表生命周期 [start_date, race_date]）
        # vs 实际本周跑步。% = 覆盖比（计划日跑量逐日封顶配对，∈[0,100]；
        # 计划外自由跑不计入执行，防「每天 12km 自由跑」把课表撑成几百 %）
        week_all = plan_repo.get_workouts(
            plan_id, ws.isoformat(), (ws + timedelta(days=6)).isoformat())
        week_wk = _plan_rows(week_all, plan)
        planned_km = sum(w.get("distance_km") or 0 for w in week_wk)
        planned_n = len(week_wk)
        done_km = sum(s["distance_km"] for s in run_sessions
                      if ws <= date.fromisoformat(s["date"]) <= ws + timedelta(days=6))
        done_n = sum(1 for s in run_sessions
                     if ws <= date.fromisoformat(s["date"]) <= ws + timedelta(days=6))
        auto_wk = load_metrics.workout_auto_done(week_wk, runs_day)
        done_plan = sum(1 for w in week_wk
                        if w["status"] == "completed" or w["id"] in auto_wk)
        cov_wk = load_metrics.plan_coverage(week_wk, runs_day)
        out["week_load"] = {
            "planned_km": round(planned_km, 1), "done_km": round(done_km, 1),
            "planned_n": planned_n, "done_n": done_n, "done_plan": done_plan,
            "pct": round(cov_wk["ratio"] * 100) if cov_wk["ratio"] is not None else None,
            "covered_km": cov_wk["covered_km"],
        }
        # 今日训练（含二练；auto_done：今天这节按当日跑步自动判定已执行）
        tws = sorted(_plan_rows(
            plan_repo.get_workouts(plan_id, today.isoformat(), today.isoformat()), plan),
            key=lambda w: w.get("slot") or 1)
        out["today_workouts"] = [{
            "id": w["id"], "slot": w.get("slot") or 1, "kind": w["kind"],
            "title": w["title"], "distance_km": w.get("distance_km"),
            "duration_min": w.get("duration_min"), "pace_zone": w.get("pace_zone"),
            "status": w["status"],
            "auto_done": w["id"] in auto_wk,
        } for w in tws]
        # 比赛倒计时
        goal = goal_repo.get_active_goal() or {}
        race = date.fromisoformat(plan["race_date"])
        total_days = max((race - date.fromisoformat(plan["start_date"])).days, 1)
        out["race"] = {
            "name": goal.get("name") or {5000: "5K", 10000: "10K",
                                        21097: "半马", 42195: "全马"}.get(goal.get("distance_m"), "目标比赛"),
            "race_date": plan["race_date"],
            "days_left": (race - today).days,
            "progress_pct": round(min(1.0, max(0.0, (today - date.fromisoformat(plan["start_date"])).days / total_days)) * 100),
            "current_week": int(plan["total_weeks"] and max(1, min(plan["total_weeks"], (today - date.fromisoformat(plan["start_date"])).days // 7 + 1))),
            "total_weeks": plan["total_weeks"],
            "vdot": plan["vdot"],
        }
        # KPI：执行覆盖（近 7 天课表日被实际跑步覆盖的比例）/ACWR/单调性/应变
        planned7 = _plan_rows(plan_repo.get_workouts(
            plan_id, (today - timedelta(days=6)).isoformat(), today.isoformat()), plan)
        acwr = load_metrics.acwr(sessions, today)
        acwr_ratio = acwr["ratio"] if acwr else None
        weekly = load_metrics.weekly_totals(sessions)
        out["kpis"] = {
            "compliance_7d": load_metrics.plan_coverage(planned7, runs_day),
            "acwr": acwr_ratio,
            "acwr_status": "ok" if acwr_ratio is not None and 0.8 <= acwr_ratio <= 1.3 else "warn",
            "monotony": load_metrics.monotony(sessions, today),
            "strain": load_metrics.strain(sessions, today),
            "last_week_km": round(weekly[-1]["km"], 1) if weekly else None,
        }
        out["weekly_series"] = _weekly_series(today, plan_id, sessions, plan, runs_day)
    else:
        out["week_load"] = out["race"] = out["kpis"] = None
        out["today_workouts"] = []
        out["weekly_series"] = _weekly_series(today, None, sessions)

    # AI 教练：今日建议摘要（不动 adjustments，详情去教练页）+ 最新聊天消息
    advice = None
    cached = kv_repo.get_app_state(f"{CACHE_PREFIX}{today.isoformat()}")
    if cached:
        data = jsonutil.loads(cached)
        advice = {"summary": data.get("summary"), "readiness": data.get("readiness"),
                  "key_signals": data.get("key_signals") or []}
    last_msg = chat_repo.list_messages(limit=1)
    out["coach"] = {
        "advice": advice,
        "last_chat": ({"role": last_msg[0]["role"], "kind": last_msg[0].get("kind") or "chat",
                       "content": (last_msg[0]["content"] or "")[:120]}
                      if last_msg else None),
    }

    # 同步状态
    st = sync_repo.get_sync_state("garmin")
    out["sync"] = {
        "last_sync_ts": st.get("last_sync_ts"),
        "error": st.get("last_error"),
        "last_stats": jsonutil.loads(st["meta_json"]).get("last_stats") if st.get("meta_json") else None,
    }

    # 成绩水平预估（近 30 天）：与目标页 180 天能力卡同源算法，窗口不同
    # 且每次同步自动重算（syncRefresh → get_dashboard）
    out["ability_30d"] = _ability_30d(profile, today,
                                      plan["vdot"] if plan else None)
    return out
