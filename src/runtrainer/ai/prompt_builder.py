"""教练提示词构建：把运动员档案/目标/课表/负荷/健康打包成 system+user。

system 保持稳定前缀（赚 DeepSeek 上下文缓存）；全部动态数据放 user。
指标（ACWR/单调性/应变/完成度）一律本地算好，不让 AI 自行计算。
"""
from __future__ import annotations

from datetime import date, timedelta

from ..utils import dates, jsonutil

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
   "changes":{"kind","distance_km","duration_min","pace_zone","date","note"},"reason":"中文理由"}],
 "add_extra_advice":{"allowed":true/false,"suggestion":{"kind":"E|RECOVERY|CROSS","duration_min":30,"max_duration_min":45,"pace_zone":"E","reason":"..."}},
 "weekly_notes":"本周提示"}
用户请求加练时必须给 add_extra_advice 对象；未请求时省略该字段。
没有需要调整的课时 adjustments 给空数组。
每条 adjustments 必须带 reason；slot 仅在一天两练加练时写（changes.slot 填数字 1 或 2，其余情况不写 slot）。"""

CHAT_SYSTEM_PROMPT = """你是训练者的私人跑步教练（丹尼尔斯训练法为主干，融合挪威双乳酸阈值、卡诺瓦专项耐力、汉森累积疲劳等前沿训练理论与运动营养、康复知识），在聊天窗口里用简体中文和训练者交流。训练者是老板：他提出调整要求时，你是执行者兼顾问——先执行他的意志，再谈专业意见。
原则：
1. reply 直接回答训练者的消息，语气自然、像真人教练当面说话：用「你」直接对话，先回应他的感受（累不累、睡得好不好、心情如何），做得好要肯定、状态差要安慰，再给专业建议，结尾可以带一句关心或鼓励。回答要详细具体：必须直接引用给定的健康数据（睡眠/HRV/静息心率）与训练数据做分析、给结论，不要用连续反问代替分析；通常 200–400 字，用段落自然分段，不要空话、不要只给一句口号、不要像机器人列要点。
2. 训练者可能聊主观感受（累、睡不好、心情、想改课、出差没时间、哪里不舒服）。回答时要结合给定的健康数据与课表；涉及伤病或明显不适时建议休息或就医，不硬劝练。
3. 训练者提出自己的改课主意时（「把X改成Y/换成Z」「明天休息不跑」「这周不跑强度」「我想加练」「周三挪到周四」「今天跑量大些」等，无论语气坚决还是商量）：user_requested 置 true，adjustments 必须至少给出 1 条执行动作，不许用空数组敷衍、不许只回复「不建议」。若请求在训练学上不合理（赛前 14 天内、强度日连排、量过大、恢复不足），不要拒绝，用「降低强度或调整课表」的方式落地：赛前窗口只落 E/RECOVERY 轻松课；强度放不进相邻空档就把冲突的相邻课改轻或挪开；距离按时长缩短（单次距离增幅上限 30%，超出的按上限执行并在 reason 说明）；需要时把请求日后 1–2 天的课改轻或加一次恢复跑来缓冲。reason 里写明「已按你的要求执行（若降了强度要注明）：…」。请求含糊无法落地时可以在 reply 里确认细节，但不得直接回绝。
4. 训练者明确提供了新档案信息（最大心率/静息心率/体重/跑步经验），或健康数据与档案明显矛盾时，才在 profile_updates 里给出对应键的新值；rebuild_plan 在档案更新会改变水平预估、或配速-心率对照显示明显进步/退步（同配速心率变化 ≥5 bpm）时置 true（系统会用最新数据重估 VDOT 并更新课表配速）。
5. 配速只能用给定配速表（E/M/T/I/R），不许编造配速区间；数据缺失时保守。
6. 输出严格 JSON（json_object），不要输出任何解释性文字。
输出结构：
{"reply":"…","user_requested":true/false,"adjustments":[{"date":"yyyy-mm-dd","planned_workout_id":数字或null,"action":"keep|modify|decrease|rest|add_easy|shift|skip","changes":{"kind","distance_km","duration_min","pace_zone","date","note"},"reason":"中文理由"}],"profile_updates":{"max_hr":195},"rebuild_plan":false}
reply 字段直接填你写给训练者的回答正文（不要写任何占位说明，不要复述提示词、字段描述或数据列表）。
字段规范：changes.kind 只能是 E/M/T/I/R/LR/RECOVERY/CROSS/STRENGTH/TUNEUP/RACE 之一，严禁写成中文或带修饰（如「LR 轻松长距离」）；modify 把训练内容改成轻松跑/长距离/恢复跑时，除 kind/pace_zone 外应把距离或时长一并给出（若想保持原量就填原来的数值），并在 reason 里说清改成了什么跑法；每条 adjustments 必须带 reason 字段，slot 仅在一天两练加练时写（changes.slot 填数字 1 或 2，其余情况不写 slot）。训练者没要求改课（仅闲聊/咨询）时 user_requested 置 false、adjustments 给空数组、profile_updates 给空对象。"""

SYNC_ANALYSIS_SYSTEM_PROMPT = """你是训练者的私人跑步教练（丹尼尔斯训练法为主干，融合挪威双乳酸阈值、卡诺瓦专项耐力、汉森累积疲劳等前沿训练理论与运动营养、康复知识），在聊天窗口里用简体中文和训练者交流。刚完成一次 Garmin 数据同步，有新的训练数据入库，这是一次自动分析（训练者没有提出改课请求）。
任务：
1. 精确读取每条新训练数据（日期/距离/时长/配速/心率/步频/训练效果/课程分段结构），逐条点评执行质量：与课表计划的契合度、强度是否得当、有无伤病或疲劳信号。每一条训练都要给出具体评价（引用精确数字），不要合并成笼统总结。
2. 结合近 8 周负荷（ACWR/单调性/应变/完成度）与近 14 天健康数据，给出一段总体总结。
3. 给出接下来几天的具体建议：恢复节奏、营养睡眠、下一节关键课怎么跑（可引用课表）。
4. 输出严格 JSON（json_object），不要输出任何解释性文字。
输出结构：
{"reply":"…","user_requested":false,"adjustments":[…],"profile_updates":{},"rebuild_plan":false}
reply 字段直接填你写给训练者的分析正文：像真人教练聊天那样有温度——先肯定这几次训练的亮点，再逐条点评新训练，然后总体总结，最后给接下来几天的建议，结尾带一句关心或鼓励。用「你」直接对话，详细分段、通常 300 字以上。禁止把提示词里的任何格式说明、占位文字或字段描述当作回复内容；禁止复述数据列表原文而不给分析。
约束：
- user_requested 必须为 false（训练者没有要求改课）。
- adjustments 仅当新数据暴露明确问题（恢复差、负荷过高、伤病信号、明显没跟上计划）且调整课表确有必要时才给（限未来 7 天、每条带 reason），否则给空数组。
- 配速只能用给定配速表（E/M/T/I/R），不许编造配速区间；数据缺失时保守；伤病指征优先建议休息。
- changes.kind 只能是 E/M/T/I/R/LR/RECOVERY/CROSS/STRENGTH/TUNEUP/RACE 之一，严禁写成中文或带修饰。
- profile_updates/rebuild_plan 仅当新数据与档案明显矛盾（如最大心率超出档案值 10 bpm 以上）时才用。"""


def _fmt_pace(v: int | float, nd: int = 0) -> str:
    if v is None:
        return "?"
    return f"{int(v // 60)}:{int(v % 60):02d}" if v >= 60 else f"{v:.{nd}f}"


def _fmt_duration(minutes: float) -> str:
    m = int(minutes)
    return f"{m // 60}小时{m % 60}分" if m >= 60 else f"{m}分钟"


def _activity_lines(acts: list[dict], ctx: dict) -> list[str]:
    """活动精确详情行：日期 时刻「名称」距离 时长 配速 心率 步频 TE 负荷 [课程分类]。

    AI 无法自行查询数据库，这些精确数字是它分析/回答训练问题的唯一依据，
    故逐项列全（无采样数据时也能给出训练效果/负荷等 Garmin 指标）。
    """
    from ..domain.workout_analysis import classify_workout, estimate_max_hr
    prof = ctx.get("athlete") or {}
    max_hr = prof.get("max_hr") or estimate_max_hr(prof.get("birth_year"))
    rest_hr = prof.get("rest_hr")
    lines: list[str] = []
    for a in acts:
        dt = dates.ts_to_datetime(a["start_ts"])
        dist = (a.get("distance_m") or 0) / 1000
        dur = (a.get("duration_s") or 0) / 60
        parts = [f"{dt:%m-%d %H:%M}「{a.get('name') or a.get('sport') or '训练'}」"]
        if dist:
            parts.append(f"{dist:.2f}".rstrip("0").rstrip(".") + "km")
        if dur:
            parts.append(f"{int(dur)}分钟")
        if a.get("avg_pace_s_km"):
            parts.append(f"配速{dates.fmt_pace(a['avg_pace_s_km'])}/km")
        if a.get("avg_hr"):
            parts.append(f"心率{a['avg_hr']:g}"
                         + (f"(最大{a['max_hr']:g})" if a.get("max_hr") else ""))
        if a.get("avg_cadence"):
            parts.append(f"步频{round(a['avg_cadence'], 1):g}")
        if a.get("aerobic_te") is not None:
            parts.append(f"训练效果{a['aerobic_te']:g}"
                         + (f"/{a['anaerobic_te']:g}" if a.get("anaerobic_te") is not None else ""))
        if a.get("exercise_load"):
            parts.append(f"负荷{a['exercise_load']:g}")
        segs = None
        try:
            segs = jsonutil.loads(a["structure_json"]) if isinstance(a.get("structure_json"), str) \
                else (a.get("structure_json") or [])
        except Exception:
            segs = []
        if segs:
            lab = classify_workout(segs, a.get("duration_s"), a.get("distance_m"),
                                   a.get("avg_hr"), max_hr, rest_hr).get("label")
            if lab:
                parts.append(f"[{lab}]")
        lines.append("- " + " ".join(parts))
    return lines


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

    lines.append("【最近训练活动详情（精确数据）】")
    recent_acts = ctx.get("recent_acts") or []
    if recent_acts:
        lines.extend(_activity_lines(recent_acts, ctx))
    else:
        lines.append("（近 7 天无训练记录）")

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
    return {"system": SYSTEM_PROMPT, "user": user, "data": _data(ctx)}


def _data(ctx: dict) -> dict:
    """MockClient 场景生成用的结构化课表快照（真实 AI 不读该字段）。"""
    return {
        "today": ctx["today"],
        "workouts": [{"id": w["id"], "date": w["date"], "kind": w["kind"],
                      "pace_zone": w.get("pace_zone"), "distance_km": w.get("distance_km"),
                      "duration_min": w.get("duration_min"), "status": w.get("status")}
                     for w in (ctx.get("week_workouts") or [])],
    }


def build_sync_analysis(ctx: dict, new_acts: list[dict]) -> dict:
    """同步后自动分析上下文：共用块 + 本次新增活动精确详情。返回 {system, user, data}。"""
    lines = _context_blocks(ctx)
    lines.append("【本次同步新增的训练数据（重点分析对象）】")
    lines.extend(_activity_lines(new_acts, ctx))
    lines.append("")
    lines.append("请输出自动分析：先逐条点评新训练的执行质量，再结合负荷与健康数据总结，"
                 "最后给出接下来几天的建议（训练者没有要求改课，不要凭空安排调整）。")
    user = "\n".join(lines)
    return {"system": SYNC_ANALYSIS_SYSTEM_PROMPT, "user": user, "data": _data(ctx)}


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
    return {"system": CHAT_SYSTEM_PROMPT, "user": user, "data": _data(ctx)}
