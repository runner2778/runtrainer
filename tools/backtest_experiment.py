"""预测改进实验（只读）：在真实库的真实比赛子集上，模拟候选改进的
时序严格回测，逐项量化「优化前 vs 候选方案」的误差，供拍板用。

时序纪律同 backtest_predict.py：第 t 场预测只能看到 t 之前的数据
（含分量计算、权重的在线学习、个人偏差校准——全部 expanding-window）。

候选方案（都在分量加权层面模拟，不含上限钳制/PB 加分——那些属于
生产链路，最终以 backtest_predict 全链路复测为准）：
  S0 生产权重（含无比赛重排，比赛分量按年龄衰减）——基线
  S1 S0 + 手表 VO2max 只在无其它分量时兜底
  S2 S1 + 比赛分量新增「对照当场峰值 ≥0.94」通道（计时/测验跑）
  S3 S2 + 分量权重按历史在线学习（非负最小二乘，向先验收缩）
  S4 S3 + 按距离个人偏差校准（EWMA，α=0.2，±1.5 VDOT 封顶）
  S5 S2 + 按距离个人偏差校准（把校准效果单独拎出来看）
  S6 S2 + 校准的生产形态：EWMA + 样本收缩 n/(n+3)（小样本不乱跳）
  S7 S6 + 分量逆误差加权（历史平均绝对误差越大的分量权重越低）
另：ACWR（7d/28d 跑量比）与 S4 残差的相关性——判断疲劳特征值不值得进模型。
"""
import json
import math
import statistics
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo, health_repo  # noqa: E402
from runtrainer.db.repos import profile_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import load_metrics, vdot as vd  # noqa: E402
from runtrainer.utils import dates, jsonutil  # noqa: E402

database.migrate()

DIST_LABELS = {5000: "5K", 10000: "10K", 21097: "半马", 42195: "全马"}
OWN_RACE_MIN = 0.94          # 候选：对照当场峰值 ≥0.94 认作比赛（计时/测验跑）
PRIOR_K = 6.0                # 权重在线学习的先验强度（n/(n+6) 混合）
BIAS_ALPHA = 0.2             # 个人偏差 EWMA 学习率
BIAS_CAP = 1.5               # 个人偏差封顶（VDOT）


def decode(a: dict) -> dict:
    a = dict(a)
    raw = a.get("structure_json")
    a["structure"] = jsonutil.loads(raw) if raw else []
    return a


def race_band(a: dict, est_max: float | None) -> tuple[bool, int | None, bool]:
    """同 backtest_predict：生产三通道检测 + 「真实比赛」判据。"""
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


prof = profile_repo.get_profile() or {}
all_rows = activity_repo.list_activities(None, None, limit=20000)
decoded_all = [decode(a) for a in all_rows]
est_max = ab.estimate_max_hr(prof.get("max_hr"), decoded_all)

races = []
for a in sorted(decoded_all, key=lambda x: x["start_ts"]):
    ok, std, genuine = race_band(a, est_max)
    if ok and genuine:
        races.append((a, std))
print("真实比赛:", len(races), "场")


