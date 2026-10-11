"""运动员个人响应模型（AI 教练升级 · 长期数字画像）。

教练不再把用户当成「5K 成绩 + 半马成绩 + VO2max」三个数字，而是从真实
活动 / 健康 / 比赛数据里学习「这个人」的反应规律：

- pace_hr           个人配速-心率曲线（20s 档均值±σ）——判断某天心率是否
                    异常的基线。同一配速对 A/B/C 运动员心率完全不同，
                    不能按绝对心率判断强度。
- fatigue_hr        前夜 RHR 偏高 / HRV 偏低时，次日同配速心率的预期偏离
                    ——识别「昨天练得有点重」在今天身体上的残留信号。
- drift             匀速长跑心率漂移的个人中位（按时长分档）——判断今天
                    漂移偏大还是正常，不拿群体平均值套个人。
- split             前/后 1/3 速度比个人中位——后程能力画像。
- recovery          强度日 vs 轻松日次日 RHR 变化——恢复速度。
- distance_profile  各距离个人偏差与预测区间（personal_history，可回放）。
- speed_reserve     5K vs 半马最佳 VDOT 差——速度储备方向。
- blocks            训练响应区块观察（区块构成 → 同配速心率变化，
                    小样本，仅趋势参考，不冒充定论）。

全部特征都带 n（样本量）；样本不足就明说不足，绝不装精确。

数据依据（真实库 E1–E5 实验，2026-10-03，
tools/_experiment_coach_response.py）：
- E1 同配速心率个人模型 MAE 8.4 bpm → 「心率异常偏高」需 ≥15 bpm 偏离
  才有区分力（8.4 bpm 是日常噪声水平）。
- E2 疲劳信号真实存在：前夜 RHR≥基线+3 → 次日同配速心率 +3.7 bpm
  （正常日 −0.9）；前夜 HRV≤基线−10 → +2.2 bpm（正常日 −2.3）；
  睡眠时长无效应——【证据不足】，模型不学睡眠敏感度。
- E3 长跑漂移中位 +10 bpm（≥40 分钟匀速跑，n=274）。
- E4 RHR 是可靠恢复指标：强度日后次日 RHR +1.0、轻松日 +0.2；
  HRV 次日 +2.1 反弹（方向与直觉相反）——恢复判断只用 RHR，不用 HRV。
- E5 前/后 1/3 速度比中位 0.915 → 该运动员后程快型（负分段）。
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

from runtrainer.domain import personal
from runtrainer.domain import workout_analysis as wa
from runtrainer.utils import dates, jsonutil

# ---- 学习窗口与样本门槛 ----
PACE_HR_DAYS = 180          # 配速-心率 / 疲劳效应 / 恢复速度 的窗口（天）
SAMPLE_DAYS = 180           # 漂移 / 后程（采样曲线）窗口
BLOCK_WEEKS = 6             # 训练响应区块长度（周）
BLOCK_COUNT = 8             # 保留最近区块数
MIN_BIN_N = 3               # 配速档最少样本
MIN_BASELINE_N = 30         # 健康基线（RHR/HRV 中位）最少天数
MIN_DRIFT_SEC = 40 * 60     # 漂移样本最短时长（匀速跑）
MIN_SPLIT_SEC = 30 * 60     # 后程样本最短时长
MIN_DRIFT_SAMPLES = 100     # 漂移采样点下限
MIN_SPLIT_SAMPLES = 80      # 后程采样点下限
MIN_BLOCK_RUNS = 5          # 区块最少样本（低于此不参与跨区块比较）
BIN_S = 20                  # 配速档宽（秒）

# ---- 疲劳效应判定阈值（E2 实验口径）----
RHR_HIGH_DELTA = 3          # 前夜 RHR ≥ 基线+3 视为偏高
HRV_LOW_DELTA = 10          # 前夜 HRV ≤ 基线−10 视为偏低
# 对照组带宽（「正常日」口径，同 E2）
RHR_NORMAL_BAND = 2
HRV_NORMAL_BAND = 10

# ---- 恢复判定口径（E4 实验口径）----
HARD_HR_MIN, HARD_DUR_MIN = 165, 30 * 60
EASY_HR_MAX, EASY_DUR_MIN = 135, 30 * 60

# ---- 区块强度分类（心率储备，与 workout_analysis 同源）----
LT1_HRR = wa.LT1_HRR
LT2_HRR = wa.LT2_HRR
CAT_NAMES = {"easy": "有氧轻松", "moderate": "中等强度", "hard": "高强度"}

DURATION_BUCKETS = ((40, 60), (60, 90), (90, 1 << 20))
RACE_LABELS = ("5K", "10K", "半马", "全马")


def _pace_bin(pace_s: float) -> int:
    return int(pace_s // BIN_S) * BIN_S


def _valid_rows(acts: list[dict], cutoff: date) -> list[dict]:
    """窗口内可参与配速-心率学习的行（过滤口径与 wa._valid_pace_hr 一致）。"""
    rows = []
    for a in acts:
        if not wa._valid_pace_hr(a):
            continue
        if dates.ts_to_date(a["start_ts"]) < cutoff:
            continue
        rows.append(a)
    return rows


def _loads_segments(a: dict) -> list:
    segs = a.get("structure_json")
    if isinstance(segs, str):
        return jsonutil.loads(segs) or []
    return segs or []


# ---------------------------------------------------------------------------
# 各特征构建
# ---------------------------------------------------------------------------

def _pace_hr(rows: list[dict]) -> dict:
    bins: dict[int, list[float]] = defaultdict(list)
    for a in rows:
        bins[_pace_bin(a["avg_pace_s_km"])].append(a["avg_hr"])
    out = {}
    for b, hrs in sorted(bins.items()):
        if len(hrs) < MIN_BIN_N:
            continue
        out[str(b)] = {"n": len(hrs), "mean": round(statistics.fmean(hrs), 1),
                       "std": round(statistics.pstdev(hrs), 1)}
    return {"bins": out, "n_rows": len(rows)}


def _fatigue_hr(rows: list[dict], health: list[dict], pace_hr: dict) -> dict:
    """前夜健康偏离基线时，次日同配速心率偏离个人基线的平均量。"""
    rhrs = [h["resting_hr"] for h in health if h.get("resting_hr")]
    hrvs = [h["hrv_avg_ms"] for h in health if h.get("hrv_avg_ms")]
    if len(rhrs) < MIN_BASELINE_N or len(hrvs) < MIN_BASELINE_N:
        return {"available": False,
                "note": f"健康基线样本不足（RHR {len(rhrs)} 天 / HRV {len(hrvs)} 天，需各 ≥{MIN_BASELINE_N}）"}
    rhr_med = statistics.median(rhrs)
    hrv_med = statistics.median(hrvs)
    bin_mean = {int(k): v["mean"] for k, v in pace_hr["bins"].items()}
    by_day = {h["date"]: h for h in health}
    groups: dict[str, list[float]] = defaultdict(list)
    for a in rows:
        b = _pace_bin(a["avg_pace_s_km"])
        if b not in bin_mean:
            continue
        prev = by_day.get((dates.ts_to_date(a["start_ts"])
                            - timedelta(days=1)).isoformat()) or {}
        dev = a["avg_hr"] - bin_mean[b]
        r, v = prev.get("resting_hr"), prev.get("hrv_avg_ms")
        if r and r >= rhr_med + RHR_HIGH_DELTA:
            groups["rhr_high"].append(dev)
        elif r and abs(r - rhr_med) <= RHR_NORMAL_BAND:
            groups["rhr_normal"].append(dev)
        if v and v <= hrv_med - HRV_LOW_DELTA:
            groups["hrv_low"].append(dev)
        elif v and abs(v - hrv_med) <= HRV_NORMAL_BAND:
            groups["hrv_normal"].append(dev)
    out: dict = {"available": True,
                 "baseline": {"rhr_median": round(rhr_med, 1),
                              "hrv_median": round(hrv_med, 1),
                              "n_health": len(health)}}
    for k, devs in groups.items():
        out[k] = ({"mean_dev": round(statistics.fmean(devs), 1), "n": len(devs)}
                  if len(devs) >= MIN_BIN_N else {"mean_dev": None, "n": len(devs)})
    return out


def _drift(acts: list[dict], samples_getter, today: date) -> dict:
    """匀速长跑心率漂移（后半−前半）的个人分布，按时长分档。"""
    cutoff = today - timedelta(days=SAMPLE_DAYS)
    drifts = []
    for a in acts:
        if dates.ts_to_date(a["start_ts"]) < cutoff or not a.get("has_samples"):
            continue
        if (a.get("duration_s") or 0) < MIN_DRIFT_SEC:
            continue
        if len(_loads_segments(a)) > 1:  # 只要纯匀速
            continue
        samples = samples_getter(a["id"])
        rows = [s for s in samples
                if s.get("hr") and s.get("t_offset_s") is not None
                and 90 <= s["hr"] <= 220]
        if len(rows) < MIN_DRIFT_SAMPLES:
            continue
        rows.sort(key=lambda s: s["t_offset_s"])
        mid = len(rows) // 2
        h1 = statistics.fmean(s["hr"] for s in rows[:mid])
        h2 = statistics.fmean(s["hr"] for s in rows[mid:])
        drifts.append({"drift": h2 - h1, "dur_min": a["duration_s"] / 60})
    if not drifts:
        return {"n": 0}
    out: dict = {"n": len(drifts),
                 "median_bpm": round(statistics.median(
                     d["drift"] for d in drifts), 1),
                 "by_duration": {}}
    for lo, hi in DURATION_BUCKETS:
        sel = [d["drift"] for d in drifts if lo <= d["dur_min"] < hi]
        if len(sel) >= MIN_BIN_N:
            label = f"{lo}-{hi}" if hi < 1000 else f"{lo}+"
            out["by_duration"][label] = {"median": round(statistics.median(sel), 1),
                                         "n": len(sel)}
    return out


def _split(acts: list[dict], samples_getter, today: date) -> dict:
    """匀速跑前/后 1/3 速度比（<1 = 后程快，负分段）。"""
    cutoff = today - timedelta(days=SAMPLE_DAYS)
    ratios = []
    for a in acts:
        if dates.ts_to_date(a["start_ts"]) < cutoff or not a.get("has_samples"):
            continue
        if (a.get("duration_s") or 0) < MIN_SPLIT_SEC:
            continue
        if len(_loads_segments(a)) > 1:
            continue
        samples = samples_getter(a["id"])
        rows = sorted([s for s in samples
                       if s.get("speed_mps") and s["speed_mps"] >= 1.5],
                      key=lambda s: s["t_offset_s"] or 0)
        if len(rows) < MIN_SPLIT_SAMPLES:
            continue
        n3 = len(rows) // 3
        pf = statistics.fmean(s["speed_mps"] for s in rows[:n3])
        pl = statistics.fmean(s["speed_mps"] for s in rows[-n3:])
        if pf > 0:
            ratios.append(pf / pl)
    if not ratios:
        return {"n": 0}
    med = statistics.median(ratios)
    style = ("后程快型（负分段）" if med < 0.95
             else "匀速型" if med <= 1.05 else "后程掉速型（正分段）")
    return {"n": len(ratios), "median_ratio": round(med, 3), "style": style}


def _recovery(acts: list[dict], health: list[dict], today: date) -> dict:
    """强度日 vs 轻松日次日 RHR / HRV 变化（E4 口径：恢复速度）。

    RHR 是可靠恢复指标；HRV 次日有反弹现象（方向与直觉相反），只作参考。
    """
    cutoff = today - timedelta(days=PACE_HR_DAYS)
    hard_days: set[date] = set()
    easy_days: set[date] = set()
    for a in acts:
        day = dates.ts_to_date(a["start_ts"])
        if day < cutoff:
            continue
        hr, dur = a.get("avg_hr"), a.get("duration_s") or 0
        if not hr or dur < HARD_DUR_MIN:
            continue
        if hr >= HARD_HR_MIN:
            hard_days.add(day)
        elif hr <= EASY_HR_MAX:
            easy_days.add(day)
    by_day = {h["date"]: h for h in health}

    def deltas(days: set[date], key: str) -> list[float]:
        vals = []
        for d in days:
            base = by_day.get(d.isoformat()) or {}
            nxt = by_day.get((d + timedelta(days=1)).isoformat()) or {}
            if base.get(key) and nxt.get(key):
                vals.append(nxt[key] - base[key])
        return vals

    out: dict = {}
    for key, name in (("resting_hr", "rhr_d1"), ("hrv_avg_ms", "hrv_d1")):
        hd, ed = deltas(hard_days, key), deltas(easy_days, key)
        out[name] = {"hard": ({"mean": round(statistics.fmean(hd), 1), "n": len(hd)}
                              if len(hd) >= MIN_BIN_N else {"mean": None, "n": len(hd)}),
                     "easy": ({"mean": round(statistics.fmean(ed), 1), "n": len(ed)}
                              if len(ed) >= MIN_BIN_N else {"mean": None, "n": len(ed)})}
    return out


def _distance_profile(history: list[dict]) -> dict:
    """各距离个人偏差 / 预测区间 / 最佳成绩；速度储备方向。

    bias 正 = 实际好于模型预测（该距离相对强），负 = 相对弱
    （personal.py 口径：bias = EWMA(actual − pred)）。
    """
    if not history:
        return {"available": False, "note": "无比赛历史（personal_history 为空）"}
    by_label: dict[str, dict] = {}
    for e in history:
        lbl, v = e.get("label"), e.get("actual_vdot")
        if not lbl or v is None:
            continue
        d = by_label.setdefault(lbl, {"n": 0, "best_vdot": 0.0, "last_day": ""})
        d["n"] += 1
        d["best_vdot"] = max(d["best_vdot"], v)
        d["last_day"] = max(d["last_day"], e.get("day") or "")
    for lbl, d in by_label.items():
        s = personal.distance_stats(history, lbl)
        iv = personal.interval(history, lbl)
        d["bias"] = s["bias"]
        d["rmse"] = s["rmse"]
        d["interval_half_vdot"] = iv["half_vdot"] if iv else None
    speed_reserve = None
    hm = by_label.get("半马") or by_label.get("全马")
    if "5K" in by_label and hm:
        d = round(by_label["5K"]["best_vdot"] - hm["best_vdot"], 1)
        style = "偏速度型" if d >= 1.5 else ("偏耐力型" if d <= -1.5 else "均衡型")
        speed_reserve = {"vdot_5k_minus_hm": d, "style": style}
    return {"available": True, "by_label": by_label,
            "speed_reserve": speed_reserve}


def _blocks(block_rows: list[dict], profile: dict, today: date) -> list[dict]:
    """训练响应区块观察：区块构成 → 同配速心率较上一区块的变化。

    小样本观察（跨区块比较需要两区块同配速档都 ≥MIN_BIN_N），只作趋势
    参考——「这个人对什么刺激有反应」的证据，不是结论。
    """
    max_hr = profile.get("max_hr")
    if not max_hr:
        max_hr = wa.estimate_max_hr(profile.get("birth_year"))
    if not max_hr:
        max_hr = 185.0  # 兜底（只影响区块内相对分类）
    rest_hr = profile.get("rest_hr") or 60
    lt1 = rest_hr + LT1_HRR * (max_hr - rest_hr)
    lt2 = rest_hr + LT2_HRR * (max_hr - rest_hr)

    def cat(hr: float) -> str:
        return "easy" if hr < lt1 else ("moderate" if hr < lt2 else "hard")

    start = today - timedelta(weeks=BLOCK_WEEKS * BLOCK_COUNT)
    blocks: list[dict] = []
    prev_bin_hr: dict[int, float] | None = None
    for i in range(BLOCK_COUNT):
        s = start + timedelta(weeks=BLOCK_WEEKS * i)
        e = s + timedelta(weeks=BLOCK_WEEKS) - timedelta(days=1)
        rows_b = [a for a in block_rows if s <= dates.ts_to_date(a["start_ts"]) <= e]
        b: dict = {"start": s.isoformat(), "end": e.isoformat(),
                   "n_runs": len(rows_b),
                   "km": round(sum(a.get("distance_m") or 0 for a in rows_b) / 1000, 1)}
        if len(rows_b) >= MIN_BLOCK_RUNS:
            counts: dict[str, int] = defaultdict(int)
            bin_hr: dict[int, list[float]] = defaultdict(list)
            for a in rows_b:
                counts[cat(a["avg_hr"])] += 1
                bin_hr[_pace_bin(a["avg_pace_s_km"])].append(a["avg_hr"])
            for k in CAT_NAMES:
                b[f"{k}_n"] = counts.get(k, 0)
            b["dominant"] = max(counts, key=counts.get) if counts else None
            means = {k: statistics.fmean(v) for k, v in bin_hr.items()
                     if len(v) >= MIN_BIN_N}
            if prev_bin_hr is not None:
                common = [k for k in means if k in prev_bin_hr]
                if common:
                    b["hr_delta"] = round(statistics.fmean(
                        means[k] - prev_bin_hr[k] for k in common), 1)
                    b["common_bins"] = len(common)
            prev_bin_hr = means
        blocks.append(b)
    return blocks


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def build(acts: list[dict], health_rows: list[dict],
          personal_history: list[dict], samples_getter,
          profile: dict | None, today: date) -> dict:
    """从历史数据重建个人响应模型（JSON 安全，持久化到 app_state）。"""
    profile = profile or {}
    rows = _valid_rows(acts, today - timedelta(days=PACE_HR_DAYS))
    block_rows = _valid_rows(acts, today - timedelta(weeks=BLOCK_WEEKS * BLOCK_COUNT))
    health = [h for h in health_rows
              if (h.get("date") or "") >= (today - timedelta(days=PACE_HR_DAYS)).isoformat()]
    pace_hr = _pace_hr(rows)
    return {
        "computed_at": dates.now_iso(),
        "window_days": PACE_HR_DAYS,
        "n_rows": len(rows),
        "pace_hr": pace_hr,
        "fatigue_hr": _fatigue_hr(rows, health, pace_hr),
        "drift": _drift(acts, samples_getter, today),
        "split": _split(acts, samples_getter, today),
        "recovery": _recovery(acts, health, today),
        "distance_profile": _distance_profile(personal_history or []),
        "blocks": _blocks(block_rows, profile, today),
    }


def expected_hr(model: dict, pace_s: float | None) -> dict | None:
    """该配速档的个人心率预期 {hr, n, std}；样本不足返回 None。"""
    if not pace_s:
        return None
    b = model["pace_hr"]["bins"].get(str(_pace_bin(pace_s)))
    if not b:
        return None
    return {"hr": b["mean"], "n": b["n"], "std": b["std"]}


def describe(model: dict) -> list[str]:
    """把模型渲染成给教练（prompt）与运动员（UI）读的中文行。

    每行带 n；样本不足的特征不出行，全部不足则给一条「数据不足」。
    """
    lines: list[str] = []
    ph = model.get("pace_hr") or {}
    top = sorted(ph.get("bins", {}).items(), key=lambda kv: -kv[1]["n"])[:3]
    for k, v in top:
        pace = int(k)
        m, s = divmod(pace, 60)
        lines.append(f"配速 {m}:{s:02d}/km 心率基线 {v['mean']:.0f}±{v['std']:.0f} bpm（n={v['n']}）")

    fh = model.get("fatigue_hr") or {}
    if fh.get("available"):
        base = fh["baseline"]
        lines.append(f"健康基线：RHR {base['rhr_median']:.0f} / HRV {base['hrv_median']:.0f} ms（n={base['n_health']} 天）")
        rh, rn = fh.get("rhr_high"), fh.get("rhr_normal")
        if rh and rh.get("mean_dev") is not None:
            ref = (f"，正常日 {rn['mean_dev']:+.1f}（n={rn['n']}）"
                   if rn and rn.get("mean_dev") is not None else "")
            lines.append(f"前夜 RHR 偏高（≥基线+{RHR_HIGH_DELTA}）：次日同配速心率平均 "
                         f"{rh['mean_dev']:+.1f} bpm（n={rh['n']}{ref}）")
        hl, hn = fh.get("hrv_low"), fh.get("hrv_normal")
        if hl and hl.get("mean_dev") is not None:
            ref = (f"，正常日 {hn['mean_dev']:+.1f}（n={hn['n']}）"
                   if hn and hn.get("mean_dev") is not None else "")
            lines.append(f"前夜 HRV 偏低（≤基线−{HRV_LOW_DELTA}）：次日同配速心率平均 "
                         f"{hl['mean_dev']:+.1f} bpm（n={hl['n']}{ref}）")
    elif fh.get("note"):
        lines.append(fh["note"])

    d = model.get("drift") or {}
    if d.get("n"):
        parts = [f"长跑心率漂移中位 {d['median_bpm']:+.0f} bpm（n={d['n']}）"]
        for k in sorted(d.get("by_duration", {}), key=lambda x: int(x.split("-")[0])):
            v = d["by_duration"][k]
            parts.append(f"{k} 分钟档 {v['median']:+.0f}（n={v['n']}）")
        lines.append("；".join(parts))

    sp = model.get("split") or {}
    if sp.get("n"):
        lines.append(f"前/后 1/3 速度比中位 {sp['median_ratio']:.2f}（n={sp['n']}）：{sp['style']}")

    rc = model.get("recovery") or {}
    r1 = rc.get("rhr_d1") or {}
    h, e = r1.get("hard"), r1.get("easy")
    if h and e and h.get("mean") is not None and e.get("mean") is not None:
        diff = h["mean"] - e["mean"]
        verdict = ("强度日后 RHR 次日回落偏慢（恢复偏慢）" if diff >= 2.0
                   else "强度日后 RHR 次日基本回落（恢复良好）")
        lines.append(f"强度日后次日 RHR {h['mean']:+.1f}（n={h['n']}）vs 轻松日 "
                     f"{e['mean']:+.1f}（n={e['n']}）：{verdict}")

    dp = model.get("distance_profile") or {}
    if dp.get("available"):
        ordered = [l for l in RACE_LABELS if l in dp["by_label"]]
        ordered += [l for l in sorted(dp["by_label"]) if l not in ordered]
        for lbl in ordered:
            d = dp["by_label"][lbl]
            bias = d.get("bias") or 0.0
            iv = (f"，预测区间 ±{d['interval_half_vdot']:.1f} VDOT"
                  if d.get("interval_half_vdot") else "")
            lines.append(f"{lbl}：最佳 VDOT {d['best_vdot']:.1f}（n={d['n']} 场，"
                         f"历史偏差 {bias:+.2f}{iv}）")
        sr = dp.get("speed_reserve")
        if sr:
            lines.append(f"速度储备：5K−半马最佳 VDOT 差 {sr['vdot_5k_minus_hm']:+.1f} → {sr['style']}")
    elif dp.get("note"):
        lines.append(dp["note"])

    solid = [b for b in (model.get("blocks") or [])
             if b.get("n_runs", 0) >= MIN_BLOCK_RUNS and b.get("hr_delta") is not None]
    for b in solid[-3:]:
        dom = CAT_NAMES.get(b.get("dominant") or "", b.get("dominant") or "混合")
        lines.append(f"{b['start'][5:]}–{b['end'][5:]} 区块：以{dom}为主"
                     f"（n={b['n_runs']}，{b['km']}km），同配速心率较上一区块 "
                     f"{b['hr_delta']:+.1f} bpm（{b['common_bins']} 个配速档，小样本仅供参考）")

    return lines or ["画像数据不足：有效配速-心率样本过少，暂无法学习个人反应规律。"]
