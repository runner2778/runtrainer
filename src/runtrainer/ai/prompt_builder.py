"""教练提示词构建：把运动员档案/目标/课表/负荷/健康打包成 system+user。

system 保持稳定前缀（赚 DeepSeek 上下文缓存）；全部动态数据放 user。
指标（ACWR/单调性/应变/完成度）一律本地算好，不让 AI 自行计算。
"""
from __future__ import annotations

from datetime import date, timedelta

ZONE_NAMES = {"E": "轻松跑", "M": "马拉松配速", "T": "乳酸阈", "I": "间歇", "R": "重复跑"}

SYSTEM_PROMPT = """你是跑步教练，用简体中文工作。训练哲学：以丹尼尔斯训练法（Jack Daniels' Running Formula，VDOT/EMTIR/周期化）为主干，融合当代前沿运动科学——挪威双乳酸阈值训练法、卡诺瓦（Canova）专项训练法、汉森（Hanson）累积疲劳训练法，以及运动营养与康复知识。
原则：
1. 训练强度分布遵循 80/20（80% 轻松，20% 强度）；强度课之间至少隔 2 天。
2. 恢复是训练的一部分：睡眠差、HRV 低、静息心率升高时优先降强度或休息。
3. 伤病指征（静息心率连续 3 天升 >5 或 HRV 连续 3 天降）必须建议休息。
4. 赛前 14 天不引入新刺激，只允许 keep/rest/decrease 或轻松课。
5. 配速只能用给定配速表（E/M/T/I/R），不许编造其他配速区间。
6. 数据缺失时保守：不确定就不加量、不加练。
7. 加练只允许 E/RECOVERY/CROSS 类型、不超过 45 分钟、每周最多 2 次，赛前 3 天禁止加练。
8. 调整日期只能在今天起 7 天内；add_easy 落在没有课的空档日（训练者启用一天两练时，可落在当日第 2 练时段，但一天最多 2 练）。
9. 周跑量总变化不超过 ±10%；单次 modify 距离变化不超过 ±30%。
10. 输出严格 JSON（json_object），不要输出任何解释性文字。

理论应用（只体现在 reason/summary/weekly_notes 的文字与 modify 的内容上，不改变配速表和动作契约）：
- 双乳酸阈值日：连续阈值刺激日（如 5×6' 亚阈 + 3×10' 亚阈分上下午两练）是提升有氧阈值的高效手段；拆成两段时单段不超过 30 分钟、总阈值量不超过 60 分钟、两练间隔 ≥5 小时并补碳水补水。心率高/恢复差时提示改为单段或降为 M 区。
- 卡诺瓦专项耐力：赛前 6–8 周长距离中嵌入 M 区段落（如 16km 含 2×5km M 区），让身体适应比赛配速的神经肌肉模式；I/R 量在赛前逐步减少、专项耐力占比上升。
- 汉森累积疲劳：长距离课不超过周跑量的 25–30%；强度课后安排疲劳状态下的轻松跑，让身体在微疲劳中学习高效跑姿与脂肪供能；周跑量分摊到 6 天而非集中在长距离日。
- 力量训练（STRENGTH 课）：不改变跑步量，疲劳大时建议 keep/skip/rest，不要求取消后补练。
- 营养与康复：围绕训练补碳水（课前 1–4g/kg、课后 1–1.2g/kg+蛋白质 20–40g）、日常蛋白质 1.6–2.0g/kg、睡眠 7–9 小时；建议写在 weekly_notes/summary/key_signals 里，不产生调整动作。

输出结构：
{"summary":"一句话总结","readiness":"good|ok|low","key_signals":["判断依据"],
 "adjustments":[{"date":"yyyy-mm-dd","planned_workout_id":数字或null,"action":"keep|modify|decrease|rest|add_easy|shift|skip",
   "changes":{"kind","distance_km","duration_min","pace_zone","date","note","slot"},"reason":"中文理由"}],
 "add_extra_advice":{"allowed":true/false,"suggestion":{"kind":"E|RECOVERY|CROSS","duration_min":30,"max_duration_min":45,"pace_zone":"E","reason":"..."}},
 "weekly_notes":"本周提示"}
用户请求加练时必须给 add_extra_advice 对象；未请求时省略该字段。
没有需要调整的课时 adjustments 给空数组。"""

