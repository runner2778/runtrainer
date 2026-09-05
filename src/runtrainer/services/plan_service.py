"""目标与计划服务：向导上下文、VDOT 选取、课表生成与落库。

VDOT 选取优先级：手动 > 目标成绩反推 > 近期 3 个月比赛成绩；
目标反推比近期水平高 2 以上时给警告。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from ..config import ENGINE_VERSION
from ..db.repos import activity_repo, goal_repo, plan_repo, profile_repo
from ..domain import vdot as vd
from ..domain.plan_engine import MIN_WEEKS, PHASE_ORDER, PlanSpec, generate_plan, pro_extra_km
from ..utils import dates

log = logging.getLogger(__name__)


def _decode_activity(a: dict) -> dict:
    """list_activities 行 → ability 输入：structure_json 解出分段结构。"""
    from ..utils import jsonutil
    a = dict(a)
    raw = a.get("structure_json")
    a["structure"] = jsonutil.loads(raw) if raw else []
    return a


def wizard_context() -> dict:
    """向导预填：当前水平预估（综合手表 VO2max/配速-心率趋势/间歇能力/
    近期比赛）、近 4 周平均周跑量。

    水平预估由 domain.ability 综合计算（不再只看最近最快配速）；
    完全无依据时回退手表 vo2max 读数。
    """
    from ..db.repos import health_repo
    from ..domain import ability as ab
    from ..utils import jsonutil
    today = dates.today()
    acts180 = [_decode_activity(a) for a in
               activity_repo.list_activities((today - timedelta(days=180)).isoformat(), limit=2000)]
    for a in acts180:
        if a.get("start_ts"):
            a["date"] = dates.ts_to_date(a["start_ts"]).isoformat()
    prof = profile_repo.get_profile() or {}
    # 静息心率：档案优先；否则取近 30 天健康数据静息心率中位数（HRR 分量）
    rest_hr = prof.get("rest_hr")
    if not rest_hr:
        rhrs = [r["resting_hr"] for r in health_repo.get_health((today - timedelta(days=30)).isoformat())
                if r.get("resting_hr")]
        if rhrs:
            rest_hr = round(sorted(rhrs)[len(rhrs) // 2], 1)
    est = ab.compute_ability(acts180, prof.get("vo2max"), prof.get("max_hr"),
                             rest_hr=rest_hr)

    recent_vdot = est.get("vdot")
    recent_vdot_source = "ability" if recent_vdot is not None else None
    recent_race = None
    for ev in est.get("evidence") or []:
        if ev.get("source") == "recent_race" and ev.get("race"):
            r = ev["race"]
            recent_race = {"distance_m": r["distance_m"],
                           "date": dates.ts_to_date(r["date"]).isoformat(),
                           "duration_s": r["duration_s"], "name": r["name"]}
    if recent_vdot is None:
        vo2 = prof.get("vo2max")
        if vo2:
            recent_vdot = round(float(vo2), 1)
            recent_vdot_source = "garmin_vo2max"
    month = activity_repo.list_activities((today - timedelta(days=28)).isoformat(), limit=1000)
    avg_km_4w = round(sum((a.get("distance_m") or 0) for a in month) / 1000 / 4, 1)
    # 训练时期智能判断（近 8 周强度与跑量分布）
    from ..domain.phase_estimator import suggest_phase
    from ..domain.workout_analysis import estimate_max_hr
    max_hr = prof.get("max_hr") or estimate_max_hr(prof.get("birth_year"))
    phase_suggestion = suggest_phase(acts180, today, max_hr=max_hr,
                                     rest_hr=prof.get("rest_hr"))
    return {"today": today.isoformat(), "recent_vdot": recent_vdot,
            "recent_vdot_source": recent_vdot_source, "recent_race": recent_race,
            "ability": {k: est.get(k) for k in
                        ("vdot", "predictions", "evidence", "max_hr", "as_of")},
            "avg_weekly_km_4w": avg_km_4w, "min_weeks": MIN_WEEKS,
            "phase_suggestion": phase_suggestion}


def _resolve_vdot(distance_m: int, target_seconds, manual_vdot,
                  recent_vdot, recent_vdot_source: str | None = None) -> tuple[float, str, list[str]]:
    """VDOT 选取：手动 > 近期水平（比赛/手表 vo2max）> 目标反推 > 默认 40。

    课表按当前水平配速；目标成绩反推高出当前水平 2 以上时警告，随数据更新动态调整。
    """
    warnings: list[str] = []
    if manual_vdot:
        return float(manual_vdot), "manual", warnings
    if recent_vdot:
        src = recent_vdot_source or "result"
        if target_seconds:
            t = round(vd.estimate_vdot(distance_m, float(target_seconds)), 1)
            if t > recent_vdot + 2:
                warnings.append(
                    f"目标成绩对应 VDOT {t}，比当前水平 {recent_vdot} 高 2 以上；"
                    f"课表先按当前水平配速，随数据更新动态调整")
        return recent_vdot, src, warnings
    if target_seconds:
        return round(vd.estimate_vdot(distance_m, float(target_seconds)), 1), "target", warnings
    warnings.append("无近期比赛成绩与目标成绩，按 VDOT 40 生成计划；可在预览中手动指定 VDOT")
    return 40.0, "default", warnings


def _build_spec(params: dict) -> tuple[PlanSpec, list[str], str]:
    goal = params.get("goal") or {}
    plan = params.get("plan") or {}
    distance_m = int(goal["distance_m"])
    race_date = date.fromisoformat(goal["race_date"])
    ctx = wizard_context()
    vdot_val, source, warns = _resolve_vdot(
        distance_m, goal.get("target_seconds"), goal.get("vdot"),
        ctx.get("recent_vdot"), ctx.get("recent_vdot_source"))
    start_date = date.fromisoformat(plan["start_date"]) if plan.get("start_date") else dates.today()
    base_km = float(plan.get("base_weekly_km") or ctx.get("avg_weekly_km_4w") or 30)
    # 训练时期：None=完整周期；"auto"=按近期强度/跑量分布智能判断；或明确时期名
    start_phase = plan.get("start_phase")
    if start_phase == "auto":
        from ..domain.phase_estimator import suggest_phase
        from ..domain.workout_analysis import estimate_max_hr
        prof = profile_repo.get_profile() or {}
        today = dates.today()
        acts = [_decode_activity(a) for a in
                activity_repo.list_activities((today - timedelta(days=56)).isoformat(), limit=1000)]
        for a in acts:
            if a.get("start_ts"):
                a["date"] = dates.ts_to_date(a["start_ts"]).isoformat()
        sug = suggest_phase(acts, today, race_date=race_date,
                            max_hr=prof.get("max_hr") or estimate_max_hr(prof.get("birth_year")),
                            rest_hr=prof.get("rest_hr"))
        start_phase = sug["phase"]
        warns.append(f"训练时期按近期数据智能判断：从{sug['phase_name']}开始"
                     f"（{'；'.join(sug['reasons'])}）")
    elif start_phase is not None and start_phase not in PHASE_ORDER:
        warns.append(f"未知训练时期 {start_phase}，已回退为完整周期")
        start_phase = None
    spec = PlanSpec(
        goal_distance_m=distance_m, race_date=race_date, vdot=vdot_val,
        base_weekly_km=base_km, start_date=start_date,
        weeks=plan.get("weeks"),
        run_days=int(plan.get("run_days") or 5),
        long_run_weekday=int(plan.get("long_run_weekday") or 6),
        goal_name=goal.get("name"),
        target_seconds=goal.get("target_seconds"),
        start_phase=start_phase,
        double_days=int(plan.get("double_days") or 0),
        double_mode=str(plan.get("double_mode") or "auto"),
        strength_days=int(plan.get("strength_days") or 0),
        pro_mode=bool(plan.get("pro_mode")),
    )
    return spec, warns, source


def _payload(res, source: str, extra_warnings: list[str]) -> dict:
    return {
        "start_date": res.start_date.isoformat(), "race_date": res.race_date.isoformat(),
        "total_weeks": res.total_weeks, "phase_weeks": res.phase_weeks,
        "start_phase": res.start_phase,
        "vdot": res.vdot, "vdot_source": source,
        "base_weekly_km": res.base_weekly_km, "peak_weekly_km": res.peak_weekly_km,
        "weekly_km": res.weekly_km,
        "pace_table": vd.pace_table(res.vdot),
        "equivalent_times": vd.equivalent_times(res.vdot),
        "warnings": extra_warnings + res.warnings,
        "workouts": [w.to_row() for w in res.workouts],
    }


def preview_plan(params: dict) -> dict:
    """只生成不落库，供向导第三步预览。"""
    spec, warns, source = _build_spec(params)
    return _payload(generate_plan(spec), source, warns)


def _persist_plan(spec: PlanSpec, res, source: str, goal: dict | None) -> tuple[dict, dict]:
    """落库：新建（或复用）goal + 新 plan + 课表；旧 active 计划归档为 superseded。"""
    if goal is None:
        goal = goal_repo.create_goal({
            "distance_m": spec.goal_distance_m, "race_date": spec.race_date.isoformat(),
            "target_seconds": spec.target_seconds, "vdot": res.vdot, "vdot_source": source,
        })
    else:
        goal_repo.update_goal(goal["id"], {"vdot": res.vdot, "vdot_source": source})
    for old in plan_repo.list_plans(limit=100):
        if old["status"] == "active":
            plan_repo.set_plan_status(old["id"], "superseded")
    plan = plan_repo.create_plan({
        "goal_id": goal["id"], "start_date": res.start_date.isoformat(),
        "race_date": res.race_date.isoformat(), "total_weeks": res.total_weeks,
        "phase_weeks": res.phase_weeks, "vdot": res.vdot,
        "base_weekly_km": res.base_weekly_km, "peak_weekly_km": res.peak_weekly_km,
        "run_days": spec.run_days, "long_run_weekday": spec.long_run_weekday,
        "engine_version": ENGINE_VERSION, "start_phase": spec.start_phase,
        "double_days": spec.double_days, "double_mode": spec.double_mode,
        "strength_days": spec.strength_days, "pro_mode": int(spec.pro_mode),
    }, [w.to_row() for w in res.workouts])
    return goal, plan


def create_goal_and_plan(params: dict) -> dict:
    """生成课表并落库：目标 + 计划 + 课表；旧 active 计划归档为 superseded。"""
    spec, warns, source = _build_spec(params)
    res = generate_plan(spec)
    goal, plan = _persist_plan(spec, res, source, None)
    payload = _payload(res, source, warns)
    payload["goal_id"] = goal["id"]
    payload["plan_id"] = plan["id"]
    return payload


def refresh_active_plan() -> dict | None:
    """真实同步后按最新水平重建 active 课表（VDOT 动态变化入口）。

    触发条件：新的水平依据（比赛/vo2max）与现计划差 ≥0.5，或现计划原本按默认值生成。
    沿用原目标与计划参数（比赛日/周数/跑量），保留过去日期的完成状态与关联活动。
    返回新计划 payload；无需重建时返回 None。
    """
    plan = plan_repo.get_active_plan()
    if not plan:
        return None
    ctx = wizard_context()
    new_vdot = ctx.get("recent_vdot")
    if new_vdot is None:
        return None
    old_vdot = float(plan["vdot"] or 0)
    if plan.get("vdot_source") != "default" and abs(new_vdot - old_vdot) < 0.5:
        return None
    goal = goal_repo.get_goal(plan["goal_id"])
    if not goal:
        return None
    # 职业模式存储的 base_weekly_km 含展示上浮量，重建须回退到原始基础量
    # （按旧 VDOT 反算，避免与新 VDOT 的增量叠加造成二次膨胀）
    raw_base = float(plan["base_weekly_km"] or 0)
    if plan.get("pro_mode"):
        raw_base = max(1.0, raw_base - pro_extra_km(old_vdot))
    params = {
        "goal": {
            "distance_m": goal["distance_m"], "race_date": goal["race_date"],
            "target_seconds": goal.get("target_seconds"), "vdot": None,  # 强制自动重选
            "name": goal.get("name"),
        },
        "plan": {
            "start_date": plan["start_date"], "weeks": plan["total_weeks"],
            "base_weekly_km": raw_base, "run_days": plan["run_days"],
            "long_run_weekday": plan["long_run_weekday"],
            # 保留原计划的时期起点（重建不改变用户的选择；旧计划无此列 → 完整周期）
            "start_phase": plan.get("start_phase"),
            # 一天两练/力量课/职业模式选项沿用原计划
            "double_days": plan.get("double_days") or 0,
            "double_mode": plan.get("double_mode") or "auto",
            "strength_days": plan.get("strength_days") or 0,
            "pro_mode": plan.get("pro_mode") or 0,
        },
    }
    spec, warns, source = _build_spec(params)
    res = generate_plan(spec)
    _, new_plan = _persist_plan(spec, res, source, goal)
    # 保留过去日期的完成状态与关联活动（重建不吞掉已完成的训练记录；
    # 一天两练按 (date, slot) 匹配）
    old_ws = {(w["date"], w.get("slot") or 1): w for w in plan_repo.get_workouts(plan["id"])}
    today = dates.today().isoformat()
    for w in plan_repo.get_workouts(new_plan["id"]):
        ow = old_ws.get((w["date"], w.get("slot") or 1))
        if ow and ow["date"] < today and (ow["status"] != "planned" or ow.get("completed_activity_id")):
            plan_repo.set_workout_status(w["id"], ow["status"], ow.get("completed_activity_id"))
    log.info("课表已按最新水平重建: VDOT %s → %s (source=%s)", old_vdot, res.vdot, source)
    payload = _payload(res, source, warns)
    payload["goal_id"] = goal["id"]
    payload["plan_id"] = new_plan["id"]
    payload["rebuilt"] = True
    return payload
