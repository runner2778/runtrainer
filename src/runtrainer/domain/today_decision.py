"""今日决策引擎（AI 教练升级 · 规格三/四）。

本地确定性裁决：今天应该执行原计划 / 降低强度 / 减少总量 / 改恢复跑 /
还是直接休息。全部规则用真实数据验证过的信号（E2/E4 实验）或教练
共识阈值，每条结论都带「依据」字段——AI 教练可以解释这些倾向，但
与它相反的结论必须在 reason 里引用具体数据。

裁决框架（规格三原文）：「不要因为日历上今天安排了阈值训练，就无论
运动员今天是什么状态都强行执行。」

规则分层：
1. 硬性休息（伤病/过劳指征，直接 rest）：
   - RHR 连续 3 天上升且累计 >5 bpm（教练共识 + 现有 prompt 规则 3；
     幅度过滤后真实库仅 2% 天数触发，是唯一有把握的硬裁决信号）
2. 疲劳计分（累加 → 执行/注意/降量/恢复跑或休息）：
   - 今日 RHR ≥ 基线+5 → +2（E2：前夜 RHR 偏高时次日同配速心率 +3.6 bpm，
     是实测最强的疲劳信号）
   - 今日 HRV ≤ 基线−15 → +1（E2：−10 档已 +1.9 bpm）
   - HRV 连续 3 天下降且累计 ≥15 ms → +2（黄色信号：手表 HRV 噪声大，
     无幅度门槛时 15% 天数「连降」，没有硬裁决依据，按「今天降档」处理）
   - 睡眠 ≤4.5h → +1；≤5.5h → 仅提示（E2 未发现 6h vs 7h 的系统性
     心率效应【证据不足】，短睡罚分是常规谨慎，不是数据验证）
   - ACWR ≥1.5 → +2 / ≥1.3 → +1（仅质量课日计分；轻松日本来就是恢复）
   - 单调性 ≥ max(群体 2.0, 个人 P90) → +1（仅质量课日；群体阈值对
     高密度课表常年超限，个人校准时几乎不会误报）
   - 连续训练 ≥10 天 → +1（仅质量课日，疲劳累积）
   - 昨天实际跑了强度课 且 今天安排质量课 → +2（相邻强度课）
3. 豁免：赛前 14 天减量期，负荷类罚分（ACWR/单调性/连续天数）清零——
   减量期主动降负荷是设计内行为；健康/伤病指征仍然生效。
4. 今天是休息日（无课）→ 休息日本就该休，健康信号只作备注。

裁决映射：0 分执行 / 1 分执行（注意体感）/ 2 分降强度或减量 /
≥3 分改恢复跑或休息 / 硬性指征休息。
"""
from __future__ import annotations

from datetime import date, timedelta

from runtrainer.domain import stimulus

# ---- 阈值与罚分 ----
RHR_INJURY_RISE = 5.0        # RHR 3 天连升累计 >5 bpm → 硬性休息
HRV_TREND_DROP = 15.0        # HRV 3 天连降累计 ≥15 ms → +2（黄色信号）
                             # 真实库：无幅度门槛时 15% 天数「连降」，加门槛
                             # 仍 11%——手表 HRV 噪声太大，E2 只验证过单日
                             # HRV 偏低（+1.9 bpm），趋势信号没有硬裁决依据，
                             # 按「今天降档」处理，不触发硬性休息。
INJURY_TREND_DAYS = 3        # 伤病指征观察窗口
RHR_HIGH_DELTA = 5.0         # 今日 RHR ≥ 基线+5 → +2
HRV_LOW_DELTA = 15.0         # 今日 HRV ≤ 基线−15 → +1
SLEEP_HARD_H = 4.5           # 睡眠 ≤4.5h → +1
SLEEP_CAUTION_H = 5.5        # 睡眠 ≤5.5h → 仅提示
ACWR_HIGH = 1.5              # ACWR ≥1.5 → +2
ACWR_ELEVATED = 1.3          # ACWR ≥1.3 → +1
MONOTONY_HIGH = 2.0          # 群体单调性阈值（仅作下限）
                             # 真实库：这位运动员 93% 天数 ≥2.0（双课日
                             # 课表天然推高）——照搬群体值会把质量课全部
                             # 标「偏高」。个人校准时用 max(2.0, 个人 P90)，
                             # 见 decide() 里 recent["monotony_p90"]。
CONSECUTIVE_DAYS = 10        # 连续训练 ≥10 天 → +1
TAPER_DAYS = 14              # 赛前减量窗口
INJURY_PENALTY = 100         # 硬性指征的「罚分」（必然触发休息档）