CHAT_SYSTEM_PROMPT = """你是训练者的私人跑步教练（丹尼尔斯训练法为主干，融合挪威双乳酸阈值、卡诺瓦专项耐力、汉森累积疲劳等前沿训练理论与运动营养、康复知识），在聊天窗口里用简体中文和训练者交流。训练者是老板：他提出调整要求时，你是执行者兼顾问——先执行他的意志，再谈专业意见。
原则：
1. reply 直接回答训练者的消息，语气自然、简洁、像教练聊天，不要列表轰炸。
2. 训练者可能聊主观感受（累、睡不好、心情、想改课、出差没时间、哪里不舒服）。回答时要结合给定的健康数据与课表；涉及伤病或明显不适时建议休息或就医，不硬劝练。
3. 训练者提出自己的改课主意时（「把X改成Y/换成Z」「明天休息不跑」「这周不跑强度」「我想加练」「周三挪到周四」「今天跑量大些」等，无论语气坚决还是商量）：user_requested 置 true，adjustments 必须至少给出 1 条执行动作，不许用空数组敷衍、不许只回复「不建议」。若请求在训练学上不合理（赛前 14 天内、强度日连排、量过大、恢复不足），不要拒绝，用「降低强度或调整课表」的方式落地：赛前窗口只落 E/RECOVERY 轻松课；强度放不进相邻空档就把冲突的相邻课改轻或挪开；距离按时长缩短（单次距离增幅上限 30%，超出的按上限执行并在 reason 说明）；需要时把请求日后 1–2 天的课改轻或加一次恢复跑来缓冲。reason 里写明「已按你的要求执行（若降了强度要注明）：…」。请求含糊无法落地时可以在 reply 里确认细节，但不得直接回绝。
4. 训练者明确提供了新档案信息（最大心率/静息心率/体重/跑步经验），或健康数据与档案明显矛盾时，才在 profile_updates 里给出对应键的新值；rebuild_plan 在档案更新会改变水平预估、或配速-心率对照显示明显进步/退步（同配速心率变化 ≥5 bpm）时置 true（系统会用最新数据重估 VDOT 并更新课表配速）。
5. 配速只能用给定配速表（E/M/T/I/R），不许编造配速区间；数据缺失时保守。
6. 输出严格 JSON（json_object），不要输出任何解释性文字。
输出结构：
{"reply":"回复文字","user_requested":true/false,"adjustments":[{"date":"yyyy-mm-dd","planned_workout_id":数字或null,"action":"keep|modify|decrease|rest|add_easy|shift|skip","changes":{"kind","distance_km","duration_min","pace_zone","date","note","slot"},"reason":"中文理由"}],"profile_updates":{"max_hr":195},"rebuild_plan":false}
训练者没要求改课（仅闲聊/咨询）时 user_requested 置 false、adjustments 给空数组、profile_updates 给空对象。"""


def _fmt_pace(v: int | float, nd: int = 0) -> str:
    if v is None:
        return "?"
    return f"{int(v // 60)}:{int(v % 60):02d}" if v >= 60 else f"{v:.{nd}f}"


def _fmt_duration(minutes: float) -> str:
    m = int(minutes)
    return f"{m // 60}小时{m % 60}分" if m >= 60 else f"{m}分钟"


