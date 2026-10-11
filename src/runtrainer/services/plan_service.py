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
from ..domain.plan_engine import MIN_WEEKS, PHASE_ORDER, PlanSpec, generate_plan
from ..utils import dates

log = logging.getLogger(__name__)


def _decode_activity(a: dict) -> dict:
    """list_activities 行 → ability 输入：structure_json 解出分段结构。"""
    from ..utils import jsonutil
    a = dict(a)
    raw = a.get("structure_json")
    a["structure"] = jsonutil.loads(raw) if raw else []
    return a


def _samples_reader(activities: list[dict]):
    """证据层样本读取器：同一轮评估内按活动 id 记忆化；库里没有样本点时返回 None。

    ability_assessment 会对同一批活动跑 2~3 次 compute_ability（先出估计、
    再让近一年 PB/完成度参与、最后按新学到的个人历史重算），证据层每轮都要
    为每个候选活动拉一次样本。不记忆化就是把同一批 activity_samples 反复
    materialize（真实库 68 万行、单次评估 280 次取样本），这是目标页
    get_goal_wizard_context 要 2.4 秒的主因。
    """
    if not any(a.get("has_samples") for a in activities):
        return None
    cache: dict[int, list] = {}

    def read(activity_id: int) -> list:
        aid = int(activity_id)
        if aid not in cache:
            cache[aid] = activity_repo.get_samples(aid)
        return cache[aid]

    return read