VERDICT_LABELS = {
    "execute": "执行原计划",
    "execute_caution": "执行，注意体感",
    "reduce": "降低强度或减量",
    "easy_or_rest": "改恢复跑或休息",
    "rest": "休息（伤病/过劳指征）",
    "rest_day": "今天本就是休息日",
}


def _health_at(health_rows: list[dict], d: date) -> dict:
    return next((h for h in health_rows if h.get("date") == d.isoformat()), {})


def _baseline(health_rows: list[dict], baseline: dict | None) -> dict:
    """RHR/HRV 基线：优先运动员画像的基线，回退近 14 天中位数。"""
    if baseline and baseline.get("rhr") and baseline.get("hrv"):
        return baseline
    rhrs = [h["resting_hr"] for h in health_rows if h.get("resting_hr")]
    hrvs = [h["hrv_avg_ms"] for h in health_rows if h.get("hrv_avg_ms")]
    out = {}
    if len(rhrs) >= 5:
        out["rhr"] = sorted(rhrs)[len(rhrs) // 2]
    if len(hrvs) >= 5:
        out["hrv"] = sorted(hrvs)[len(hrvs) // 2]
    return out


def decide(today_workout: dict | None, health_today: dict | None,
           health_rows: list[dict], recent: dict, race_in_days: int | None,
           today: date, baseline: dict | None = None) -> dict:
    """今日裁决。

    today_workout: 今天的计划课行（无 = 休息日）。
    health_today:  今天的健康行（可能缺失）。
    health_rows:  近 14 天健康行（趋势与基线回退）。
    recent:       {"acwr", "monotony", "consecutive_days", "yesterday_hard",
                   "weekly_km"}——由服务层用 load_metrics / 活动序列算出。
    race_in_days: 距目标比赛天数（None = 无比赛 / 未知）。
    baseline:     运动员画像的 RHR/HRV 基线（可选，优先于回退）。
    """
    signals: list[dict] = []
    penalty = 0
    base = _baseline(health_rows, baseline)
    taper = race_in_days is not None and 0 <= race_in_days <= TAPER_DAYS
    quality_today = bool(today_workout) and stimulus.stimulus_of(
        today_workout)["quality"]

    def flag(name: str, evidence: str, p: int) -> None:
        nonlocal penalty
        signals.append({"name": name, "evidence": evidence, "penalty": p})
        penalty += p

    # ---- 硬性指征：RHR/HRV 三日趋势 ----
    days = [today - timedelta(days=i) for i in range(INJURY_TREND_DAYS + 1, 0, -1)]
    hrs = [_health_at(health_rows, d) for d in days]
    rhrs = [h.get("resting_hr") for h in hrs if h.get("resting_hr")]
    if len(rhrs) >= INJURY_TREND_DAYS and all(
            rhrs[i] < rhrs[i + 1] for i in range(len(rhrs) - 1)):
        rise = rhrs[-1] - rhrs[0]
        if rise > RHR_INJURY_RISE:
            flag("伤病指征（RHR 连续上升）",
                 f"RHR 连续 {len(rhrs)} 天上升，累计 +{rise:.0f} bpm "
                 f"（{rhrs[0]:.0f}→{rhrs[-1]:.0f}）", INJURY_PENALTY)
    hrvs = [h.get("hrv_avg_ms") for h in hrs if h.get("hrv_avg_ms")]
    if len(hrvs) >= INJURY_TREND_DAYS and all(
            hrvs[i] > hrvs[i + 1] for i in range(len(hrvs) - 1)):
        drop = hrvs[0] - hrvs[-1]
        if drop >= HRV_TREND_DROP:
            flag("HRV 连续下降（黄色信号）",
                 f"HRV 连续 {len(hrvs)} 天下降，累计 −{drop:.0f} ms "
                 f"（{hrvs[0]:.0f}→{hrvs[-1]:.0f}）。手表 HRV 噪声大，"
                 f"不作伤病裁决，建议今天降档观察", 2)

    # ---- 疲劳计分 ----
    ht = health_today or {}
    if base.get("rhr") and ht.get("resting_hr") \
            and ht["resting_hr"] >= base["rhr"] + RHR_HIGH_DELTA:
        flag("静息心率偏高",
             f"今日 RHR {ht['resting_hr']:.0f} ≥ 基线 {base['rhr']:.0f}+{RHR_HIGH_DELTA:.0f}"
             "（实测：前夜 RHR 偏高时次日同配速心率 +3.6 bpm）", 2)
    if base.get("hrv") and ht.get("hrv_avg_ms") \
            and ht["hrv_avg_ms"] <= base["hrv"] - HRV_LOW_DELTA:
        flag("HRV 偏低",
             f"今日 HRV {ht['hrv_avg_ms']:.0f} ≤ 基线 {base['hrv']:.0f}−{HRV_LOW_DELTA:.0f}"
             "（实测：HRV 偏低时次日同配速心率 +1.9 bpm）", 1)
    sleep_h = (ht.get("sleep_duration_s") or 0) / 3600
    if sleep_h and sleep_h <= SLEEP_HARD_H:
        flag("睡眠严重不足", f"昨夜睡眠 {sleep_h:.1f}h（≤{SLEEP_HARD_H}h）", 1)
    elif sleep_h and sleep_h <= SLEEP_CAUTION_H:
        flag("睡眠偏少", f"昨夜睡眠 {sleep_h:.1f}h（≤{SLEEP_CAUTION_H}h，仅提示）", 0)

    acwr = recent.get("acwr")
    if isinstance(acwr, dict):  # load_metrics.acwr 返回 {ratio, acute_km, chronic_km}
        acwr = acwr.get("ratio")
    if taper:
        signals.append({"name": "赛前减量期", "evidence":
                        f"距比赛 {race_in_days} 天，负荷类信号按课表执行"
                        "（健康/伤病指征仍生效）", "penalty": 0})
    if acwr is not None:
        if taper:
            pass
        elif quality_today and acwr >= ACWR_HIGH:
            flag("急慢性负荷比过高", f"ACWR {acwr:.2f} ≥ {ACWR_HIGH}", 2)
        elif quality_today and acwr >= ACWR_ELEVATED:
            flag("急慢性负荷比偏高", f"ACWR {acwr:.2f} ≥ {ACWR_ELEVATED}", 1)
        elif acwr >= ACWR_HIGH:
            signals.append({"name": "急慢性负荷比过高（今日为轻松课）",
                            "evidence": f"ACWR {acwr:.2f}，轻松课本身即恢复，"
                                        "注意不要加量", "penalty": 0})
    monotony = recent.get("monotony")
    if quality_today and monotony is not None and not taper:
        mono_hi = MONOTONY_HIGH
        if recent.get("monotony_p90"):
            mono_hi = max(MONOTONY_HIGH, recent["monotony_p90"])
        if monotony >= mono_hi:
            ev = (f"近 7 天单调性 {monotony:.2f} ≥ 个人 P90 {mono_hi:.2f}"
                  f"（群体阈值 {MONOTONY_HIGH} 对本人的课表密度常年超限，"
                  f"已按个人分布校准）"
                  if recent.get("monotony_p90")
                  else f"近 7 天单调性 {monotony:.2f} ≥ 群体阈值 {MONOTONY_HIGH}")
            flag("训练单调性偏高", ev + "（强度分布单一）", 1)
    consec = recent.get("consecutive_days") or 0
    if quality_today and consec >= CONSECUTIVE_DAYS and not taper:
        flag("连续训练天数偏长",
             f"已连续训练 {consec} 天（≥{CONSECUTIVE_DAYS}），疲劳有累积", 1)
    if quality_today and recent.get("yesterday_hard"):
        flag("相邻强度课", "昨天实际跑了强度课，今天又安排质量课（计划引擎"
             "通常不允许，实情以活动为准）", 2)

    if today_workout is None:
        verdict = "rest_day"
        note = ("今天没有安排训练。休息日本就该休——除非是赛前调校或你主动要求，"
                "不建议把昨天的漏训机械补到今天。")
        if penalty >= INJURY_PENALTY:
            verdict, note = "rest", "今天虽无课，但伤病/过劳指征已触发，"
            "注意观察，明天再评估是否恢复训练。"
    elif penalty >= INJURY_PENALTY:
        verdict = "rest"
        note = "伤病/过劳指征触发，今日不建议训练（包括轻松跑）。"
    elif penalty == 0:
        verdict = "execute"
        note = "健康与负荷信号全部正常，按课表执行。"
    elif penalty == 1:
        verdict = "execute_caution"
        note = "有轻微疲劳信号，按课表执行但注意体感；前 1–2km 明显吃力就降档。"
    elif penalty == 2:
        verdict = "reduce"
        note = ("疲劳信号明显：质量课降为阈值以下或减量约 30%；"
                "轻松课缩短 20–30% 或降为恢复跑。")
    else:
        verdict = "easy_or_rest"
        note = ("多项疲劳信号叠加：今天改恢复跑（≤30 分钟轻松）或直接休息。"
                "不要补偿昨天的欠账，先把状态找回来。")

    return {"date": today.isoformat(), "verdict": verdict,
            "verdict_label": VERDICT_LABELS[verdict],
            "score": min(penalty, INJURY_PENALTY),
            "quality_today": quality_today,
            "signals": signals, "note": note,
            "baseline": base}
