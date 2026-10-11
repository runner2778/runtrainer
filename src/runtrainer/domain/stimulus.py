"""训练刺激-恢复模型（AI 教练升级 · 规格八）。

教练按「训练刺激」理解课表，而不是按课名：有氧 / 阈值 / VO2max / 神经肌肉
/ 力量 / 肌耐力 / Durability / 恢复刺激，各自有不同的恢复时长，并影响未来
24h / 48h / 72h 训练质量——回答「今天训练很好，为什么三天后崩了」。

口径说明（诚实标注）：
- 恢复成本 AU 是**群体先验标尺**：1 AU ≈ 1 km 轻松跑所需恢复量。
  课型权重来自运动生理学常识（阈值 2.5× 轻松、间歇 3×、重复 3.5×），
  **未做个人校准**——它给教练当推理锚点（哪个课是本周疲劳的主要来源、
  下一个质量课前残留多少），不做硬裁决。
- 疲劳按指数衰减（半衰期 3 天）：24h 后残留 79%、48h 后 63%、72h 后 50%。
- 硬裁决（今天执行/降量/休息）由 today_decision 给出——那套规则是用
  真实数据（E2 疲劳信号、E4 恢复形态）验证过的。
- 周刺激平衡是「距离目标 × 刺激类型」的在场检查，不碰计划引擎的
  质量课上限（guardrails 已管上限，这里只管缺口）。
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta

# ---- 课型 → 刺激向量与恢复成本（群体先验）----
# cost_per_km: AU/km；cost_per_session: AU/次（无里程的课）。
# vector 值 = 该课每 km 贡献的该刺激强度（相对单位，只用于周平衡比例）。
STIMULUS: dict[str, dict] = {
    "RECOVERY": {"vector": {"recovery": 1.0}, "cost_per_km": 0.6,
                 "label": "恢复刺激"},
    "E": {"vector": {"aerobic": 1.0}, "cost_per_km": 1.0,
          "label": "有氧基础"},
    "M": {"vector": {"aerobic": 0.6, "threshold": 0.4}, "cost_per_km": 2.0,
          "label": "有氧耐力（马拉松配速）"},
    "T1": {"vector": {"aerobic": 0.4, "threshold": 0.6}, "cost_per_km": 2.2,
           "label": "巡航阈值"},
    "T": {"vector": {"threshold": 1.0}, "cost_per_km": 2.5, "label": "阈值"},
    "I": {"vector": {"vo2max": 1.0}, "cost_per_km": 3.0, "label": "最大摄氧量"},
    "R": {"vector": {"neuromuscular": 1.0}, "cost_per_km": 3.5,
          "label": "神经肌肉 / 速度"},
    "TUNEUP": {"vector": {"threshold": 0.5, "vo2max": 0.5}, "cost_per_km": 2.0,
               "label": "赛前调校"},
    "RACE": {"vector": {"aerobic": 0.5, "threshold": 0.5}, "cost_per_km": 4.0,
             "label": "比赛"},
    "CROSS": {"vector": {"cross": 1.0}, "cost_per_km": 0.5, "label": "交叉训练"},
    "STRENGTH": {"vector": {"strength": 1.0}, "cost_per_session": 8.0,
                 "label": "力量"},
}

HALF_LIFE_DAYS = 3.0                       # 疲劳指数衰减半衰期
QUALITY_RESIDUAL_LIMIT = 25.0              # 质量课前残留疲劳上限（AU）
                                           # ≈ 前一天刚跑完 10km 阈值的残留（25×0.79）
QUALITY_KINDS = ("T", "T1", "I", "R", "M", "TUNEUP", "RACE")
STAMINA_KM = 15.0                          # ≥15km 计入长距离耐力（Durability）刺激
STAMINA_MIN = 90                           # ≥90 分钟计入长距离耐力刺激
FORECAST_DAYS = 7                          # 未来质量课干扰检查窗口

# 周刺激平衡：各目标距离的缺口检查（在场检查，不碰上限）
GOAL_NEEDS: dict[str, list[tuple[str, str]]] = {
    "5K": [("speed", "神经肌肉 / 速度刺激（I 或 R 至少一次）")],
    "10K": [("threshold", "阈值刺激（T1/T/M 至少一次）"),
            ("speed", "速度 / VO2max 刺激（I 或 R 至少一次）")],
    "半马": [("threshold", "阈值刺激（T1/T 至少一次）"),
             ("stamina", "长距离耐力刺激（≥90 分钟或 ≥15km 至少一次）")],
    "全马": [("threshold", "阈值 / 马拉松配速刺激（T1/T/M 至少一次）"),
             ("stamina", "长距离耐力刺激（≥120 分钟或 ≥25km 至少一次）")],
}
FULL_MARATHON_STAMINA_KM = 25.0
FULL_MARATHON_STAMINA_MIN = 120


def _decay(days_ago: float) -> float:
    return 0.5 ** (days_ago / HALF_LIFE_DAYS)


def stimulus_of(w: dict) -> dict:
    """单课的刺激向量 + 恢复成本。

    w: 计划课行（kind / distance_km / duration_min / pace_zone / status）。
    未知课型按轻松跑处理（不臆造刺激）。
    """
    kind = (w.get("kind") or "E").upper()
    spec = STIMULUS.get(kind, STIMULUS["E"])
    km = float(w.get("distance_km") or 0)
    dur_min = float(w.get("duration_min") or 0)
    cost = spec.get("cost_per_km", 1.0) * km
    if "cost_per_session" in spec:
        cost += spec["cost_per_session"]
    vector = {k: v * km for k, v in spec["vector"].items()}
    stamina_min = min(dur_min, km * 6) if (km >= STAMINA_KM or dur_min >= STAMINA_MIN) else 0.0
    return {"kind": kind, "label": spec["label"], "vector": vector,
            "cost_au": round(cost, 1), "stamina_min": round(stamina_min, 0),
            "distance_km": km, "duration_min": dur_min,
            "recovery_days": recovery_days(cost),
            "quality": kind in QUALITY_KINDS}


def recovery_days(cost_au: float) -> str:
    """恢复成本的粗略天数映射（展示口径：×0.5 半衰期衰减后的感觉）。"""
    if cost_au < 10:
        return "约 1 天内"
    if cost_au < 20:
        return "约 1–2 天"
    if cost_au < 35:
        return "约 2–3 天"
    return "约 3–4 天"


def _day_of(w: dict) -> date:
    return date.fromisoformat(w["date"][:10])


def acute_fatigue(workouts: list[dict], today: date) -> dict:
    """截至今天（含）的残留疲劳：Σ 成本 × 指数衰减。

    只累计今天及以前的课（实际已发生的刺激），不含未来计划。
    """
    contribs = []
    for w in workouts:
        d = _day_of(w)
        if d > today:
            continue
        days_ago = max(0, (today - d).days)
        cost = stimulus_of(w)["cost_au"]
        if cost > 0:
            contribs.append({"date": d.isoformat(), "kind": w.get("kind"),
                             "cost_au": cost,
                             "residual": round(cost * _decay(days_ago), 1),
                             "days_ago": days_ago})
    contribs.sort(key=lambda c: -c["residual"])
    residual = round(sum(c["residual"] for c in contribs), 1)
    return {"residual_au": residual, "n": len(contribs),
            "top": contribs[:3]}


def fatigue_forecast(workouts: list[dict], today: date,
                     days: int = FORECAST_DAYS) -> list[dict]:
    """未来 days 天每天开训前的残留疲劳（含过去残留 + 当天之前的新课）。"""
    out = []
    for i in range(1, days + 1):
        d = today + timedelta(days=i)
        contribs = [stimulus_of(w)["cost_au"] * _decay((d - _day_of(w)).days)
                    for w in workouts if _day_of(w) < d]
        out.append({"date": d.isoformat(),
                    "residual_au": round(sum(contribs), 1)})
    return out


def quality_baseline(workouts: list[dict], today: date,
                     lookback_days: int = 90) -> dict | None:
    """个人校准：过去质量课「开训前的残留疲劳」分布（中位数 / P75）。

    群体上限（25 AU）不能照搬到每个人——课表密度（如双阈值日、每日双练）
    会让正常质量课的课前残留远高于它，照搬会把整张课表都标成「挤得太近」。
    用这个人自己过去质量课的课前残留校准；样本不足（<3 节）返回 None，
    调用方回退群体上限。近似：窗口最老的质量课缺窗口之前的衰减贡献
    （10 天前的课只贡献 10%），误差可接受。
    """
    past_q = [w for w in workouts
              if _day_of(w) < today and stimulus_of(w)["quality"]]
    residuals = []
    for w in past_q:
        d = _day_of(w)
        if d < today - timedelta(days=lookback_days):
            continue
        residuals.append(sum(
            stimulus_of(x)["cost_au"] * _decay((d - _day_of(x)).days)
            for x in workouts if _day_of(x) < d))
    if len(residuals) < 3:
        return None
    rs = sorted(residuals)
    return {"n": len(residuals),
            "median_au": round(statistics.median(rs), 1),
            "p75_au": round(rs[int(len(rs) * 0.75)], 1)}


def quality_day_flags(workouts: list[dict], today: date,
                      race_date: date | None = None,
                      days: int = FORECAST_DAYS,
                      baseline: dict | None = None) -> list[dict]:
    """未来质量课的干扰检查：开训前残留 > 个人校准上限 → 提示「课前疲劳偏高」。

    上限 = max(群体下限 25, 个人过去质量课基线中位 × 1.35)——只有明显高于
    这个人自己习惯的课前疲劳时才提示。赛前 14 天（taper 期）内的质量课
    不提示——减量期本来就在主动降负荷，按课表执行。
    """
    limit = QUALITY_RESIDUAL_LIMIT
    limit_desc = f"群体上限 {QUALITY_RESIDUAL_LIMIT:.0f}"
    if baseline:
        limit = max(QUALITY_RESIDUAL_LIMIT,
                    round(baseline["median_au"] * 1.35, 1))
        limit_desc = (f"个人校准上限 {limit:.0f}"
                      f"（过去质量课基线中位 {baseline['median_au']:.0f} AU，n={baseline['n']}）")
    horizon = today + timedelta(days=days)
    upcoming = [w for w in workouts
                if _day_of(w) > today and stimulus_of(w)["quality"]]
    upcoming.sort(key=_day_of)
    forecast = {f["date"]: f["residual_au"]
                for f in fatigue_forecast(workouts, today, days)}
    flags = []
    for w in upcoming:
        d = _day_of(w)
        if d > horizon:
            continue
        s = stimulus_of(w)
        residual = forecast.get(d.isoformat(), 0.0)
        if race_date and (race_date - d).days < 14:
            flag, note = "taper", "赛前 14 天减量期内，按课表执行"
        elif residual > limit:
            flag, note = ("fatigued",
                          f"课前残留疲劳 {residual:.0f} AU > {limit_desc}："
                          "前面几天的课挤得比这个人平时的节奏更紧，"
                          "考虑把课前一天的课减量或改轻松")
        else:
            flag, note = "ok", f"课前残留 {residual:.0f} AU（{limit_desc}）"
        flags.append({"date": d.isoformat(), "kind": s["kind"],
                      "label": s["label"], "residual_au": residual,
                      "flag": flag, "note": note})
    return flags


def week_balance(workouts: list[dict], goal_label: str | None = None,
                 week_start: date | None = None, today: date | None = None) -> dict:
    """本周刺激平衡：距离目标需要的刺激类型是否在场。

    只在场检查（计划引擎已管上限）；goal_label 未识别时用通用检查。
    """
    if week_start is None or today is None:
        return {"ok": True, "notes": [], "missing": []}
    week_end = min(today, week_start + timedelta(days=6))
    rows = [w for w in workouts if week_start <= _day_of(w) <= week_end]
    needs = GOAL_NEEDS.get(goal_label or "", []) if goal_label else []
    missing: list[str] = []
    if needs:
        has_threshold = any(stimulus_of(w)["kind"] in ("T", "T1", "M") for w in rows)
        has_speed = any(stimulus_of(w)["kind"] in ("I", "R") for w in rows)
        has_stamina = any((stimulus_of(w)["stamina_min"] > 0) for w in rows)
        has_full_stamina = any(
            (w.get("distance_km") or 0) >= FULL_MARATHON_STAMINA_KM
            or (w.get("duration_min") or 0) >= FULL_MARATHON_STAMINA_MIN
            for w in rows)
        for key, text in needs:
            ok = {"threshold": has_threshold, "speed": has_speed,
                  "stamina": (has_full_stamina if goal_label == "全马"
                              else has_stamina)}.get(key, False)
            if not ok:
                missing.append(text)
    else:
        has_quality = any(stimulus_of(w)["quality"] for w in rows)
        has_stamina = any(stimulus_of(w)["stamina_min"] > 0 for w in rows)
        if not has_quality:
            missing.append("本周无质量课（阈值 / 间歇 / 马拉松配速）")
        if not has_stamina:
            missing.append("本周无长距离耐力刺激（≥90 分钟或 ≥15km）")
    total_au = round(sum(stimulus_of(w)["cost_au"] for w in rows), 1)
    return {"ok": not missing, "notes": missing, "missing": missing,
            "week_au": total_au, "n": len(rows)}


def describe_stimulus(w: dict) -> str:
    """单课刺激的中文描述（prompt / UI 共用）。"""
    s = stimulus_of(w)
    parts = [f"{s['label']}刺激（{s['distance_km']:.1f}km · {s['duration_min']:.0f} 分钟）"]
    if s["stamina_min"] > 0:
        parts.append("含长距离耐力刺激")
    parts.append(f"恢复成本 {s['cost_au']:.0f} AU（{s['recovery_days']}）")
    return " · ".join(parts)