def _context_blocks(ctx: dict) -> list[str]:
    """共用上下文块：档案/目标/计划/配速/今日/课表/负荷/健康（今日建议与聊天共用）。"""
    today = ctx["today"]
    lines: list[str] = []

    ath = ctx.get("athlete") or {}
    lines.append("【运动员档案】")
    if ath:
        lines.append(
            f"昵称 {ath.get('nickname') or '跑者'}，性别 {ath.get('sex') or '?'}，"
            f"出生年 {ath.get('birth_year') or '?'}，最大心率 {ath.get('max_hr') or '?'}，"
            f"静息心率 {ath.get('rest_hr') or '?'}，跑步经验 {ath.get('run_experience') or '?'}"
        )
    else:
        lines.append("（未填写档案）")

    goal = ctx.get("goal") or {}
    lines.append("【目标】")
    lines.append(
        f"距离 {goal.get('distance_m')}m，比赛日期 {goal.get('race_date')}，"
        f"目标成绩 {goal.get('target_seconds') and _fmt_duration(goal['target_seconds'] / 60) or '完赛'}，"
        f"VDOT {ctx.get('vdot')}"
    )
    if ctx.get("race_in_days") is not None:
        lines.append(f"距比赛 {ctx['race_in_days']} 天")

    plan = ctx.get("plan") or {}
    lines.append("【计划进度】")
    lines.append(
        f"第 {plan.get('current_week')} / {plan.get('total_weeks')} 周，"
        f"当前阶段 {plan.get('current_phase')}，本周目标 {plan.get('week_km')}km"
    )

    paces = ctx.get("paces") or {}
    lines.append("【配速表（s/km）】")
    if paces:
        if "E" in paces:
            lines.append(f"E 轻松跑 {_fmt_pace(paces['E']['slow_s_km'])}–{_fmt_pace(paces['E']['fast_s_km'])}")
        for z, name in ZONE_NAMES.items():
            if z != "E" and z in paces:
                v = paces[z]
                lines.append(f"{z} {name} {_fmt_pace(v)}")
    else:
        lines.append("（无配速表）")

    lines.append("【今日】")
    tws = ctx.get("today_workouts") or []
    if tws:
        for tw in tws:
            lines.append(
                f"{tw['date']}：{tw['kind']} {tw['title']}，{tw.get('distance_km')}km "
                f"{tw.get('duration_min')}分钟，配速区 {tw.get('pace_zone') or '-'}"
                + (f"（第 {tw.get('slot') or 1} 练）" if (tw.get('slot') or 1) == 2 else "")
            )
    else:
        lines.append(f"{today}：休息日（无课表）")

    lines.append("【本周及未来 7 天课表】")
    for w in ctx.get("week_workouts") or []:
        lines.append(
            f"- id={w['id']} {w['date']} {w['kind']} {w['title']} "
            f"{w.get('distance_km')}km {w.get('duration_min')}分钟 区{w.get('pace_zone') or '-'} "
            f"状态{w.get('status')}"
            + (f" 第{w.get('slot') or 1}练" if (w.get('slot') or 1) == 2 else "")
        )

    lines.append("【近 8 周训练（本地计算）】")
    rec = ctx.get("recent") or {}
    wk = rec.get("weekly_km") or []
    if wk:
        lines.append("周跑量 " + "，".join(f"{x:.0f}km" for x in wk))
    if rec.get("acwr"):
        lines.append(f"ACWR {rec['acwr']['ratio']}（急性 {rec['acwr']['acute_km']}km / 慢性 {rec['acwr']['chronic_km']}km）")
    if rec.get("monotony") is not None:
        lines.append(f"单调性 {rec['monotony']}，应变 {rec.get('strain')}")
    c7 = rec.get("compliance_7d") or {}
    if c7.get("ratio") is not None:
        lines.append(f"近 7 天完成度 {c7['done_km']}km / 计划 {c7['planned_km']}km（{c7['ratio'] * 100:.0f}%）")

    lines.append("【配速-心率对照（本地计算，判断进步/退步的依据）】")
    pht = ctx.get("pace_hr_trend") or {}
    if pht.get("best_drop") is not None:
        lines.append(pht.get("note") or
                     f"同配速（{pht.get('best_label')}/km）平均心率较最初时期变化 {pht['best_drop']:+.0f} bpm")
    phw = ctx.get("pace_hr_weekly") or []
    if phw:
        lines.append("近几周平均（最新在前）：" + "；".join(
            f"{w['week_start']} 配速 {_fmt_pace(w['avg_pace_s_km'])} 心率 {w['avg_hr']:.0f}（{w['runs']}次）"
            for w in phw))
    else:
        lines.append("（暂无有效配速-心率数据）")

    lines.append("【近 14 天健康数据】")
    health = ctx.get("health") or []
    if health:
        for h in health:
            parts = [f"{h['date']} 睡眠 {h.get('sleep_duration_h')}h（评分 {h.get('sleep_score')}）"]
            if h.get("hrv_avg_ms") is not None:
                parts.append(f"HRV {h['hrv_avg_ms']}ms（{h.get('hrv_status')}）")
            if h.get("resting_hr") is not None:
                parts.append(f"静息心率 {h['resting_hr']}")
            if h.get("stress_avg") is not None:
                parts.append(f"压力 {h['stress_avg']}")
            if h.get("body_battery_min") is not None:
                parts.append(f"身体电量 {h['body_battery_min']}")
            lines.append("；".join(parts))
    else:
        lines.append("（无健康数据：睡眠/HRV 等缺失，请保守建议）")
    return lines


