"""批33 成绩预测回测 v2（只读）：证据管线 vs 旧生产链路，严格时序对比。

与 backtest_predict.py 同一数据与时间纪律：预测某场比赛只能用该场之前的
数据（活动/健康/近一年最佳/个人历史全部按 as_of 裁剪，历史只含此前比赛）。
Before = 批33 之前的真实生产口径：旧加权链路 + 分量逆误差加权 + 按距离
偏差校准 + 旧预测区间（该距离原始误差 RMSE 向 4.0 VDOT 先验收缩 ×1.28，
personal.py 模块注释所载旧口径——用户看到的「10K ±3~4 分钟」来源）。
通过在运行时把 compute_ability 的证据管线分发条件短路得到（生产源码不动，
旧链路主体就是批33 之前的生产代码）。
After = 批33 证据管线（同一历史输入 + 训练证据/个人转换/比赛分级）。
两组各维护自己的个人历史（与各自生产口径的落库格式一致）。

输出：误差统计（MAE/中位/偏差/RMSE/MAPE）按距离与场景（该距离首场比赛/
比赛稀少/训练证据丰富与否）、Before/After 预测区间覆盖率与宽度、逐场明细。
"""
import json
import sys
import statistics
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo, health_repo, plan_repo, profile_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402
from runtrainer.domain import load_metrics, personal, vdot as vd  # noqa: E402
from runtrainer.utils import dates, jsonutil  # noqa: E402

database.migrate()

# ---- 旧生产口径（批33 之前）：把证据管线分发短路，恢复旧加权链路 ----
# 批33 只在 compute_ability 入口加了一行分发（personal_history 在场 → 证据
# 管线）；旧链路主体（分量加权/逆误差加权/偏差校准）就是批33 之前的生产
# 代码。此处运行时把分发条件换成恒假，得到「有个人历史也走旧加权」的
# 批33 之前口径，生产源码零改动。
import inspect  # noqa: E402

_legacy_ns = dict(ab.__dict__)
_legacy_src = inspect.getsource(ab.compute_ability).replace(
    "if personal_history is not None and not weight_profile:",
    "if False and personal_history is not None and not weight_profile:")
exec(compile(_legacy_src, "<legacy_compute>", "exec"), _legacy_ns)
legacy_compute = _legacy_ns["compute_ability"]

# 旧预测区间：该距离原始误差 RMSE 向 4.0 VDOT 先验收缩（personal.py 模块
# 注释所载旧口径；批33 重构前的区间公式），半宽 = 1.28 × 收缩 RMSE
OLD_INTERVAL_PRIOR = 4.0
OLD_INTERVAL_SHRINK_K = 4.0


def old_interval(history: list[dict], label: str) -> dict | None:
    s = personal.distance_stats(history, label)
    if not s["n"] or s["rmse"] is None:
        return None
    rmse = ((s["rmse"] ** 2 * s["n"]
             + OLD_INTERVAL_PRIOR ** 2 * OLD_INTERVAL_SHRINK_K)
            / (s["n"] + OLD_INTERVAL_SHRINK_K)) ** 0.5
    return {"n": s["n"], "half_vdot": round(personal.INTERVAL_Z * rmse, 2)}

DIST_LABELS = {5000: "5K", 10000: "10K", 21097: "半马", 42195: "全马"}


def decode(a: dict) -> dict:
    a = dict(a)
    raw = a.get("structure_json")
    a["structure"] = jsonutil.loads(raw) if raw else []
    return a


def race_band(a: dict, est_max: float | None) -> tuple[bool, int | None, bool]:
    dist, dur = a.get("distance_m"), a.get("duration_s")
    if not dist or not dur or dist < 3000 or dist > 43000:
        return False, None, False
    struct = a.get("structure") or []
    if (sum(1 for s in struct if s.get("type") == "work") >= 2
            and any(s.get("type") == "rest" for s in struct)):
        return False, None, False
    avg_hr, own_max = a.get("avg_hr"), a.get("max_hr")
    name = (a.get("name") or "").lower()
    if not any(h in name for h in ab.RACE_NAME_HINTS) and not (
            avg_hr and (est_max or own_max)
            and (avg_hr / (est_max or own_max) >= ab.RACE_HR_MIN_REF
                 or (own_max and avg_hr / own_max >= ab.YEAR_WHOLE_OWN_MIN))):
        return False, None, False
    for std, tol in ab.RACE_BANDS:
        if abs(dist - std) / std <= tol:
            genuine = any(h in name for h in ab.RACE_NAME_HINTS) or (
                avg_hr and est_max and avg_hr / est_max >= 0.75
                and own_max and avg_hr / own_max >= ab.YEAR_WHOLE_OWN_MIN)
            return True, std, genuine
    return False, None, False