def ability_assessment(today: date | None = None, window_days: int | None = None,
                       weight_profile: dict | None = None) -> dict:
    """成绩水平预估统一入口（仪表盘 / 目标向导 / AI 教练同源同口径）。

    唯一实现：默认 180 天活动切片 + 手表 VO2max/档案最大心率/静息心率 +
    近一年各距离最佳成绩与训练保持度 + 课表质量课完成度（按当日跑量自动判定
    执行，不依赖手动勾选）。任何调用方重算一次，同步进数据后每次调用即自动
    刷新；全库不再存在第二套「30 天口径」造成仪表盘与向导/AI 数字打架（曾出现
    仪表盘 50.6 vs 向导 55.0）。返回含 est 各字段 + 上下文（plan_vdot/
    year_bests/consistency/acts_window…）供上层拼装。

    window_days / weight_profile 是给「一键评估」用的（最近三周、间歇能力占
    大头）：只换**参与估计的活动切片与分量权重**，取数、近一年 PB、比赛上限
    钳制、课表完成度判定全部沿用同一条链路——不再长出一套会跟主口径打架的
    估算实现。

    主口径另带个人误差学习（第十九批）：检测到新比赛时回放当时的预测、
    把误差记入个人历史（app_state），后续预估自动做分量逆误差加权与按距离
    偏差校准。偏差校准与预测区间两种口径都应用（实验验证：主口径历史偏差
    套到一键评估口径同样降误差，MAE 3.35→3.05 VDOT，8 场真实比赛）；
    分量逆误差加权只在主口径（实验 S7 在主口径验证，quick 权重档案不混用）。
    """
    from ..db.repos import health_repo
    from ..domain import ability as ab
    from ..domain import load_metrics
    today = today or dates.today()
    window = int(window_days) if window_days else 180
    quick = weight_profile is not None
    # 一次拉近一年：窗口内切片供水平预估分量，全年行供近一年最佳成绩/保持度
    acts_year = [_decode_activity(a) for a in
                 activity_repo.list_activities((today - timedelta(days=364)).isoformat(),
                                               limit=3000)]
    for a in acts_year:
        if a.get("start_ts"):
            a["date"] = dates.ts_to_date(a["start_ts"]).isoformat()
    acts_win = [a for a in acts_year
                if a.get("date", "") >= (today - timedelta(days=window)).isoformat()]
    prof = profile_repo.get_profile() or {}
    # 静息心率：档案优先；否则取近 30 天健康数据静息心率中位数（HRR 分量）
    rest_hr = prof.get("rest_hr")
    if not rest_hr:
        rhrs = [r["resting_hr"] for r in health_repo.get_health((today - timedelta(days=30)).isoformat())
                if r.get("resting_hr")]
        if rhrs:
            rest_hr = round(sorted(rhrs)[len(rhrs) // 2], 1)
    # 课表质量课完成度（课程内容 + 完成情况参与预估）：近 8 周计划内
    # T/I/R/TUNEUP 完成比例，按课表生命周期裁剪；执行按当日实际跑量自动
    # 判定（不手动勾完成也算跑过），首次并入——调整不依赖 PB/比赛证据在场
    plan_exec = None
    plan = plan_repo.get_active_plan()
    if plan:
        rows = plan_repo.get_workouts(
            plan["id"], (today - timedelta(days=ab.QUALITY_WINDOW_DAYS)).isoformat(),
            today.isoformat())
        rows = [w for w in rows
                if plan["start_date"] <= w["date"] <= plan["race_date"]]
        run_day: dict[str, float] = {}
        for a in acts_year:
            if (a.get("date") and a.get("distance_m")
                    and load_metrics.is_running(a.get("sport"))):
                run_day[a["date"]] = run_day.get(a["date"], 0.0) \
                    + a["distance_m"] / 1000.0
        plan_exec = ab.quality_execution(rows, today=today,
                                         auto_done=load_metrics.workout_auto_done(rows, run_day))
    personal_history = _personal_history_load()
    samples_fn = _samples_reader(acts_year)
    # 近一年各距离最佳成绩（比赛硬证据 + 长跑最快分段）+ 训练保持度：
    # 供「预估水平/今年状态」类提问与仪表盘引用，并参与水平预估（PB 加成）。
    # max_hr 只由窗口活动决定（compute_ability 内部就是 estimate_max_hr(
    # profile_max_hr, activities) 且按 round() 返回），所以先算出来即可让 PB
    # 加分并进首轮估计——原先「先无 PB 估一次、拿到 max_hr 再算 PB、再估
    # 一次」等于把同一批活动（真库 280 次取样本 + 240 次滑窗）跑两遍，
    # 首轮结果只是被丢掉当跳板。round() 必须复刻：distance_bests 的最快
    # 分段按 max_hr 比例卡心率，传 185.3 与传 185 会选出不同的分段。
    max_hr = ab.estimate_max_hr(prof.get("max_hr"), acts_win)
    year_bests = ab.distance_bests(acts_year, get_samples=samples_fn,
                                   max_hr=round(max_hr) if max_hr else None)
    consistency = ab.training_consistency(acts_year, today)
    est = ab.compute_ability(acts_win, prof.get("vo2max"), prof.get("max_hr"),
                             rest_hr=rest_hr, as_of=today,
                             year_bests=year_bests or None,
                             plan_exec=plan_exec, weight_profile=weight_profile,
                             effort_window_days=window if quick else None,
                             personal_history=personal_history or None,
                             samples_fn=samples_fn)
    # 个人误差学习（第十九批）：新比赛回放当时的预测、误差落库；有新记录
    # 立即用新历史重算一遍，仪表盘当场看到校准后的预测
    if not quick and est.get("vdot") is not None:
        est_max = ab.estimate_max_hr(prof.get("max_hr"), acts_year)
        history_new = _record_personal_races(personal_history, est_max,
                                             acts_year, today, plan)
        if history_new != personal_history:
            _personal_history_save(history_new)
            est = ab.compute_ability(acts_win, prof.get("vo2max"), prof.get("max_hr"),
                                     rest_hr=rest_hr, as_of=today,
                                     year_bests=year_bests or None,
                                     plan_exec=plan_exec,
                                     personal_history=history_new,
                                     samples_fn=samples_fn)
    return {**{k: est.get(k) for k in
               ("vdot", "predictions", "zones", "evidence", "max_hr", "as_of")},
            "prediction_intervals": est.get("prediction_intervals"),
            "personal_races": est.get("personal_races"),
            "window_days": window,
            "plan_vdot": plan.get("vdot") if plan else None,
            "plan": plan, "profile": prof,
            "year_bests": year_bests, "consistency": consistency,
            "acts_year": acts_year, "acts_window": acts_win, "rest_hr": rest_hr}


# 个人误差历史在 app_state 的键（第十九批）：存的是模型历史预测 vs 实际
# 比赛的误差记录（每场一条，含当时各分量 VDOT，约几百字节）。分量逆误差
# 加权/按距离偏差校准/预测区间全部从记录即时推导——不存衍生状态，脏了
# 也能无痛重建。
PERSONAL_HISTORY_KEY = "personal_history"


def _personal_history_load() -> list[dict]:
    from ..db.repos import kv_repo
    from ..utils import jsonutil
    raw = kv_repo.get_app_state(PERSONAL_HISTORY_KEY)
    if not raw:
        return []
    try:
        history = jsonutil.loads(raw) or []
    except Exception:   # noqa: BLE001 —— 脏数据不该炸预估链路
        log.warning("个人误差历史解析失败，按空历史处理", exc_info=True)
        return []
    # 批33 一次性迁移：给旧记录补比赛分级（次全力比赛不参与学习与锚定）
    from ..domain import personal as pers
    upgraded = pers.upgrade_entries(history)
    if upgraded != history:
        _personal_history_save(upgraded)
    return upgraded


def _personal_history_save(history: list[dict]) -> None:
    from ..db.repos import kv_repo
    from ..utils import jsonutil
    kv_repo.set_app_state(PERSONAL_HISTORY_KEY, jsonutil.dumps(history))


def _replay_race(history: list[dict], race: dict, acts_year: list[dict],
                 plan: dict | None) -> dict | None:
    """回放一场比赛当时的预测：只取比赛日之前的数据，走与正式预估同一条
    链路（含个人历史/近一年 PB/课表完成度）。返回个人历史条目；当时预测
    不出来（数据不足）就返回 None——不拿今天的数据回填过去。"""
    from ..db.repos import health_repo
    from ..domain import ability as ab
    from ..domain import load_metrics
    day = date.fromisoformat(race["day"])
    acts_pre = [a for a in acts_year if (a.get("date") or "") < race["day"]]
    acts_win = [a for a in acts_pre
                if (a.get("date") or "") >= (day - timedelta(days=180)).isoformat()]
    prof = profile_repo.get_profile() or {}
    rest_hr = prof.get("rest_hr")
    if not rest_hr:
        rhrs = [r["resting_hr"] for r in health_repo.get_health(
            (day - timedelta(days=30)).isoformat(),
            (day - timedelta(days=1)).isoformat()) if r.get("resting_hr")]
        if rhrs:
            rest_hr = round(sorted(rhrs)[len(rhrs) // 2], 1)
    plan_exec = None
    if plan:
        rows = plan_repo.get_workouts(
            plan["id"], (day - timedelta(days=ab.QUALITY_WINDOW_DAYS)).isoformat(),
            (day - timedelta(days=1)).isoformat())
        rows = [w for w in rows if plan["start_date"] <= w["date"] <= day.isoformat()]
        run_day: dict[str, float] = {}
        for a in acts_year:
            if (a.get("date") and a.get("distance_m")
                    and load_metrics.is_running(a.get("sport"))):
                run_day[a["date"]] = run_day.get(a["date"], 0.0) \
                    + a["distance_m"] / 1000.0
        plan_exec = ab.quality_execution(
            rows, today=day,
            auto_done=load_metrics.workout_auto_done(rows, run_day))
    max_hr = ab.estimate_max_hr(prof.get("max_hr"), acts_win)
    samples_fn = _samples_reader(acts_pre)
    year_bests = ab.distance_bests(acts_pre, get_samples=samples_fn,
                                   max_hr=max_hr)
    est = ab.compute_ability(acts_win, prof.get("vo2max"), prof.get("max_hr"),
                             rest_hr=rest_hr, as_of=day, year_bests=year_bests,
                             plan_exec=plan_exec, personal_history=history,
                             samples_fn=samples_fn)
    pred_s = (est.get("predictions") or {}).get(race["label"])
    if not pred_s:
        return None
    pred_vdot = round(vd.estimate_vdot(vd.DISTANCES[race["label"]], pred_s), 1)
    # 批33：记录比赛分级（次全力不参与学习）与当时的训练证据水平（原始
    # VDOT，个人训练→比赛转换差的学习样本）。被回放的这场比赛本身不在
    # 训练窗口内，单独分级。
    from ..domain import evidence as evm
    ev_list = est.get("ev_list") or []
    ev_deltas = evm.conversion_deltas(history)
    prior_races = [e["actual_vdot"] for e in history
                   if e.get("label") == race["label"]
                   and e.get("actual_vdot") is not None]
    quality = evm.grade_race(race, ev_list, ev_deltas, max_hr,
                             prior_races=prior_races)
    ev_feats = {k: f.get("eff") for k, f in (est.get("ev_fitness") or {}).items()
                if k in ("interval", "steady", "best_effort")
                and f.get("eff") is not None}
    return {"day": race["day"], "label": race["label"],
            "actual_vdot": race["actual_vdot"], "pred_vdot": pred_vdot,
            "quality": quality, "ev_feats": ev_feats,
            "feats": {c["kind"]: c["vdot"] for c in est.get("components") or []}}


def _record_personal_races(history: list[dict], est_max: float | None,
                           acts_year: list[dict], today: date,
                           plan: dict | None) -> list[dict]:
    """检测新比赛并把「当时预测 vs 实际」的误差记入个人历史。

    只收近 180 天的比赛（acts_year 只拉到近一年，更早的比赛回放窗口
    不完整，会记进假误差）；一次最多回放最近 10 场（防首次评估时全库
    历史比赛一次性回放拖慢首屏，剩下的下次评估继续补）。
    """
    from ..domain import ability as ab
    from ..domain import personal as pers
    races = ab.detect_races(acts_year, est_max)
    last_day = history[-1]["day"] if history else ""
    floor = (today - timedelta(days=180)).isoformat()
    new = [r for r in races
           if r["day"] and r["day"] > last_day and r["day"] >= floor]
    for r in new[-10:]:
        entry = _replay_race(history, r, acts_year, plan)
        if entry:
            history = pers.record(history, entry)
    return history


# 「一键评估」结果落 app_state 的键。存的是**可展示切片**而非整个
# ability_assessment 返回——后者含近一年活动列表（带 structure），存下来是
# 几十上百 KB 的垃圾。
QUICK_KEY = "quick_assessment"
QUICK_IV_VER = 2   # 预测区间口径版本（批33 去偏残差 + 证据密度）；旧缓存作废重估


def quick_assessment(today: date | None = None) -> dict:
    """一键评估：按最近三周训练重估成绩（间歇/速度能力占大头）并落库。

    与 ability_assessment 同一条链路，只收窄窗口、换权重档案（见
    ability.QUICK_WEIGHTS）。结果存 app_state，此后仪表盘卡片就显示这次评估，
    直到有新活动进库（含比赛）自动重估，或用户再点一次。
    """
    from ..db.repos import kv_repo
    from ..domain import ability as ab
    from ..utils import jsonutil
    today = today or dates.today()
    a = ability_assessment(today, window_days=ab.QUICK_WINDOW_DAYS,
                           weight_profile=ab.QUICK_WEIGHTS)
    payload = {
        "mode": "quick",
        "window_days": a.get("window_days"),
        "assessed_at": dates.now_iso(),
        # 评估时的活动总数：新活动进库（含比赛）→ 数字变了 → 自动重估。
        # 用条数而不是时间戳——一次同步可能补录历史活动，条数必然变。
        "acts_n": activity_repo.count_activities(),
        "iv_ver": QUICK_IV_VER,
        "plan_vdot": a.get("plan_vdot"),
        "year_bests": a.get("year_bests") or [],
        "consistency": a.get("consistency"),
        **{k: a.get(k) for k in ("vdot", "predictions", "zones", "evidence",
                                 "max_hr", "as_of", "prediction_intervals",
                                 "personal_races")},
    }
    kv_repo.set_app_state(QUICK_KEY, jsonutil.dumps(payload))
    return payload


def quick_assessment_cached(today: date | None = None) -> dict | None:
    """已落库的一键评估；有新活动进库时先重估再返回。

    没有存过就返回 None——仪表盘这时仍走 180 天主口径，用户没点过按钮时
    不该凭空多出一套「最近三周」的数字。
    """
    from ..db.repos import kv_repo
    from ..utils import jsonutil
    raw = kv_repo.get_app_state(QUICK_KEY)
    if not raw:
        return None
    try:
        stored = jsonutil.loads(raw) or {}
    except Exception:   # noqa: BLE001 —— 存量脏数据不该让仪表盘整页失败
        log.warning("一键评估缓存解析失败，丢弃并回退长期口径", exc_info=True)
        kv_repo.delete_app_state(QUICK_KEY)   # 清掉，免得每次开仪表盘都报一次
        return None
    if stored.get("iv_ver") != QUICK_IV_VER or stored.get("acts_n") != activity_repo.count_activities():
        return quick_assessment(today)   # 区间口径升级 / 新数据（含比赛）→ 重新评估
    return stored


def vdot_table_context(vdot: float | None = None) -> dict:
    """丹尼尔斯 VDOT 训练表：默认档位 + 参考档位 + 心率上下文。

    默认档优先「当前预估」（一键评估缓存 → 长期口径），其次课表 VDOT，最后
    给中性默认档；前端另有「用课表」按钮可一键切过去，所以这里把 plan_vdot
    一并返回，省得页面再发一次请求。没有缓存又没有显式指定时才走重口径
    （ability_assessment 要拉一年活动），打开页面不该每次付这个钱。
    """
    from ..domain import training_table as tt
    from ..domain.workout_analysis import estimate_max_hr
    prof = profile_repo.get_profile() or {}
    max_hr = prof.get("max_hr") or estimate_max_hr(prof.get("birth_year"))
    rest_hr = prof.get("rest_hr")
    plan = plan_repo.get_active_plan() or {}
    plan_vdot = float(plan["vdot"]) if plan.get("vdot") else None
    cached = quick_assessment_cached() or {}
    est_vdot = float(cached["vdot"]) if cached.get("vdot") else None
    est_label = (f"当前预估（近 {cached.get('window_days', 21)} 天一键评估）"
                 if est_vdot else None)
    if not est_vdot and not vdot:
        live = ability_assessment().get("vdot")
        if live:
            est_vdot, est_label = float(live), "当前水平预估"
    if vdot:
        v, src, label = float(vdot), "manual", f"手动指定 {round(float(vdot), 1)}"
    elif est_vdot:
        v, src = est_vdot, ("quick" if cached.get("vdot") else "ability")
        label = est_label
    elif plan_vdot:
        v, src, label = plan_vdot, "plan", "当前课表"
    else:
        v, src, label = tt.VDOT_DEFAULT, "default", "默认档"
    return {"vdot": round(v, 1), "vdot_source": src, "vdot_label": label,
            "estimate_vdot": round(est_vdot, 1) if est_vdot else None,
            "plan_vdot": plan_vdot, "rest_hr": rest_hr, "max_hr": max_hr}


def wizard_context() -> dict:
    """向导预填：当前水平预估（综合手表 VO2max/配速-心率趋势/间歇能力/
    近期比赛 + 近一年 PB/保持度）、近 4 周平均周跑量、训练时期建议。

    水平预估走 ability_assessment() 统一入口（与仪表盘/AI 教练同源同口径）；
    完全无依据时回退手表 vo2max 读数。
    """
    today = dates.today()
    a = ability_assessment(today)
    est_vdot = a.get("vdot")
    recent_vdot = est_vdot
    recent_vdot_source = "ability" if recent_vdot is not None else None
    recent_race = None
    for ev in a.get("evidence") or []:
        if ev.get("source") == "recent_race" and ev.get("race"):
            r = ev["race"]
            recent_race = {"distance_m": r["distance_m"],
                           "date": dates.ts_to_date(r["date"]).isoformat(),
                           "duration_s": r["duration_s"], "name": r["name"]}
    prof = a.get("profile") or {}
    if recent_vdot is None:
        vo2 = prof.get("vo2max")
        if vo2:
            recent_vdot = round(float(vo2), 1)
            recent_vdot_source = "garmin_vo2max"
    month = activity_repo.list_activities((today - timedelta(days=28)).isoformat(), limit=1000)
    avg_km_4w = round(sum((act.get("distance_m") or 0) for act in month) / 1000 / 4, 1)
    # 训练时期智能判断（近 8 周强度与跑量分布）
    from ..domain.phase_estimator import suggest_phase
    from ..domain.workout_analysis import estimate_max_hr
    max_hr = prof.get("max_hr") or estimate_max_hr(prof.get("birth_year"))
    phase_suggestion = suggest_phase(a.get("acts_window") or [], today, max_hr=max_hr,
                                     rest_hr=a.get("rest_hr"))
    return {"today": today.isoformat(), "recent_vdot": recent_vdot,
            "recent_vdot_source": recent_vdot_source, "recent_race": recent_race,
            "ability": {k: a.get(k) for k in
                        ("vdot", "predictions", "zones", "evidence", "max_hr", "as_of")}
            | {"year_bests": a.get("year_bests") or [],
               "consistency": a.get("consistency")},
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


def ensure_recovery_pace(plan: dict | None) -> int:
    """计划恢复课配速带自愈（读取侧幂等调用）。

    旧引擎把恢复跑/放松晚跑（kind=RECOVERY）按 E 带 59–74%VDOT 落库，
    配速明显偏快；六区体系下恢复课应在 50–59% 恢复带。按计划 VDOT 把这类
    课的 pace_zone/配速区间原地修正（不重建课表、不丢 AI 调整），已对齐
    时是空操作。日历/仪表盘每次加载即触发 → 既有计划无需等同步重建即时更新。
    """
    if not plan or not plan.get("vdot"):
        return 0
    try:
        rec = vd.pace_table(float(plan["vdot"]))["RECOVERY"]
    except (TypeError, ValueError):
        return 0
    return plan_repo.repair_recovery_paces(plan["id"], rec["slow_s_km"], rec["fast_s_km"])


def refresh_active_plan() -> dict | None:
    """真实同步/导入后把 active 课表的配速同步到最新水平（VDOT 动态变化入口）。

    只改配速，绝不重排课表。先前的实现在 |ΔVDOT| ≥ 0.2 时整表重建：归档旧
    计划、新建计划行、重跑 generate_plan。而 VDOT 几乎每次同步都会漂移过
    0.2（真实库 2026-10-03~10-11 重建 10 次，计划 id 从 57 走到 66），重建
    又只把过去日期的完成状态拷过去、其余一律重排——AI 调整、手动改动、
    plan id 全部作废，用户看到的就是「计划课表丢失」。课表的结构（比赛日/
    周数/周量/训练日/时期起点）本来就全部抄自当前计划，VDOT 只决定配速，
    所以同步配速在语义上等价而无破坏性。需要重排课表时由目标向导显式生成。

    返回 sync_plan_paces 的结果（changed=True 时）——调用方据此提示「课表配速
    已同步」；无变化/被锁定/无估计时返回 None（沿用各调用点已有的 if 判断）。
    """
    plan = plan_repo.get_active_plan()
    if not plan:
        return None
    ctx = wizard_context()
    new_vdot = ctx.get("recent_vdot")
    if new_vdot is None:
        return None
    try:
        res = sync_plan_paces(target_vdot=new_vdot)
    except Exception:  # noqa: BLE001
        log.exception("配速同步失败（不影响同步主流程）")
        return None
    return res if res.get("changed") else None


# ---------------------------------------------------------------- 配速同步
# 预估 VDOT → 课表配速。此前只有「ΔVDOT≥0.2 就整表重建」一条路：小漂移
# （实测真实库 0.5 VDOT ≈ 各档 2 s/km）永远落不到课表上，而每次重建又会把
# 已排好的课重新洗牌。这里补一条外科手术式的通路：只改配速列。
PACE_SYNC_MIN_DELTA = 0.2      # 与重建门限同值：小于它视为同一水平
PACE_SYNC_MIN_SEC = 1.0        # 配速列变化小于 1 秒不写库（显示精度以下=抖动）


def _pace_map(target_vdot: float) -> dict:
    """VDOT → {zone: (slow_s_km, fast_s_km)}（E/RECOVERY 是带，其余单值）。"""
    out: dict[str, tuple[float, float]] = {}
    for k, v in vd.pace_table(target_vdot).items():
        if k == "vdot":
            continue
        if isinstance(v, dict):
            out[k] = (v["slow_s_km"], v["fast_s_km"])
        else:
            out[k] = (v, v)
    return out


def pace_sync_status(plan: dict | None = None, target_vdot: float | None = None) -> dict:
    """课表配速与预估水平的一致度（只读，给界面显示）。

    返回 {plan_vdot, target_vdot, delta, stale_workouts, total_workouts,
    needs_sync, locked, samples}。locked=True 表示用户在目标页手锁过 VDOT，
    自动同步不应覆盖（要覆盖得显式 force）。
    """
    plan = plan or plan_repo.get_active_plan()
    if not plan:
        return {"available": False}
    goal = goal_repo.get_goal(plan["goal_id"]) or {}
    if target_vdot is None:
        cached = quick_assessment_cached() or {}
        target_vdot = cached.get("vdot")
        if not target_vdot:
            target_vdot = ability_assessment().get("vdot")
    plan_vdot = float(plan["vdot"]) if plan.get("vdot") else None
    locked = (goal.get("vdot_source") == "manual")
    if target_vdot is None or plan_vdot is None:
        return {"available": False, "plan_vdot": plan_vdot,
                "target_vdot": target_vdot, "locked": locked}
    pm = _pace_map(target_vdot)
    today = dates.today().isoformat()
    stale = 0
    total = 0
    stale_rows: list[dict] = []
    for w in plan_repo.get_workouts(plan["id"]):
        if w.get("status") in ("done", "skipped") or not w.get("pace_zone"):
            continue
        if (w.get("source") or "") == "ai":
            continue
        total += 1
        tgt = pm.get(w["pace_zone"])
        if not tgt:
            continue
        if (w.get("pace_slow_s_km") is None
                or abs(w["pace_slow_s_km"] - tgt[0]) >= PACE_SYNC_MIN_SEC):
            stale += 1
            stale_rows.append({"date": w["date"], "zone": w["pace_zone"],
                               "title": w.get("title"),
                               "old_slow": w.get("pace_slow_s_km"),
                               "new_slow": tgt[0]})
    samples = sorted(stale_rows, key=lambda s: s["date"])[:3]
    return {
        "available": True,
        "plan_vdot": round(plan_vdot, 1),
        "target_vdot": round(float(target_vdot), 1),
        "delta": round(float(target_vdot) - plan_vdot, 2),
        "needs_sync": stale > 0,
        "stale_workouts": stale,
        "total_workouts": total,
        "locked": locked,
        "samples": samples,
        "as_of": today,
    }


def sync_plan_paces(target_vdot: float | None = None, force: bool = False,
                    include_past: bool = False) -> dict:
    """把预估 VDOT 对应的配速写进课表（外科手术式，不重建计划）。

    做三件事：训练计划表的 vdot、目标表的 vdot/vdot_source、以及每节
    「引擎生成且未执行」课的配速列。距离/时长/分段/日期/完成状态一律不动——
    重排课表只在目标向导里由用户显式生成（refresh_active_plan 现在也走这条路）。

    target_vdot 缺省取「一键评估缓存 → 长期口径预估」。目标页手锁过 VDOT
    （vdot_source='manual'）时默认不动，除非 force=True。
    返回 {changed, reason, plan_vdot, target_vdot, repaced, samples}。
    """
    plan = plan_repo.get_active_plan()
    if not plan:
        return {"changed": False, "reason": "no_plan"}
    goal = goal_repo.get_goal(plan["goal_id"])
    if not goal:
        return {"changed": False, "reason": "no_goal"}
    if target_vdot is None:
        cached = quick_assessment_cached() or {}
        target_vdot = cached.get("vdot")
        if not target_vdot:
            target_vdot = ability_assessment().get("vdot")
    if not target_vdot:
        return {"changed": False, "reason": "no_estimate"}
    target_vdot = round(float(target_vdot), 2)
    old_vdot = float(plan["vdot"]) if plan.get("vdot") else None
    if goal.get("vdot_source") == "manual" and not force:
        return {"changed": False, "reason": "locked", "plan_vdot": old_vdot,
                "target_vdot": target_vdot}
    if old_vdot is not None and abs(target_vdot - old_vdot) < PACE_SYNC_MIN_DELTA \
            and not force:
        return {"changed": False, "reason": "in_sync", "plan_vdot": round(old_vdot, 1),
                "target_vdot": target_vdot}

    pm = _pace_map(target_vdot)
    since = None if include_past else dates.today().isoformat()
    changed_rows = plan_repo.repace_workouts(plan["id"], pm, since_date=since)

    from ..db import database
    with database.get_conn() as conn:
        conn.execute("UPDATE training_plans SET vdot = ? WHERE id = ?",
                     (target_vdot, plan["id"]))
    goal_repo.update_goal(goal["id"], {"vdot": target_vdot,
                                       "vdot_source": "ability"})
    log.info("课表配速同步：VDOT %s → %s，改动 %s 节课",
             old_vdot, target_vdot, len(changed_rows))
    return {
        "changed": True,
        "reason": "synced",
        "plan_vdot": round(old_vdot, 1) if old_vdot is not None else None,
        "target_vdot": target_vdot,
        "replaced_vdot": True,
        "repaced": len(changed_rows),
        "samples": sorted(changed_rows, key=lambda r: r["date"])[:5],
        "pace_table": vd.pace_table(target_vdot),
    }
