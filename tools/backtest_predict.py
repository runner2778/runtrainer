"""成绩预测回测（只读）：对真实库里每一场「比赛」做严格时序预测。

时序纪律：预测某场比赛时只能用 start_ts < 该场比赛 start_ts 的数据
（含近一年最佳、分段扫描、健康数据、课表完成度——全部按 as_of 裁剪）。
预测值 = compute_ability 的等效成绩；真实值 = 实际用时按距离等比折算到
标准距离（9.8km 计时 → 10K 用时）。另做两个基线：
  population = 赛前 180 天内最佳比赛的 VDOT 直接换等效成绩（纯群体公式，无个性化）
  components = 各分量单独预测该场（阈值/HRR/间歇/最快分段/专项课/手表读数）
输出按距离统计的 MAE / 中位绝对误差 / 偏差 / RMSE / MAPE。
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
from runtrainer.domain import load_metrics, personal, vdot as vd  # noqa: E402
from runtrainer.utils import dates, jsonutil  # noqa: E402

database.migrate()

DIST_LABELS = {5000: "5K", 10000: "10K", 21097: "半马", 42195: "全马"}


def decode(a: dict) -> dict:
    a = dict(a)
    raw = a.get("structure_json")
    a["structure"] = jsonutil.loads(raw) if raw else []
    return a


def race_band(a: dict, est_max: float | None) -> tuple[bool, int | None, bool]:
    """该活动是否算「比赛」（与模型自身判定同款：名称提示 / 档案最大心率
    90% / 本场峰值 88% 三通道，排除间歇结构），命中则返回标准距离；
    第三个返回值 = 是否「真实比赛」（地面真值判据：名称提示，或 对照当场
    峰值 ≥88% 且 平均心率 ≥75% 档案最大——把轻松跑心率漂移冒充的
    「比赛」滤掉，回测头条统计只用真实比赛）。"""
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


# ---------- 数据清单 ----------
prof = profile_repo.get_profile() or {}
all_rows = activity_repo.list_activities(None, None, limit=20000)
decoded_all = [decode(a) for a in all_rows]
est_max = ab.estimate_max_hr(prof.get("max_hr"), decoded_all)
print("== 数据清单 ==")
print("活动总数:", len(all_rows),
      "| 带结构:", sum(1 for a in decoded_all if a.get("structure")),
      "| 带样本:", sum(1 for a in decoded_all if a.get("has_samples")))
print("档案: vo2max=%s max_hr=%s rest_hr=%s birth=%s" % (
    prof.get("vo2max"), prof.get("max_hr"), prof.get("rest_hr"), prof.get("birth_year")))
health_rows = health_repo.get_health("2000-01-01")
print("健康数据:", len(health_rows), "天", health_rows[0]["date"] if health_rows else "",
      "→", health_rows[-1]["date"] if health_rows else "")
if all_rows:
    lo = dates.ts_to_date(min(a["start_ts"] for a in all_rows))
    hi = dates.ts_to_date(max(a["start_ts"] for a in all_rows))
    print("活动时间范围:", lo, "→", hi)

races = []
for a in sorted(decoded_all, key=lambda x: x["start_ts"]):
    ok, std, genuine = race_band(a, est_max)
    if ok:
        races.append((a, std, genuine))
print("\n== 检测到的比赛（按时间序）==  共", len(races), "场",
      "| 真实比赛", sum(1 for *_, g in races if g), "场")
for a, std, genuine in races:
    day = dates.ts_to_date(a["start_ts"])
    v = vd.estimate_vdot(a["distance_m"], a["duration_s"])
    print("  %s  %s  %-6s %7.0fm  %s  用时 %s  等效VDOT %.1f  avg_hr=%s max=%s  %s" % (
        day, ("真" if genuine else "训") + " " + DIST_LABELS[std], std, a["distance_m"],
        fmt_t(a["duration_s"]), a["name"],
        v, a.get("avg_hr"), a.get("max_hr"), a["source"]))

# ---------- 逐场回测 ----------
plan = plan_repo.get_active_plan()
results = []


def run_prediction(a: dict, std: int, personal_history: list[dict] | None = None) -> dict:
    """对比赛 a（标准距离 std）做严格时序预测。返回预测与过程信息。

    personal_history: 个人误差历史（只含此前比赛）→ 生产个人学习链路
    （分量逆误差加权 + 按距离偏差校准 + 预测区间）；None = 无学习。
    """
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
    # 近一年最佳：只扫赛前活动（赛本身不在内）
    max_hr = ab.estimate_max_hr(prof.get("max_hr"), acts_win)
    year_bests = ab.distance_bests(
        acts_year,
        get_samples=(lambda aid: activity_repo.get_samples(aid))
        if any(x.get("has_samples") for x in acts_year) else None,
        max_hr=max_hr)
    est = ab.compute_ability(acts_win, prof.get("vo2max"), prof.get("max_hr"),
                             rest_hr=rest_hr, as_of=as_of, year_bests=year_bests,
                             plan_exec=plan_exec, personal_history=personal_history)
    label = DIST_LABELS[std]
    pred_s = (est.get("predictions") or {}).get(label)
    return {"est": est, "pred_s": pred_s, "acts_win": acts_win, "year_bests": year_bests,
            "rest_hr": rest_hr, "plan_exec": plan_exec}


print("\n== 逐场回测（严格时序；优化前 = 无个人历史，优化后 = 生产个人学习链路）==")
history: list[dict] = []
for a, std, genuine in races:
    r = run_prediction(a, std)
    est = r["est"]
    day = dates.ts_to_date(a["start_ts"])
    label = DIST_LABELS[std]
    actual_s = a["duration_s"] * std / a["distance_m"]
    pred_s = r["pred_s"]
    err = (pred_s - actual_s) if pred_s else None
    # 优化后：生产个人学习链路，历史只含此前比赛（与线上逐场学习同口径）
    r2 = run_prediction(a, std, personal_history=history)
    est2 = r2["est"]
    pred2_s = r2["pred_s"]
    err2 = (pred2_s - actual_s) if pred2_s else None
    interval = (est2.get("prediction_intervals") or {}).get(label) or {}
    half_vdot = interval.get("half_vdot")
    in_interval = None
    if genuine and half_vdot and pred2_s:
        actual_v = vd.estimate_vdot(a["distance_m"], a["duration_s"])
        pred_v = vd.estimate_vdot(std, pred2_s)
        in_interval = abs(actual_v - pred_v) <= half_vdot
    if genuine and pred2_s:
        actual_v = vd.estimate_vdot(a["distance_m"], a["duration_s"])
        pred_v = vd.estimate_vdot(std, pred2_s)
        history = personal.record(history, {
            "day": day.isoformat(), "label": label,
            "actual_vdot": round(actual_v, 1), "pred_vdot": round(pred_v, 1),
            "feats": {c["kind"]: c["vdot"]
                      for c in est2.get("components") or []}})
    # 群体基线：赛前 180 天内最佳比赛 VDOT 直接换等效
    base = ab.best_recent_race(r["acts_win"], est.get("max_hr"))
    base_pred = vd.predict_time(std, base["vdot"]) if base else None
    base_err = (base_pred - actual_s) if base_pred else None
    results.append({"day": day.isoformat(), "label": label, "dist": std,
                    "genuine": genuine,
                    "actual_s": actual_s, "pred_s": pred_s, "err_s": err,
                    "pred2_s": pred2_s, "err2_s": err2,
                    "in_interval": in_interval,
                    "base_err_s": base_err, "base_vdot": base and base["vdot"],
                    "vdot": est.get("vdot"),
                    "sources": [e["source"] for e in est.get("evidence") or []],
                    "name": a["name"], "raw_dist": a["distance_m"]})
    print("  %s %-5s 实际 %s  前 %s %+6.0fs  后 %s %+6.0fs  基线(群体) %+6.0fs  VDOT %.1f" % (
        day, label, fmt_t(actual_s),
        fmt_t(pred_s) if pred_s else "  无",
        err if err is not None else float("nan"),
        fmt_t(pred2_s) if pred2_s else "  无",
        err2 if err2 is not None else float("nan"),
        base_err if base_err is not None else float("nan"),
        est.get("vdot") or float("nan")))
    print("        证据: " + ", ".join(e["source"] for e in est2.get("evidence") or []))


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
    mape = sum(abs(v) / r["actual_s"] for v, r in zip(vals, rows)
               if r[key] is not None) / n * 100
    return {"n": n, "mae_s": round(mae), "median_ae_s": round(med),
            "bias_s": round(bias), "rmse_s": round(rmse), "mape_pct": round(mape, 1)}


def print_stats(title, rows, key):
    s = stats(rows, key)
    print("  %-28s" % title, json.dumps(s, ensure_ascii=False) if s else "无样本")


print("\n== 误差统计（err = 预测 − 实际，秒；正 = 预测偏慢/高估用时）==")
real = [r for r in results if r["genuine"]]
print("  [真实比赛子集 n=%d / 全部检测 n=%d]" % (len(real), len(results)))
print_stats("真实比赛 · 优化前(无个人历史)", real, "err_s")
print_stats("真实比赛 · 优化后(个人学习)", real, "err2_s")
print_stats("真实比赛 · 群体基线", real, "base_err_s")
print_stats("全部检测 · 优化前(无个人历史)", results, "err_s")
print_stats("全部检测 · 优化后(个人学习)", results, "err2_s")
print_stats("全部检测 · 群体基线", results, "base_err_s")
for lab in ("5K", "10K", "半马", "全马"):
    sub = [r for r in results if r["label"] == lab]
    subr = [r for r in real if r["label"] == lab]
    if sub:
        print_stats(lab + " · 优化前", sub, "err_s")
        print_stats(lab + " · 优化后", sub, "err2_s")
        print_stats(lab + " · 群体基线", sub, "base_err_s")
        if subr:
            print_stats(lab + " · 优化前(真)", subr, "err_s")
            print_stats(lab + " · 优化后(真)", subr, "err2_s")

# 预测区间覆盖率：真实比赛实际值落在优化后预测 ±半宽内的比例
cov = [r for r in real if r["in_interval"] is not None]
if cov:
    n_in = sum(1 for r in cov if r["in_interval"])
    print("\n== 预测区间覆盖率（真实比赛，优化后口径；半宽 = 1.28×历史误差RMSE）==")
    print("  覆盖 %d/%d = %.0f%%（目标 ~80%%）" % (n_in, len(cov), n_in / len(cov) * 100))

# ---------- 分量消融：每个分量单独预测该场比赛 ----------
print("\n== 分量消融（各分量单独预测，err 同上）==")
comp_rows = []
for a, std, genuine in races:
    r = run_prediction(a, std)
    day = dates.ts_to_date(a["start_ts"])
    label = DIST_LABELS[std]
    actual_s = a["duration_s"] * std / a["distance_m"]
    acts, yb = r["acts_win"], r["year_bests"]
    max_hr = ab.estimate_max_hr(prof.get("max_hr"), acts)
    race = ab.best_recent_race(acts, max_hr)
    thr = ab.threshold_pace_from_trend(acts, max_hr, day)
    iv = ab.interval_ability(acts, max_hr, day)
    hrr = ab.hrr_ability(acts, max_hr, r["rest_hr"], day) if r["rest_hr"] else None
    q = ab.quality_workout_evidence(acts, max_hr, day)
    eff = ab._recent_efforts_evidence(yb, day)
    cands = {
        "race(比赛)": race["vdot"] if race else None,
        "threshold(阈值回归)": thr["vdot"] if thr else None,
        "interval(间歇)": iv["vdot"] if iv else None,
        "hrr(储备心率)": hrr["vdot"] if hrr else None,
        "quality(专项课)": q["vdot"] if q else None,
        "efforts(最快分段)": eff["vdot"] if eff else None,
        "vo2max(手表)": round(float(prof["vo2max"]), 1) if prof.get("vo2max") else None,
    }
    row = {"day": day.isoformat(), "label": label, "actual_s": actual_s,
           "genuine": genuine}
    for name, v in cands.items():
        row["c_" + name] = (vd.predict_time(std, v) - actual_s) if v else None
    comp_rows.append(row)
for name in ("race(比赛)", "threshold(阈值回归)", "interval(间歇)", "hrr(储备心率)",
             "quality(专项课)", "efforts(最快分段)", "vo2max(手表)"):
    print_stats(name, comp_rows, "c_" + name)
    print_stats(name + " · 真实子集", [r for r in comp_rows if r["genuine"]],
                "c_" + name)

# ---------- 个人距离转换关系 ----------
print("\n== 个人距离转换（本场 VDOT vs 其它距离比赛 VDOT 的差，只看真实比赛）==")
own_v = []
for a, std, genuine in races:
    if genuine:
        own_v.append((DIST_LABELS[std], dates.ts_to_date(a["start_ts"]),
                      round(vd.estimate_vdot(a["distance_m"], a["duration_s"]), 1)))
for lab, day, v in own_v:
    print("  %s %s 本场等效 VDOT %.1f" % (day, lab, v))
for i, (la, da, va) in enumerate(own_v):
    for lb, db, vb in own_v[i + 1:]:
        print("  %s(%s %.1f) → %s(%.1f)：个人偏移 %+.1f VDOT（群体模型偏移为 0）"
              % (la, da, va, lb, vb, vb - va))