def comps_at(a: dict) -> dict:
    """赛前（不含本场）各分量 VDOT + 时序严格的过程量。"""
    race_ts, race_day = a["start_ts"], dates.ts_to_date(a["start_ts"])
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
            (race_day - timedelta(days=30)).isoformat(),
            (race_day - timedelta(days=1)).isoformat()) if r.get("resting_hr")]
        if rhrs:
            rest_hr = round(sorted(rhrs)[len(rhrs) // 2], 1)
    max_hr = ab.estimate_max_hr(prof.get("max_hr"), acts_win)
    year_bests = ab.distance_bests(
        acts_year,
        get_samples=(lambda aid: activity_repo.get_samples(aid))
        if any(x.get("has_samples") for x in acts_year) else None,
        max_hr=max_hr)
    out = {
        "race": ab.best_recent_race(acts_win, max_hr),
        "threshold": ab.threshold_pace_from_trend(acts_win, max_hr, race_day),
        "interval": ab.interval_ability(acts_win, max_hr, race_day),
        "hrr": ab.hrr_ability(acts_win, max_hr, rest_hr, race_day) if max_hr and rest_hr else None,
        "quality": ab.quality_workout_evidence(acts_win, max_hr, race_day) if max_hr else None,
        "efforts": ab._recent_efforts_evidence(year_bests, race_day),
        "vo2max": round(float(prof["vo2max"]), 1) if prof.get("vo2max") else None,
        "age_race": None,
    }
    # 候选通道：对照当场峰值 ≥0.94（计时/测验跑）也认比赛，但必须有
    # 平均心率 ≥75% 档案最大的强度下限——否则轻松跑的心率漂移（如
    # 127/135=0.94 的 60 分钟慢跑）会被当成比赛锚点
    own = None
    for x in acts_win:
        d, dur = x.get("distance_m"), x.get("duration_s")
        avg_hr, own_max = x.get("avg_hr"), x.get("max_hr")
        if not (d and dur and avg_hr and own_max
                and avg_hr / own_max >= OWN_RACE_MIN
                and avg_hr / est_max >= 0.75):
            continue
        struct = x.get("structure") or []
        if any(s.get("type") == "rest" for s in struct):
            continue
        if any(h in (x.get("name") or "").lower() for h in ab.RACE_NAME_HINTS):
            continue
        for std, tol in ab.RACE_BANDS:
            if abs(d - std) / std <= tol:
                rec = {"distance_m": int(std), "duration_s": round(dur),
                       "date": x["start_ts"],
                       "vdot": round(vd.estimate_vdot(d, dur), 1),
                       "name": x.get("name")}
                if own is None or rec["vdot"] > own["vdot"]:
                    own = rec
                break
    out["race_own"] = own
    # 比赛分量年龄
    if out["race"]:
        out["age_race"] = ab._rec_age_days(out["race"], race_day)
    # ACWR（7d/28d 跑量比）——疲劳候选特征
    km7, km28 = 0.0, 0.0
    for x in acts_year:
        if x.get("distance_m") and load_metrics.is_running(x.get("sport")):
            age = (race_day - dates.ts_to_date(x["start_ts"])).days
            if 0 <= age < 7:
                km7 += x["distance_m"] / 1000.0
            if 0 <= age < 28:
                km28 += x["distance_m"] / 1000.0
    out["acwr"] = (km7 / 7) / (km28 / 28) if km28 > 0 else None
    return out


rows = []
for a, std in races:
    c = comps_at(a)
    rows.append({
        "day": dates.ts_to_date(a["start_ts"]),
        "label": DIST_LABELS[std],
        "actual_v": round(vd.estimate_vdot(a["distance_m"], a["duration_s"]), 1),
        "comps": c,
    })
print("组件计算完成\n")


def race_w(c) -> float:
    """生产口径：比赛权重按年龄 60 天全额 0.28 → 180 天线性降到 0.12。"""
    age = c["age_race"]
    if age is None:
        return ab.RACE_WEIGHT
    if age <= ab.RACE_FULL_DAYS:
        return ab.RACE_WEIGHT
    return max(ab.RACE_WEIGHT_FLOOR, ab.RACE_WEIGHT
               - (ab.RACE_WEIGHT - ab.RACE_WEIGHT_FLOOR)
               * (age - ab.RACE_FULL_DAYS) / (180 - ab.RACE_FULL_DAYS))


# 各方案共享的「先验权重」（kind → w），S3 学习后与它混合
def prior_weights(c, race_v) -> dict:
    has_race = race_v is not None
    if has_race:
        return {"race": race_w(c), "vo2max": 0.16, "threshold": 0.22,
                "hrr": 0.10, "interval": 0.12, "quality": 0.16,
                "efforts": ab.EFFORT_WEIGHT}
    return {"vo2max": 0.10, "threshold": 0.30, "quality": 0.20,
            "hrr": 0.14, "interval": 0.16, "efforts": ab.EFFORT_WEIGHT}


def nnls_fit(feats, acts, init, iters=1500, lr=0.05):
    """非负最小二乘（投影梯度，单纯形约束 Σw=1）。feats: [{kind: vdot}...]"""
    keys = list(init)
    w = [init[k] / sum(init.values()) for k in keys]
    n = len(feats)
    for _ in range(iters):
        g = [0.0] * len(keys)
        for i in range(n):
            pred = sum(feats[i].get(k, 0.0) * w[j] for j, k in enumerate(keys)
                       if feats[i].get(k) is not None)
            # 缺失特征按「不参与」处理：残差按在场权重重缩放
            denom = sum(w[j] for j, k in enumerate(keys) if feats[i].get(k) is not None)
            if denom <= 0:
                continue
            pred = pred / denom
            resid = pred - acts[i]
            for j, k in enumerate(keys):
                if feats[i].get(k) is not None:
                    g[j] += 2 * resid * (feats[i][k] - pred) / denom
        for j in range(len(keys)):
            w[j] = max(0.0, w[j] - lr * g[j] / max(n, 1))
        s = sum(w)
        if s > 0:
            w = [x / s for x in w]
    return dict(zip(keys, w))


# 展开时序模拟
sims = {k: [] for k in ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7")}
bias = {tag: {} for tag in ("S4", "S5", "S6", "S7")}   # 各方案自己的按距离 EWMA（VDOT）
w_learned = None          # S3 学到的权重（保留给调试）
hist = []                 # (day, label, feats, actual_v, prior_pred_S2)


def calib_bias(b, label, err, alpha, cap=BIAS_CAP):
    """EWMA 更新；b[label] = {bias, n}。"""
    s = b.get(label, {"bias": 0.0, "n": 0})
    s = {"bias": max(-cap, min(cap, s["bias"] + alpha * (err - s["bias"]))),
         "n": s["n"] + 1}
    b[label] = s
    return s


def calib_apply(b, label, shrink_k=3.0):
    """应用：偏差按样本数收缩 n/(n+k)。"""
    s = b.get(label)
    if not s or not s["n"]:
        return 0.0
    return s["bias"] * s["n"] / (s["n"] + shrink_k)

for i, r in enumerate(rows):
    c, act_v = r["comps"], r["actual_v"]
    label = r["label"]
    # S2 的 race 分量 = 生产 race 与 own 通道更强的一个
    race_s2 = c["race"]
    if c["race_own"] and (not race_s2 or c["race_own"]["vdot"] > race_s2["vdot"]):
        race_s2 = c["race_own"]
    race_s2_w = None
    if race_s2:
        # own 通道的比赛按相同年龄规则（用其 date 重算年龄）
        age = ab._rec_age_days(race_s2, r["day"]) if race_s2.get("date") else None
        c2 = {"age_race": age}
        race_s2_w = race_w(c2)

    def feats_for(race_v, drop_vo2):
        f = {}
        for kind in ("race", "threshold", "interval", "hrr", "quality", "efforts",
                     "vo2max"):
            v = c[kind]
            if kind == "race":
                v = race_v
            if v is not None:
                f[kind] = v["vdot"] if isinstance(v, dict) else v
        # 生产语义：vo2max 只在没有其它任何分量时兜底
        if drop_vo2 and "vo2max" in f and len(f) > 1:
            del f["vo2max"]
        return f

    preds = {}
    # S0：生产
    p0 = prior_weights(c, c["race"])
    f0 = feats_for(c["race"], drop_vo2=False)
    v0 = sum(f0[k] * p0[k] for k in f0) / sum(p0[k] for k in f0) if f0 else None
    preds["S0"] = v0
    # S1：+ vo2max 兜底
    f1 = feats_for(c["race"], drop_vo2=True)
    p1 = prior_weights(c, c["race"])
    preds["S1"] = (sum(f1[k] * p1[k] for k in f1) / sum(p1[k] for k in f1)
                   if f1 else None)
    # S2：+ own 比赛通道
    f2 = feats_for(race_s2, drop_vo2=True)
    p2 = {"race": race_s2_w} if race_s2_w is not None else {}
    p2.update(p1)
    if race_s2_w is None:
        p2.pop("race", None)
    preds["S2"] = (sum(f2[k] * p2[k] for k in f2) / sum(p2[k] for k in f2)
                   if f2 else None)
    # S3：+ 权重在线学习（只用此前比赛拟合，向 S2 先验收缩）
    if i > 0 and hist:
        n_prior = len(hist)
        feats_h = [h["feats_s2"] for h in hist]
        acts_h = [h["actual_v"] for h in hist]
        init = {"race": 0.28, "threshold": 0.22, "hrr": 0.10, "interval": 0.12,
                "quality": 0.16, "efforts": 0.14, "vo2max": 0.05}
        w_fit = nnls_fit(feats_h, acts_h, init)
        w_learned = w_fit
        # 与 S2 先验混合：n/(n+6) 学到的 + 6/(n+6) 先验（先验取 S0 口径）
        mix = n_prior / (n_prior + PRIOR_K)
        p3 = {}
        for k in set(list(w_fit) + list(p2)):
            p3[k] = mix * w_fit.get(k, 0.0) + (1 - mix) * p2.get(k, 0.0)
        preds["S3"] = (sum(f2[k] * p3[k] for k in f2) / sum(p3[k] for k in f2)
                       if f2 else None)
    else:
        preds["S3"] = preds["S2"]
    # S4/S5：按距离个人偏差校准（各自维护自己的偏差序列）
    for tag in ("S4", "S5"):
        base = preds["S3"] if tag == "S4" else preds["S2"]
        preds[tag] = base + bias[tag].get(label, 0.0) if base is not None else None
    # S6：S2 + 校准的生产形态（EWMA + 样本收缩 n/(n+3)，小样本不乱跳）
    preds["S6"] = (preds["S2"] + calib_apply(bias["S6"], label)
                   if preds["S2"] is not None else None)
    # S7：S6 + 分量逆误差加权（历史平均绝对误差越大的分量权重越低）
    p7 = p2
    if hist:
        errs = {k: [] for k in p2}
        for h in hist:
            for k in p2:
                if h["feats_s2"].get(k) is not None:
                    errs[k].append(abs(h["feats_s2"][k] - h["actual_v"]))
        p7 = {k: p2[k] / (0.5 + sum(errs[k]) / len(errs[k])) if errs[k] else p2[k]
              for k in p2}
    v7 = (sum(f2[k] * p7[k] for k in f2) / sum(p7[k] for k in f2)) if f2 else None
    preds["S7"] = (v7 + calib_apply(bias["S7"], label)) if v7 is not None else None
    for tag, v in preds.items():
        sims[tag].append((r["day"], label, act_v, v))
    # 更新：本场误差进各方案自己的个人偏差（S4 用 S4 的误差，S5 用 S5 的）
    if preds["S4"] is not None:
        b = bias["S4"]
        b[label] = max(-BIAS_CAP, min(BIAS_CAP,
                                      b.get(label, 0.0)
                                      + BIAS_ALPHA * (act_v - preds["S4"])))
    if preds["S5"] is not None:
        b = bias["S5"]
        b[label] = max(-BIAS_CAP, min(BIAS_CAP,
                                      b.get(label, 0.0)
                                      + BIAS_ALPHA * (act_v - preds["S5"])))
    for tag in ("S6", "S7"):
        if preds[tag] is not None:
            calib_bias(bias[tag], label, act_v - preds[tag], BIAS_ALPHA)
    hist.append({"day": r["day"], "label": label, "feats_s2": f2,
                 "actual_v": act_v})


def err_stats(pairs):
    vals = [p - a for _, _, a, p in pairs if p is not None]
    if not vals:
        return None
    n = len(vals)
    ae = [abs(v) for v in vals]
    return {"n": n, "mae_vdot": round(sum(ae) / n, 2),
            "median_ae_vdot": round(statistics.median(ae), 2),
            "bias_vdot": round(sum(vals) / n, 2),
            "rmse_vdot": round((sum(v * v for v in vals) / n) ** 0.5, 2)}


print("== 各方案（真实比赛，时序严格；VDOT 单位，1 VDOT ≈ 5K 12–18s）==")
for tag in sims:
    s = err_stats(sims[tag])
    print("  %-4s" % tag, json.dumps(s, ensure_ascii=False) if s else "无样本")

print("\n== 按距离（S0 vs S2 vs S4 vs S6 vs S7；bias 正 = 高估用时/低估水平）==")
for lab in ("5K", "10K", "半马"):
    for tag in ("S0", "S2", "S4", "S6", "S7"):
        s = err_stats([x for x in sims[tag] if x[1] == lab])
        print("  %s %-4s" % (lab, tag),
              json.dumps(s, ensure_ascii=False) if s else "无样本")

print("\n== 逐场（S0 → S2 → S4 → S7 预测 VDOT vs 实际）==")
for i, r in enumerate(rows):
    parts = []
    for tag in ("S0", "S2", "S4", "S7"):
        _, _, a, p = sims[tag][i]
        parts.append("%s %s%s" % (tag, p if p is not None else "--",
                                  "" if p is None else "%+4.1f" % (p - a)))
    print("  %s %-4s 实际 %4.1f | %s" % (
        r["day"], r["label"], r["actual_v"], " | ".join(parts)))

print("\n== S3 最后一场前学到的权重（NNLS 拟合）==")
if w_learned:
    print("  ", json.dumps({k: round(v, 3) for k, v in w_learned.items()},
                           ensure_ascii=False))

# 疲劳特征相关性：ACWR 与 S4 残差
print("\n== ACWR/疲劳特征 vs S4 残差（相关性；|r|>0.5 才值得进模型）==")
xs, ys = [], []
for i, r in enumerate(rows):
    a, p = sims["S4"][i][2], sims["S4"][i][3]
    ac = r["comps"]["acwr"]
    if p is not None and ac is not None:
        xs.append(ac)
        ys.append(p - a)
if len(xs) >= 3:
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = cov / (sum((x - mx) ** 2 for x in xs)
               * sum((y - my) ** 2 for y in ys)) ** 0.5
    print("  ACWR n=%d r=%.2f" % (len(xs), r))
    for x, y in zip(xs, ys):
        print("    acwr=%4.2f resid=%+5.1f" % (x, y))
else:
    print("  样本不足")