def build(ctx: dict) -> dict:
    """ctx 见 coach_service._gather()。返回 {system, user, data}。"""
    today = ctx["today"]
    lines = _context_blocks(ctx)

    lines.append("【用户请求】")
    if ctx.get("extra_requested"):
        lines.append("用户今天想加练（增加一次训练）。请评估恢复数据："
                     "可以 → add_extra_advice.allowed=true 并在未来 7 天空档日给 add_easy；"
                     "不宜 → allowed=false 并说明。")
    else:
        lines.append("用户未请求加练。除非有明显必要，不要安排 add_easy。")
    if ctx.get("user_note"):
        lines.append(f"用户备注：{ctx['user_note']}")

    user = "\n".join(lines)
    data = {
        "today": today,
        "workouts": [{"id": w["id"], "date": w["date"], "kind": w["kind"],
                      "pace_zone": w.get("pace_zone"), "distance_km": w.get("distance_km"),
                      "duration_min": w.get("duration_min"), "status": w.get("status")}
                     for w in (ctx.get("week_workouts") or [])],
    }
    return {"system": SYSTEM_PROMPT, "user": user, "data": data}


def build_chat(ctx: dict, history: list[dict]) -> dict:
    """聊天上下文：共用块 + 水平预估 + 聊天记录 + 用户消息。返回 {system, user, data}。"""
    today = ctx["today"]
    lines = _context_blocks(ctx)

    ab = ctx.get("ability") or {}
    if ab.get("vdot"):
        lines.append("【当前水平预估】")
        lines.append(f"综合 VDOT {ab['vdot']}，等效成绩 " +
                     " / ".join(f"{k} {_fmt_duration(v / 60)}" for k, v in (ab.get("predictions") or {}).items()))
        ev = [f"{e.get('source')} VDOT {e.get('vdot')}" for e in (ab.get("evidence") or [])]
        if ev:
            lines.append("依据：" + "、".join(ev))

    if history:
        lines.append("【最近聊天记录（时间倒序）】")
        for m in reversed(history[-10:]):
            who = "训练者" if m["role"] == "user" else "教练"
            lines.append(f"{who}：{m['content'][:300]}")

    lines.append("【用户消息】")
    lines.append(ctx.get("user_note") or "")

    user = "\n".join(lines)
    data = {
        "today": today,
        "workouts": [{"id": w["id"], "date": w["date"], "kind": w["kind"],
                      "pace_zone": w.get("pace_zone"), "distance_km": w.get("distance_km"),
                      "duration_min": w.get("duration_min"), "status": w.get("status")}
                     for w in (ctx.get("week_workouts") or [])],
    }
    return {"system": CHAT_SYSTEM_PROMPT, "user": user, "data": data}