def fmt_t(s):
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


prof = profile_repo.get_profile() or {}
all_rows = activity_repo.list_activities(None, None, limit=20000)
decoded_all = [decode(a) for a in all_rows]
est_max = ab.estimate_max_hr(prof.get("max_hr"), decoded_all)

races = []
for a in sorted(decoded_all, key=lambda x: x["start_ts"]):
    ok, std, genuine = race_band(a, est_max)
    if ok:
        races.append((a, std, genuine))
print("== 检测到的比赛 ==", len(races), "场 | 真实比赛",
      sum(1 for *_, g in races if g), "场")

plan = plan_repo.get_active_plan()


def run_prediction(a: dict, std: int, personal_history, samples=True,
                   legacy=False) -> dict:
    """严格时序预测。legacy=True → 批33 之前生产口径（旧加权 + 个人学习，
    分发已被短路）；legacy=False → 批33 证据管线。samples=False 时不给样本
    读取函数（旧链路本来不用；保留开关以便对比同输入）。"""
    race_ts = a["start_ts"]
    race_day = dates.ts_to_date(race_ts)
    as_of = race_day
    pre = [x for x in decoded_all if x["start_ts"] < race_ts]
    acts_year = [x for x in pre
                 if dates.ts_to_date(x["start_ts"]) >= race_day - timedelta(days=364)]
    for x in acts_year:
        x["date"] = dates.ts_to_date(x["start_ts"]).isoformat()
    acts_win = [x for x in acts_year
                if x["date"] >= (race_day - timedelta(days=180)).isoformat()]
    rest_hr = prof.get("rest_hr")
    if not rest_hr:
        rhrs = [r["resting_hr"] for r in health_repo.get_health(
            (race_day - timedelta(days=30)).isoformat(), (race_day - timedelta(days=1)).isoformat())
            if r.get("resting_hr")]
        if rhrs:
            rest_hr = round(sorted(rhrs)[len(rhrs) // 2], 1)
    plan_exec = None
    if plan and plan["start_date"] <= race_day.isoformat():
        rows = [w for w in plan_repo.get_workouts(
            plan["id"], (race_day - timedelta(days=ab.QUALITY_WINDOW_DAYS)).isoformat(),
            (race_day - timedelta(days=1)).isoformat())
            if plan["start_date"] <= w["date"] <= race_day.isoformat()]
        run_day: dict[str, float] = {}
        for x in acts_year:
            if x.get("date") and x.get("distance_m") and load_metrics.is_running(x.get("sport")):
                run_day[x["date"]] = run_day.get(x["date"], 0.0) + x["distance_m"] / 1000.0
        plan_exec = ab.quality_execution(rows, today=race_day,
                                         auto_done=load_metrics.workout_auto_done(rows, run_day))
    max_hr = ab.estimate_max_hr(prof.get("max_hr"), acts_win)
    year_bests = ab.distance_bests(
        acts_year,
        get_samples=(lambda aid: activity_repo.get_samples(aid))
        if any(x.get("has_samples") for x in acts_year) else None,
        max_hr=max_hr)
    samples_fn = (lambda aid: activity_repo.get_samples(aid)) \
        if samples and any(x.get("has_samples") for x in acts_win) else None
    if legacy:
        est = legacy_compute(acts_win, prof.get("vo2max"), prof.get("max_hr"),
                             rest_hr=rest_hr, as_of=as_of, year_bests=year_bests,
                             plan_exec=plan_exec, personal_history=personal_history,
                             samples_fn=samples_fn)
    else:
        est = ab.compute_ability(acts_win, prof.get("vo2max"), prof.get("max_hr"),
                                 rest_hr=rest_hr, as_of=as_of, year_bests=year_bests,
                                 plan_exec=plan_exec, personal_history=personal_history,
                                 samples_fn=samples_fn)
    label = DIST_LABELS[std]
    pred_s = (est.get("predictions") or {}).get(label)
    return {"est": est, "pred_s": pred_s, "max_hr": max_hr}


print("\n== 逐场回测（严格时序）==")
rows = []
hist_before: list[dict] = []   # 旧链路历史（feats，生产旧口径）
hist_after: list[dict] = []    # 证据管线历史（quality + ev_feats）
for a, std, genuine in races:
    day = dates.ts_to_date(a["start_ts"])
    label = DIST_LABELS[std]
    actual_s = a["duration_s"] * std / a["distance_m"]
    actual_v = vd.estimate_vdot(a["distance_m"], a["duration_s"])
    # Before：批33 之前生产口径（旧加权 + 分量逆误差加权 + 偏差校准，
    # 分发已短路——有历史走个人学习，无历史=纯旧模型，与旧生产落库一致）
    r1 = run_prediction(a, std, hist_before, legacy=True)
    # After：证据管线
    r2 = run_prediction(a, std, hist_after)
    pred1_s, pred2_s = r1["pred_s"], r2["pred_s"]
    err1 = (pred1_s - actual_s) if pred1_s else None
    err2 = (pred2_s - actual_s) if pred2_s else None
    est2 = r2["est"]
    interval = (est2.get("prediction_intervals") or {}).get(label) or {}
    half_vdot = interval.get("half_vdot")
    in_interval = None
    if genuine and half_vdot and pred2_s:
        pred_v = vd.estimate_vdot(std, pred2_s)
        in_interval = abs(actual_v - pred_v) <= half_vdot
    # Before 旧区间（批33 之前口径：该距离原始误差 RMSE 向 4.0 收编）
    old_iv = old_interval(hist_before, label) if genuine else None
    old_half = old_iv["half_vdot"] if old_iv else None
    in_old_interval = None
    if old_half and pred1_s:
        pred1_v = vd.estimate_vdot(std, pred1_s)
        in_old_interval = abs(actual_v - pred1_v) <= old_half
    # 两条链路各自记录本场误差（与各自生产落库口径一致）：
    # 生产只回放 detect_races 严格检测的比赛（名称/心率门），回测的宽松
    # race_band 多出的训练计时（轻松跑）不进历史——否则旧口径区间的原始
    # RMSE 会被 30+ VDOT 的计时跑撑爆（实测半宽 16 VDOT），真实生产不会。
    # 生产回放时 acts_year 含比赛日当天的活动（历史刷新在活动入库之后），
    # 而 est 的 race_grades 只含窗口内更早的比赛（本场 start_ts 不满足
    # < race_ts），所以这里用 detect_races 单独判定本场（检测是逐活动
    # 的，[a] 单元素与混在全窗口里的结论一致）。
    strict_here = bool(ab.detect_races([a], est_max))
    if pred1_s and strict_here:
        pred1_v = round(vd.estimate_vdot(std, pred1_s), 1)
        hist_before = personal.record(hist_before, {
            "day": day.isoformat(), "label": label,
            "actual_vdot": round(actual_v, 1), "pred_vdot": pred1_v,
            "feats": {c["kind"]: c["vdot"]
                      for c in r1["est"].get("components") or []}})
    if pred2_s and strict_here:
        pred2_v = round(vd.estimate_vdot(std, pred2_s), 1)
        race_rec = {"day": day.isoformat(), "label": label,
                    "actual_vdot": round(actual_v, 1), "name": a.get("name"),
                    "hr_ref": (round(a["avg_hr"] / est_max, 2)
                               if a.get("avg_hr") and est_max else None)}
        ev_list = est2.get("ev_list") or []
        prior_races = [e["actual_vdot"] for e in hist_after
                       if e.get("label") == label
                       and e.get("actual_vdot") is not None]
        quality = evm.grade_race(race_rec, ev_list,
                                 evm.conversion_deltas(hist_after), r2["max_hr"],
                                 prior_races=prior_races)
        ev_feats = {k: f.get("eff")
                    for k, f in (est2.get("ev_fitness") or {}).items()
                    if k in ("interval", "steady", "best_effort")
                    and f.get("eff") is not None}
        hist_after = personal.record(hist_after, {
            "day": day.isoformat(), "label": label,
            "actual_vdot": round(actual_v, 1), "pred_vdot": pred2_v,
            "quality": quality, "ev_feats": ev_feats,
            "feats": {c["kind"]: c["vdot"]
                      for c in est2.get("components") or []}})
    else:
        quality = None
    n_ev = len(est2.get("ev_list") or [])
    q_cur = quality if pred2_s else None
    # 赛前历史计数：剔除本场刚记录的条目
    n_allout = sum(1 for e in hist_after if e.get("quality") != "submax") \
        - (1 if q_cur == "all_out" else 0)
    n_label = sum(1 for e in hist_after
                  if e.get("label") == label and e.get("quality") != "submax") \
        - (1 if q_cur == "all_out" else 0)
    rows.append({"day": day.isoformat(), "label": label, "dist": std,
                 "genuine": genuine, "actual_s": actual_s, "actual_v": actual_v,
                 "pred1_s": pred1_s, "err1_s": err1,
                 "pred2_s": pred2_s, "err2_s": err2,
                 "half_vdot": half_vdot,
                 "in_interval": in_interval,
                 "old_half_vdot": old_half,
                 "in_old_interval": in_old_interval,
                 "n_ev": n_ev, "n_allout": n_allout,
                 "n_label": n_label,
                 "quality": q_cur,
                 "name": a["name"]})
    mark = "真" if genuine else "训"
    print("  %s %s%s 实际 %s VDOT %.1f | 旧 %+6.0fs | 新 %+6.0fs | 新半宽 %.1fVDOT 覆盖%s | 旧半宽 %s 覆盖%s | 证据%d 全开%d" % (
        day, mark, label, fmt_t(actual_s), actual_v,
        err1 if err1 is not None else float("nan"),
        err2 if err2 is not None else float("nan"),
        half_vdot if half_vdot else float("nan"),
        ("✓" if in_interval else "✗") if in_interval is not None else "—",
        ("%.1f" % old_half) if old_half else "—",
        ("✓" if in_old_interval else "✗") if in_old_interval is not None else "—",
        n_ev, n_allout))
    if genuine and pred2_s:
        # 证据管线内部分解：定位大误差来源
        comps = {c["kind"]: c["vdot"] for c in est2.get("components") or []}
        fit = est2.get("ev_fitness") or {}
        be = fit.get("best_effort_buckets") or {}
        fsum = {k: (f.get("vdot"), f.get("n")) for k, f in fit.items()
                if k in ("interval", "steady", "best_effort")}
        grades = " ".join("%s:%s" % (r["label"], r["quality"][:1])
                          for r in est2.get("race_grades") or [])
        print("      通道 %s | 训练能力 %s | 分段档 %s | 比赛通道 %.1f | 分级 %s"
              % (json.dumps(comps, ensure_ascii=False),
                 json.dumps(fsum, ensure_ascii=False),
                 json.dumps({k: v.get("vdot") for k, v in be.items()},
                            ensure_ascii=False),
                 est2.get("race_vdot") or float("nan"), grades))
        if label == "半马":
            print("      预测 %s | 依据 %s"
                  % (json.dumps(est2.get("predictions"), ensure_ascii=False),
                     json.dumps([d["source"] + ": " + d["detail"] for d in
                                 est2.get("evidence") or []], ensure_ascii=False)))


def stats(rows, key):
    vals = [r[key] for r in rows if r[key] is not None]
    if not vals:
        return None
    n = len(vals)
    ae = [abs(v) for v in vals]
    mae = sum(ae) / n
    med = statistics.median(ae)
    bias = sum(vals) / n
    rmse = (sum(v * v for v in vals) / n) ** 0.5
    return {"n": n, "mae_s": round(mae), "median_ae_s": round(med),
            "bias_s": round(bias), "rmse_s": round(rmse)}


def print_stats(title, rows, key):
    s = stats(rows, key)
    print("  %-30s" % title, json.dumps(s, ensure_ascii=False) if s else "无样本")


print("\n== 误差统计（err = 预测 − 实际，秒；正 = 预测偏慢/高估用时）==")
real = [r for r in rows if r["genuine"]]
print("  [真实比赛 n=%d / 全部检测 n=%d]" % (len(real), len(rows)))
print_stats("真实比赛 · 旧生产链路", real, "err1_s")
print_stats("真实比赛 · 证据管线(批33)", real, "err2_s")
print_stats("全部检测 · 旧生产链路", rows, "err1_s")
print_stats("全部检测 · 证据管线(批33)", rows, "err2_s")
for lab in ("5K", "10K", "半马", "全马"):
    sub = [r for r in rows if r["label"] == lab]
    subr = [r for r in real if r["label"] == lab]
    if sub:
        print_stats(lab + " · 旧", sub, "err1_s")
        print_stats(lab + " · 新", sub, "err2_s")
        if subr:
            print_stats(lab + " · 旧(真)", subr, "err1_s")
            print_stats(lab + " · 新(真)", subr, "err2_s")

# 预测区间：覆盖率 + 平均宽度（证据管线口径）
cov = [r for r in real if r["in_interval"] is not None]
if cov:
    n_in = sum(1 for r in cov if r["in_interval"])
    widths = [r["half_vdot"] for r in cov]
    print("\n== 预测区间（真实比赛）==")
    print("  After·证据管线：覆盖 %d/%d = %.0f%%（目标 ~80%%）| 平均半宽 %.2f VDOT"
          % (n_in, len(cov), n_in / len(cov) * 100,
             sum(widths) / len(widths) if widths else 0))
    for lab in ("5K", "10K", "半马", "全马"):
        sub = [r for r in cov if r["label"] == lab]
        if sub:
            w = sum(r["half_vdot"] for r in sub) / len(sub)
            half_s = statistics.median(
                int(round(r["pred2_s"] - vd.predict_time(r["dist"],
                    vd.estimate_vdot(r["dist"], r["pred2_s"]) + r["half_vdot"])))
                for r in sub)
            print("    %-4s n=%d 平均半宽 %.2f VDOT（≈%ds）" % (lab, len(sub), w, half_s))
cov_old = [r for r in real if r["in_old_interval"] is not None]
if cov_old:
    n_in_o = sum(1 for r in cov_old if r["in_old_interval"])
    widths_o = [r["old_half_vdot"] for r in cov_old]
    print("  Before·旧口径：覆盖 %d/%d = %.0f%% | 平均半宽 %.2f VDOT"
          % (n_in_o, len(cov_old), n_in_o / len(cov_old) * 100,
             sum(widths_o) / len(widths_o)))
    for lab in ("5K", "10K", "半马", "全马"):
        sub = [r for r in cov_old if r["label"] == lab]
        if sub:
            w = sum(r["old_half_vdot"] for r in sub) / len(sub)
            half_s = statistics.median(
                int(round(r["pred1_s"] - vd.predict_time(r["dist"],
                    vd.estimate_vdot(r["dist"], r["pred1_s"]) + r["old_half_vdot"])))
                for r in sub)
            print("    %-4s n=%d 平均半宽 %.2f VDOT（≈%ds）" % (lab, len(sub), w, half_s))

# 场景对比
print("\n== 场景对比（真实比赛，证据管线）==")
first = [r for r in real if r["n_label"] == 0]
print_stats("  该距离首场比赛（无同距离历史）", first, "err2_s")
print_stats("  有同距离比赛历史", [r for r in real if r["n_label"] > 0], "err2_s")
few = [r for r in real if r["n_allout"] <= 1]
print_stats("  比赛稀少（赛前全开比赛 ≤1 场）", few, "err2_s")
print_stats("  比赛较多（赛前全开比赛 >1 场）",
            [r for r in real if r["n_allout"] > 1], "err2_s")
rich = [r for r in real if r["n_ev"] >= 10]
print_stats("  训练证据丰富（窗口证据 ≥10 条）", rich, "err2_s")
print_stats("  训练证据稀疏（窗口证据 <10 条）",
            [r for r in real if r["n_ev"] < 10], "err2_s")
print_stats("  次全力比赛（训练计时，非真）",
            [r for r in rows if not r["genuine"]], "err2_s")
