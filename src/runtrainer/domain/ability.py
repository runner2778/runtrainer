"""当前水平预估（各距离）：综合手表 VO2max、近几个月配速-心率趋势、
间歇/节奏跑能力、近一月长跑最快分段与近期比赛成绩，输出各距离等效成绩
与建议 VDOT。

设计参考手表/训练平台的能力模型：
- 比赛成绩 = 硬证据（权重随距今时间衰减：60 天内全额，180 天降到下限）
- 手表 VO2max = 长期能力底数
- 阈值配速（88% HRmax 对应的配速）来自近 90 天配速-心率加权回归（半衰期
  28 天），反映当下有氧能力趋势
- 间歇 work 段配速反映最大摄氧量上限（近期课加权主导）
- 近一月长跑滑窗切出的最快分段（Best Effort）= 当下能力证据
各分量样本均按距今半衰期加权（第十八批）：近期训练主导预估、旧数据兜底，
预估随每一次训练即时灵敏更新。
纯函数层：接收活动列表与档案读数，不碰 DB。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import evidence, personal, vdot as vd

# 标准比赛距离带 ± 容差
RACE_BANDS = ((5000, 0.10), (10000, 0.10), (21097, 0.05), (42195, 0.05))
DISTANCE_LABELS = {5000: "5K", 10000: "10K", 21097: "半马", 42195: "全马"}
RACE_NAME_HINTS = ("比赛", "race", "5k", "10k", "半马", "全马", "marathon", "test")
# 「全力/比赛强度」的平均心率下限（对个体最大心率）：比赛冲刺平均可达
# 90–95% HRmax；阈值巡航整场约 84–88%——用 88% 会把配速心率都很稳的
# 强度巡航课误判成比赛（曾把 10.4km 阈值课当 10K 比赛锚点压死水平预估）
RACE_HR_MIN_REF = 0.90
# 「对照当场峰值」比赛通道（第十九批）：真·全力测试跑（操场计时/测验，名称
# 常不带比赛字样）场均心率可达当场峰值的 94%+（5K 计时 173/182≈0.95、10K
# 计时 159/180≈0.88 都能过）。此通道必须叠加平均心率 ≥75% 档案/估算最大
# 心率的下限——轻松跑的心率漂移（127/135≈0.94 的 60 分钟慢跑）比值同样
# 好看，不设下限会把它当比赛锚点（实验阶段真实踩过这个坑：漂移假比赛把
# 半马预测从 56.2 VDOT 拖到 43.6）。阈值巡航 avg/当场峰值 0.84–0.93，与
# 0.94 线基本不重叠——原 88% 线把巡航误认比赛的教训不受影响。
OWN_RACE_MIN_REF = 0.94
OWN_RACE_EST_MAX_MIN = 0.75
# 心率-配速趋势样本要求
TREND_MIN_RUNS = 5
TREND_MIN_DURATION_S = 15 * 60
TREND_PACE_MAX_S_KM = 8 * 60  # 慢于 8:00/km 的散步不参与回归
TREND_PACE_MIN_S_KM = 270  # 快于 4:30/km 的样本剔除：多为间歇日（停表无 laps、
# 无法分段）或 GPS 漂移，整场配速被快段拉低而心率不高，不是稳定配速样本
TREND_MIN_HR = 100  # 跑中平均心率低于此值视为传感器异常/GPS 漂移失真样本
TREND_MIN_HR_SPAN = 20  # 样本心率跨度太窄的回归无外推意义
TREND_MAX_EXTRAP_BPM = 10  # 目标心率超出样本最大心率的外推上限；超出则弃用趋势
# 分量（E 区样本外推 20+ bpm 不可靠；跑过真 T 跑、样本覆盖 88% HRmax 时才生效）
# 间歇 work 段长度区间（米）
WORK_SEG_RANGE_M = (300.0, 2000.0)
SPRINT_SEG_MAX_M = 500.0  # ≤500m 的 work 段按冲刺（R）强度反算，否则按间歇（I）
# 间歇恢复时间变量：休息/快跑时长比参考值 0.6；休息越短 → 同样快段配速
# 代表更高水平，每偏离 1 个单位修正 20%（速度→VDOT 近似线性）
INTERVAL_REST_REF = 0.6
INTERVAL_REST_K = 0.20
# ---- 间歇强度类型识别（第十五批）：同一组快跑段按
# 心率 / 休息结构 / 段落长度判断落在哪个生理区间，各自按对应 %VDOT 反算
INTERVAL_VO2MAX_HR_MIN = 0.92   # 段落平均心率 ≥92% HRmax：已到 V·O2 区（铁证，优先）
INTERVAL_T_HR_WIN = (0.84, 0.92)  # 长段落在阈值心率窗口内 → 乳酸阈值/巡航
INTERVAL_THRESH_REST_MAX = 0.55  # 休息/快跑比 ≤0.55 → 短休连续刺激 → 乳酸阈值型
INTERVAL_THRESH_DIST_MIN = 1000.0  # 无可靠心率时 ≥1km 长段落按阈值巡航处理
# 节奏跑（连续匀速巡航，非间歇）识别：15–60min、平均心率 84–94% HRmax。
# 配速窗口放宽（3:20–8:20/km，不只收 4:30 内）——阈值课配速随水平浮动，
# 心率带本身已排除慢走/假慢样本；GPS 假快样本是「快配速+低心率」，同样进不来
CRUISE_MIN_DURATION_S = 15 * 60
CRUISE_MAX_DURATION_S = 60 * 60
CRUISE_PACE_WIN = (200.0, 500.0)
CRUISE_HR_MIN = 0.84   # LT2≈88% HRmax，阈值课整场平均约在此下沿之上
CRUISE_HR_MAX = 0.94   # 更高多为全力/比赛节奏，不做巡航样本
CRUISE_MIN_RUNS = 2
# 近一年 PB 参与预估：只取超出现有估计的差距的一部分（PB 是过去最好状态，
# 当前水平不能被一次巅峰成绩拉满），且随时间衰减（越久越软），封顶防失控
PB_MIN_GAP = 0.3
PB_WEIGHT = 0.35
PB_BOOST_CAP = 1.5
PB_FULL_RECENT_DAYS = 180        # 半年内 PB 全额计权
PB_DECAY_DAYS = 364              # 一年前衰减到权重下限
PB_DECAY_FLOOR = 0.35
# ---- 课表完成度参与预估（第十六批）：课表按 VDOT 生成了质量课却没执行，
# 说明「按计划训练」在现实没发生，估计应更保守；反之计划跑得动，配速区间
# 就有人撑。统计近 8 周计划内质量课（T/I/R/TUNEUP）的完成比例——
# 由服务层从 plan_repo 取行、本层只做纯计算。
QUALITY_KINDS = ("T", "T1", "I", "R", "TUNEUP")   # T1=双阈值日上段 LT1 巡航阈，同为质量课
QUALITY_WINDOW_DAYS = 56         # 近 8 周计划内质量课
QUALITY_MIN_TOTAL = 2            # 计划质量课 <2 堂 → 样本不足，不参与调整
QUALITY_K = 1.6                  # 完成率 → VDOT 线性系数（r=1→+0.48，r=0→−1.12）
QUALITY_ADJ_MAX = 0.6            # 全额完成只小幅加分（配速本就是按该水平生成的）
QUALITY_ADJ_MIN = -1.2           # 全缺勤大幅下调，防按「没练成的水平」生成课表
# ---- 专项区间强度课分量（第十七批）：近一个月的连续匀速高强度课
# （整场心率落在阈值带、非间歇）是「当下」配速-心率证据——比 90 天回归
# 平均更贴现状。心率是生理门控：平均心率过低（没在跑强度）/ 过高（已近
# 全力）都排除；间歇日快慢交替拉低整场均心率，整场配速不代表阈值，排除。
# 心率下沿 82%（真实用户阈值课整场均心率常落 82–85%，84% 会漏接；M 带
# 顶正是 82%，再低就开始放进 M/轻松跑了）；95% 上沿排除已近全力样本。
QT_HR_WIN = (0.82, 0.95)         # 阈值强度课整场平均心率带（%HRmax）
QT_PACE_WIN = (180.0, 500.0)     # 3:00–8:20/km（阈值配速随水平浮动，不硬收窄）
QT_DUR_WIN = (10 * 60, 75 * 60)  # 连续匀速 10–75 min
QT_WINDOW_DAYS = 35              # 近一个月（留几天松弛）
QT_MIN_RUNS = 3                  # 专项课 <3 堂 → 样本不足，不参与
QT_LIFT = 1.0                    # 上限放松后最高到「专项课中位 +1」（不逐课追高）
# 储备心率(HRR)配速分量
HRR_TARGET = 0.70        # 用 70% HRR 对应配速反推（有氧区代表强度）
HRR_MIN = 0.40           # 过低 HRR 样本（慢走/热身）剔除
HRR_MAX = 0.85           # 高于此属阈值区，非有氧稳定样本
HRR_MIN_SPAN = 0.10      # 样本 HRR 跨度太窄回归无意义
HRR_MAX_EXTRAP = 0.08    # 目标 70% HRR 超出样本最大 HRR 的外推上限
# %HRR ≈ %VO2max（Karvonen）；跑步速度的 %vVO2 略高于 %VO2max（经验线性换算）
HRR_TO_VVO2 = 0.8
HRR_TO_VVO2_OFFSET = 0.2
# 同配速心率时期趋势（进步/退步检测）
HR_TREND_WINDOW_DAYS = 180
HR_TREND_VDOT_PER_BPM = 0.35   # 同一配速下心率每降 1 bpm ≈ 0.35 VDOT（保守经验值）
HR_TREND_ADJ_MIN = -0.5        # 心率上升惩罚下限（天气/疲劳也可能抬心率，不重罚）
HR_TREND_ADJ_MAX = 1.5         # 进步奖励上限（趋势是软证据，保守加分）
# ---- 时间衰减权重（第十八批）：预估更灵敏捕捉近期训练、更灵敏更新 ----
# 各分量在窗口内不再等权聚合：样本按距今半衰期衰减（0.5^(age/hl)），
# 加权中位/加权 Theil-Sen 让近几周训练主导、旧数据只作兜底底数——进步
# 不被旧样本稀释、退步不被旧成绩拖住（真实库曾见 152 个旧有氧样本把
# HRR 分量钉在 4:50/km，而近期间歇已跑进 3:32/km）。
TREND_HALF_LIFE_DAYS = 28      # 阈值回归 / 巡航分量
INTERVAL_HALF_LIFE_DAYS = 30   # 间歇分量
QT_HALF_LIFE_DAYS = 14         # 专项强度课（35 天窗口内再偏最近两周）
# 近一月长跑最快分段（Best Effort）分量：滑窗切出的「当下」能力证据
EFFORT_WINDOW_DAYS = 30
EFFORT_HALF_LIFE_DAYS = 14
EFFORT_WEIGHT = 0.14
# 比赛时效：60 天内全额计权、上限 +2；此后线性衰减到 180 天（权重下限
# 0.12、上限放宽到 +3.5）——旧比赛不该把当下水平钉死（真实库曾见半马
# 53.5+2 把预估钉在 55.5，而巡航/间歇证据已达 56.5/55.0）
RACE_WEIGHT = 0.28
RACE_FULL_DAYS = 60
RACE_WEIGHT_FLOOR = 0.12
RACE_CAP_BASE = 2.0
RACE_CAP_GROW = 1.5
RACE_CAP_GROW_DAYS = 120

# ---- 「一键评估」权重档案（最近三周）----
# 与默认口径的取舍不同：默认权重问「当前整体水平」（阈值趋势/专项课为主体，
# 180 天窗口）；这里问「这三周练出来的速度能力」，按用户指定让间歇/速度能力
# 占大头。interval 与 efforts 是同一能力的两个来源——前者只在带 work/rest
# 分段的间歇课上有样本（真实库近三周仅 1 课），后者（滑窗最快 1/3/5km 分段）
# 每次跑都有；_weighted 按在场分量归一化，interval 缺席时其权重自然落到
# efforts 上，即「work 段优先、最快分段兜底」。
QUICK_WINDOW_DAYS = 21       # 三周：够看出一段训练营的效果，又不被旧状态拖平
QUICK_WEIGHTS = {
    "race": 0.30,        # 三周内跑了比赛 = 最硬的证据，与速度能力同量级
    "interval": 0.34,    # 间歇能力（work 段配速 + 休息/快跑比修正）
    "efforts": 0.22,     # 最快分段（速度能力的直接测量，也是间歇的兜底）
    "threshold": 0.16,   # 阈值/巡航配速
    "hrr": 0.12,         # 配速 ↔ 储备心率区间（Karvonen）
    "quality": 0.10,     # 专项强度课完成度
    "vo2max": 0.06,      # 手表读数：只在没有训练分量时兜底（有训练分量则不参与加权）
}


def _theil_sen(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Theil-Sen 稳健回归（两两斜率中位数，纯 Python）。

    真实手表数据混有 GPS 漂移（假快配速）与心率带接触不良（假低心率）
    样本，普通最小二乘会被离群点拉偏、外推阈值配速虚高；Theil-Sen 对
    最多 ~29% 离群点鲁棒。点不足返回 None。
    """
    n = len(xs)
    if n < 3:
        return None
    slopes: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            if dx != 0:
                slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return None
    slopes.sort()
    slope = slopes[len(slopes) // 2]
    intercepts = sorted(ys[i] - slope * xs[i] for i in range(n))
    return slope, intercepts[len(intercepts) // 2]


def _recency(age: float, half_life: float) -> float:
    """半衰期权重：age 天前的样本权重 0.5^(age/hl)（今天=1，hl 天前=0.5）。"""
    return 0.5 ** (max(age, 0.0) / half_life)


def _rec_age_days(rec: dict, as_of: date | None) -> float:
    """记录（date iso 串 | 数值时间戳 | start_ts）距 as_of 的天数；解析失败 0。"""
    day = None
    v = rec.get("date")
    if isinstance(v, str):
        try:
            day = date.fromisoformat(v[:10])
        except ValueError:
            day = None
    elif isinstance(v, (int, float)):
        day = _ts_to_day(v)
    if day is None and rec.get("start_ts"):
        day = _ts_to_day(rec["start_ts"])
    if day is None:
        return 0.0
    return float(max(((as_of or date.today()) - day).days, 0))


def _ts_to_day(ts) -> date | None:
    from ..utils import dates
    try:
        return dates.ts_to_date(ts)
    except Exception:
        return None


def _weighted_median(vals: list[float], weights: list[float]) -> float:
    """加权中位数：按值排序累加权重，首个累计权重 ≥ 总权重一半的值。

    等权时退化为普通中位数（偶数取靠前中位）；近期权重大时由近期样本主导。
    """
    if not vals:
        raise ValueError("empty vals")
    pairs = sorted(zip(vals, weights), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def _weighted_theil_sen(xs: list[float], ys: list[float],
                        weights: list[float]) -> tuple[float, float] | None:
    """加权 Theil-Sen：两两斜率按 min(w_i, w_j) 取加权中位（截距同理）。

    min() 配对权重让含任一新近样本的配对不被完全压低（近期信号可上浮），
    同时离群点仍被多数好对压住——GPS 漂移/心率异常样本的鲁棒性保持
    （近期离群点权重虽高，坏对总权重仍低于全体一半）。点不足 3 或无
    有效斜率返回 None。
    """
    n = len(xs)
    if n < 3:
        return None
    slopes: list[tuple[float, float]] = []
    intercepts: list[tuple[float, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            if dx != 0:
                w = min(weights[i], weights[j])
                slopes.append(((ys[j] - ys[i]) / dx, w))
                # 过 (x_i,y_i)(x_j,y_j) 直线的截距 = (y_i*x_j - y_j*x_i)/(x_j - x_i)
                intercepts.append(((ys[i] * xs[j] - ys[j] * xs[i]) / dx, w))
    if not slopes:
        return None
    slope = _weighted_median([s for s, _ in slopes], [w for _, w in slopes])
    intercept = _weighted_median([s for s, _ in intercepts],
                                 [w for _, w in intercepts])
    return slope, intercept


def _vdot_for_pace(pace_s_km: float, pct: float) -> float:
    """二分：找 vdot 使 pace_s_km(vdot, pct) == pace。pace_table 单调递减。"""
    lo, hi = 20.0, 100.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if vd.pace_s_km(mid, pct) > pace_s_km:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 1)


def estimate_max_hr(profile_max_hr, activities: list[dict]) -> float | None:
    """HRmax：档案设置优先；否则取近 90 天活动 max_hr 的 95 分位。"""
    if profile_max_hr:
        return float(profile_max_hr)
    vals = sorted(a["max_hr"] for a in activities if a.get("max_hr"))
    if not vals:
        return None
    idx = min(len(vals) - 1, int(len(vals) * 0.95))
    return float(vals[idx])


def _is_interval_day(structure) -> bool:
    """真间歇课判定：≥2 个快跑段。采样级识别后匀速跑是单个 continuous 段、
    长距离跑中的单次提速不会产生多快段；只有间歇/重复课才有多段 work。"""
    segs = structure or []
    return sum(1 for s in segs if s.get("type") == "work") >= 2


def threshold_pace_from_trend(activities: list[dict], max_hr: float,
                              as_of: date | None = None) -> dict | None:
    """近 90 天配速-心率加权回归 → 88% HRmax（阈值）对应配速。

    只采用稳定配速样本：间歇课的整场 avg_pace 被快段拉低而 avg_hr 不高，
    混入会让回归斜率失真、外推阈值配速虚高——间歇能力已由 interval_ability
    分量单独反映，此处排除；心率 <100 的失真样本一并剔除。样本按距今
    半衰期加权（加权 Theil-Sen）：近期训练主导回归，旧数据只兜底。
    """
    if not max_hr:
        return None
    pts = []
    for a in activities:
        age = _rec_age_days(a, as_of)
        if age > 90:
            continue
        if (not a.get("avg_pace_s_km") or not a.get("avg_hr")
                or a["avg_hr"] < TREND_MIN_HR
                or (a.get("duration_s") or 0) < TREND_MIN_DURATION_S
                or not (TREND_PACE_MIN_S_KM <= a["avg_pace_s_km"] <= TREND_PACE_MAX_S_KM)
                or _is_interval_day(a.get("structure"))):
            continue
        pts.append((a["avg_pace_s_km"], a["avg_hr"],
                    _recency(age, TREND_HALF_LIFE_DAYS)))
    if len(pts) < TREND_MIN_RUNS:
        return None
    hrs = [h for _, h, _ in pts]
    if max(hrs) - min(hrs) < TREND_MIN_HR_SPAN:
        return None
    reg = _weighted_theil_sen([p for p, _, _ in pts], hrs, [w for _, _, w in pts])
    if not reg:
        return None
    slope, intercept = reg
    # 配速越快（s/km 越小）心率越高 → 斜率应为负；异常斜率直接弃用
    if slope >= 0:
        return None
    target_hr = max_hr * 0.88
    if target_hr - max(hrs) > TREND_MAX_EXTRAP_BPM:
        return None  # 外推超出样本心率范围：E 区样本推不出阈值配速
    pace = (target_hr - intercept) / slope
    if pace <= 0 or pace > TREND_PACE_MAX_S_KM:
        return None
    return {"pace_s_km": round(pace, 1), "n_runs": len(pts),
            "vdot": _vdot_for_pace(pace, vd.T_PCT)}


def best_recent_race(activities: list[dict], max_hr: float | None = None) -> dict | None:
    """近 180 天内的最佳比赛成绩（标准距离带内，名称/心率/全力测试跑三判定）。

    max_hr：已知的个体最大心率（档案设置 > 活动样本 95 分位，由调用方算出）。
    判定心率用它对冲「单场跑自己的纪录」的偏差：阈值巡航的平均心率通常在
    个体最大心率的 84–88% 上下，而比赛/全力平均 ≥90%（RACE_HR_MIN_REF）——
    用单场 max_hr 比值（avg/max≈0.94+）或 88% 线会把配速心率都很稳的强度课
    误判成比赛（曾把 10.4km 阈值课当 10K 比赛锚点，把水平预估压到 51 封顶）。
    无 max_hr 时退回单场比值。
    第三通道（第十九批）「对照当场峰值 ≥94%」认真·全力测试跑（操场计时/
    测验，名称常不带比赛字样——真实库 18:31 的 5K 计时 173/182≈0.95 此前
    被 0.90 线拒掉，半马预测整体虚高 166s）：必须叠加平均心率 ≥75% max_hr
    下限并排除带休息段的间歇结构——轻松跑的心率漂移（127/135≈0.94 的慢跑）
    比值同样好看，不设下限会把它当比赛锚点。阈值巡航 avg/当场峰值 0.84–0.93
    过不了 0.94 线，原「巡航误认比赛」的教训不受影响。
    """
    best = None
    for a in activities:
        dist = a.get("distance_m")
        dur = a.get("duration_s")
        if not dist or not dur or dist < 3000 or dist > 43000:
            continue
        name = (a.get("name") or "").lower()
        avg_hr = a.get("avg_hr")
        if avg_hr and (max_hr or a.get("max_hr")):
            hr_ref = max_hr or a["max_hr"]
            hr_ok = avg_hr / hr_ref >= RACE_HR_MIN_REF
        else:
            hr_ok = False
        name_ok = any(h in name for h in RACE_NAME_HINTS)
        own_ok = False
        if max_hr and avg_hr and a.get("max_hr") and not name_ok:
            struct = a.get("structure") or []
            own_ok = (not any(s.get("type") == "rest" for s in struct)
                      and avg_hr / a["max_hr"] >= OWN_RACE_MIN_REF
                      and avg_hr / max_hr >= OWN_RACE_EST_MAX_MIN)
        if not (hr_ok or name_ok or own_ok):
            continue
        for std, tol in RACE_BANDS:
            if abs(dist - std) / std > tol:
                continue
            v = vd.estimate_vdot(dist, dur)
            if best is None or v > best["vdot"]:
                best = {"distance_m": int(std), "duration_s": round(dur),
                        "date": a.get("start_ts"), "name": a.get("name"),
                        "vdot": round(v, 1), "raw_distance_m": dist,
                        "avg_hr": a.get("avg_hr"), "max_hr": a.get("max_hr")}
    return best


def detect_races(activities: list[dict], est_max: float | None = None) -> list[dict]:
    """检测「真实比赛」活动（个人误差学习的地面真值口径，第十九批）。

    判据与回测工具一致——回测怎么量，线上就怎么学：
      - 距离在标准距离带内（含容差），3km–43km；
      - 排除带休息段的间歇结构；
      - 名称提示，或（平均心率 ≥75% 档案/估算最大心率 且 对照当场峰值
        ≥88%）——后者把轻松跑心率漂移冒充的「比赛」滤掉，同时留住操场
        计时/测验跑（5K 计时 173/182≈0.95）。
    返回按日期升序的 [{day(iso), label, actual_vdot, distance_m,
    duration_s, name}]。est_max 缺省时退回各场当场峰值判定。
    """
    from ..utils import dates
    out = []
    for a in sorted(activities, key=lambda x: x.get("start_ts") or 0):
        dist = a.get("distance_m")
        dur = a.get("duration_s")
        if not dist or not dur or dist < 3000 or dist > 43000:
            continue
        struct = a.get("structure") or []
        if (sum(1 for s in struct if s.get("type") == "work") >= 2
                and any(s.get("type") == "rest" for s in struct)):
            continue
        name = (a.get("name") or "").lower()
        avg_hr = a.get("avg_hr")
        own_max = a.get("max_hr")
        genuine = any(h in name for h in RACE_NAME_HINTS) or (
            avg_hr and est_max and avg_hr / est_max >= OWN_RACE_EST_MAX_MIN
            and own_max and avg_hr / own_max >= YEAR_WHOLE_OWN_MIN)
        if not genuine:
            continue
        for std, tol in RACE_BANDS:
            if abs(dist - std) / std <= tol:
                day = a.get("date")
                if not day and a.get("start_ts"):
                    day = dates.ts_to_date(a["start_ts"]).isoformat()
                out.append({
                    "day": day,
                    "label": DISTANCE_LABELS[std],
                    "actual_vdot": round(vd.estimate_vdot(dist, dur), 1),
                    "distance_m": dist, "duration_s": round(dur),
                    "name": a.get("name"), "id": a.get("id"),
                    # 比赛分级参照：对照估算最大心率的相对努力（见 evidence.grade_race）
                    "hr_ref": (round(avg_hr / est_max, 2)
                               if avg_hr and est_max else None),
                    # 主观用力（佳明 Connect feel/rpe 或手动补录）：分级时优先于心率
                    "feel": a.get("feel"), "rpe": a.get("rpe"),
                })
                break
    return out


# ---- 近一年各距离最佳成绩 + 训练保持度（第十四批）----
# 水平预估此前只看近 180 天（30 天卡/180 天向导）；用户要求把「近一年各项
# 距离最好成绩、训练保持度」也读进来：比赛/近似距离整场活动是硬证据，平日
# 长跑里滑窗切出的最快分段（Garmin Best Effort 思路）补足没跑过正式比赛
# 的距离；保持度用「周活跃率 + 近 4 周跑量 vs 全年周均」量化。
YEAR_BEST_DISTANCES = (("5K", 5000), ("10K", 10000), ("半马", 21097), ("全马", 42195))
YEAR_BEST_TOL = 0.06        # 整场活动距离与标准距离容差（≈近似全程的比赛）
# 整场候选的「对照当场峰值」通道（YEAR_WHOLE_OWN_MIN）：真·全力测试跑（操场
# 计时/测验，名称常不带比赛字样）场均心率可达当场记录峰值的 88%+（5K 计时
# 173/182≈0.95、10K 计时 159/180≈0.88）——仅对照档案最大心率 90% 线会把它们
# 全拒掉，近一年最佳退回「长跑最快段」投影、比真实成绩慢 10%+（真实库实证：
# 18:31 的 5K 计时被拒后显示 21:11 的分段投影）。阈值巡航的 avg/当场峰值与之
# 重叠（0.88–0.93）也能过此门——无碍：本通道只服务「近一年最佳成绩」展示表，
# 取等效 VDOT 最快者，巡航 VDOT≈49–50 压不过真全力跑（≥51）；而预估/证据
# 路径（best_recent_race 等）仍只看档案最大心率 90% 线，巡航误认比赛的教训
# 不受影响。
YEAR_WHOLE_OWN_MIN = 0.88
SEG_RANGE = (0.97, 1.12)    # 滑窗覆盖距离相对标准距离的下/上界
SEG_MIN_HR_RATIO = 0.82     # 分段平均心率 ≥82% 最大心率才认（防散步/轻松段）
YEAR_SAMPLE_RUN_CAP = 40    # 参与分段扫描的活动上限（防超大库拖慢聊天/看板）
CONSISTENCY_YEAR_DAYS = 364


def distance_bests(activities: list[dict], get_samples=None,
                   max_hr: float | None = None) -> list[dict]:
    """近一年（调用方给窗口内活动）各标准距离最佳成绩。

    - 整场近似比赛：距离在容差带内且（名称含比赛标记 | 平均心率 ≥90% 档案/
      估算最大心率 | 平均心率 ≥88% 本场记录峰值——真·全力测试跑常只有此条）且
      非间歇结构（≥2 组 work + 有 rest 的总时长含休息，不算整场成绩）→ 折算 VDOT；
    - 最长分段（Best Effort）：带样本的更长跑中，按时间滑窗找覆盖距离在
      [0.97, 1.12]×标准距离、最快的最短耗时窗口；窗口平均心率 ≥82% 最大心率
      才认（无心率样本的窗口不参与——配速可能被下坡/漂移虚高）。
    get_samples(activity_id) -> 按时间序的样本 dict 列表（t_offset_s/hr/speed_mps）。
    返回按距离升序的 [{distance, best_seconds(按最快分段配速折算标准距离), date,
    vdot, name, source: race|effort, pace_s_km, avg_hr}]；无依据的距离不出现。
    """
    best: dict[str, dict] = {}
    scanned = 0
    for a in activities:
        dist = a.get("distance_m") or 0
        dur = a.get("duration_s") or 0
        if not dist or not dur:
            continue
        name = (a.get("name") or "").lower()
        # 两个心率参照系：档案/估算个体最大心率（真比赛全力平均 ≥90% 线，
        # 与 best_recent_race 同款——防阈值巡航误认比赛）与本场记录峰值
        # （YEAR_WHOLE_OWN_MIN 通道——真·全力测试跑 vs 档案最大心率常在
        # 84–88%，只有对照当场峰值才够 88%+；见常量注释）。该放宽只出现在
        # 近一年最佳展示，不开放给预估的证据路径。
        avg_hr = a.get("avg_hr")
        if avg_hr and (max_hr or a.get("max_hr")):
            a_hr_ratio = avg_hr / (max_hr or a["max_hr"])
        else:
            a_hr_ratio = None
        act_ratio = (avg_hr / a["max_hr"]
                     if avg_hr and a.get("max_hr") else None)
        name_ok = any(h in name for h in RACE_NAME_HINTS)
        struct = a.get("structure") or []
        is_interval = (isinstance(struct, list)
                       and sum(1 for s in struct if s.get("type") == "work") >= 2
                       and any(s.get("type") == "rest" for s in struct))
        whole_ok = (name_ok
                    or (a_hr_ratio is not None and a_hr_ratio >= RACE_HR_MIN_REF)
                    or (act_ratio is not None and act_ratio >= YEAR_WHOLE_OWN_MIN)) \
            and not is_interval
        for label, std in YEAR_BEST_DISTANCES:
            if whole_ok and abs(dist - std) / std <= YEAR_BEST_TOL and dur > 0:
                v = vd.estimate_vdot(dist, dur)
                rec = {"distance": label, "best_seconds": round(dur),
                       "date": a.get("date"), "vdot": round(v, 1),
                       "name": a.get("name") or "", "source": "race",
                       "pace_s_km": round(dur / dist * 1000, 1),
                       "avg_hr": a.get("avg_hr")}
                _keep_best(best, label, rec)
        # 分段扫描：仅处理带样本、距离够长到能切出最小标准距离的跑；
        # 心率样本缺失的不切（配速可能虚高，宁缺毋滥）
        if not get_samples or scanned >= YEAR_SAMPLE_RUN_CAP:
            continue
        if not a.get("has_samples") or dist < YEAR_BEST_DISTANCES[0][1] * SEG_RANGE[0]:
            continue
        if a.get("id") is None:
            continue
        scanned += 1
        try:
            samples = get_samples(a["id"]) or []
        except Exception:
            continue
        seg_bests = _best_effort_windows(samples, max_hr)
        if not seg_bests:
            continue
        for label, (pace_s_m, hr_avg) in seg_bests.items():
            std_m = dict(YEAR_BEST_DISTANCES)[label]  # 外层循环结束后 std 已失效
            proj_s = pace_s_m * std_m  # 以最快分段配速折算整段标准距离
            rec = {"distance": label, "best_seconds": round(proj_s),
                   "date": a.get("date"),
                   "vdot": round(vd.estimate_vdot(std_m, proj_s), 1),
                   "name": f"{a.get('name') or '跑步'}·{label}最快段",
                   "source": "effort",
                   "pace_s_km": round(pace_s_m * 1000, 1),
                   "avg_hr": round(hr_avg) if hr_avg else None}
            _keep_best(best, label, rec)
    return [best[l] for l, _ in YEAR_BEST_DISTANCES if l in best]


def _keep_best(best: dict, label: str, rec: dict) -> None:
    """同距离多证据取等效 VDOT 最高者；VDOT 并列（1 位小数相同）取总用时更短者
    ——「近一年最佳」展示的是最快成绩，结果不随活动遍历顺序摇摆。"""
    cur = best.get(label)
    if cur is None:
        best[label] = rec
    elif rec["vdot"] > cur["vdot"]:
        best[label] = rec
    elif rec["vdot"] == cur["vdot"] and rec["best_seconds"] < cur["best_seconds"]:
        best[label] = rec


def _best_effort_windows(samples: list[dict], max_hr: float | None,
                         ) -> dict[str, tuple[float, float | None]]:
    """一次活动内，为每个标准距离滑窗求最快分段。

    返回 {label: (最快分段配速 s/m, 窗口平均心率 | None)}。窗口覆盖距离在
    [0.97, 1.12]×标准之间时按「每米用时」比较——直接比总耗时会让刚好
    压到区间下沿的短窗口假快。前缀和 O(n) 双指针：覆盖 <0.97×标准时右端
    前进，>1.12×标准时左端前进直到窗口重新落回区间或右端到头。
    """
    n = len(samples)
    if n < 2:
        return {}
    # 前缀：pd[k]/pt[k] = 前 k 个样本累计距离/时间；ph/pc 为累计心率与样本数
    pd = [0.0] * (n + 1)
    pt = [0.0] * (n + 1)
    ph = [0.0] * (n + 1)
    pc = [0] * (n + 1)
    t_prev = None
    for i, s in enumerate(samples):
        t = float(s.get("t_offset_s") or 0)
        spd = float(s.get("speed_mps") or 0)
        dt = (t - t_prev) if t_prev is not None else 0
        if dt < 0:  # 异常时间戳回退视为 0
            dt = 0
        pd[i + 1] = pd[i] + (max(spd, 0) * dt if t_prev is not None else 0)
        pt[i + 1] = t
        hr = s.get("hr")
        ph[i + 1] = ph[i] + (float(hr) if hr else 0)
        pc[i + 1] = pc[i] + (1 if hr else 0)
        t_prev = t
    out: dict[str, tuple[float, float, float | None]] = {}
    for label, std in YEAR_BEST_DISTANCES:
        lo, hi = std * SEG_RANGE[0], std * SEG_RANGE[1]
        j = 0
        for i in range(n + 1):
            if j <= i:
                j = i + 1
            while j <= n and pd[j] - pd[i] < lo:
                j += 1
            if j > n:
                break
            d = pd[j] - pd[i]
            if d > hi:
                continue  # 距离越长耗时未必更长，左端继续前进找更短覆盖
            t = pt[j] - pt[i]
            if t <= 0:
                continue
            hr_n = pc[j] - pc[i]
            hr_avg = (ph[j] - ph[i]) / hr_n if hr_n > 0 else None
            # 有最大心率可依时，无心率样本的窗口不参与（配速可能被下坡/漂移虚高）
            if max_hr and (hr_avg is None or hr_avg < max_hr * SEG_MIN_HR_RATIO):
                continue
            pace_s_m = t / d if d > 0 else 0  # 每米用时：公平跨窗口长度比较
            cur = out.get(label)
            if cur is None or pace_s_m < cur[0]:
                out[label] = (pace_s_m, hr_avg)
    return out


def training_consistency(activities: list[dict], today: date | None = None) -> dict:
    """近一年训练保持度：周活跃率 + 近 4 周跑量相对全年周均。

    activities 为近一年活动（含 date 或 start_ts）。返回
    {run_weeks, total_weeks, run_week_pct, recent_4w_avg_km, recent_vs_year_pct}；
    无任何跑步时 total_weeks=1、各值 0（避免除零）。
    """
    today = today or date.today()
    runs = []
    for a in activities:
        d = a.get("date") or a.get("start_ts")
        if not d:
            continue
        try:
            day = date.fromisoformat(d[:10]) if isinstance(d, str) else date.fromisoformat(d)
        except (ValueError, TypeError):
            continue
        km = (a.get("distance_m") or 0) / 1000
        if day > today or day < today - timedelta(days=CONSISTENCY_YEAR_DAYS):
            continue
        runs.append((day, km))
    if not runs:
        return {"run_weeks": 0, "total_weeks": 1, "run_week_pct": 0,
                "recent_4w_avg_km": 0, "recent_vs_year_pct": 0}
    first_day = min(day for day, _ in runs)
    week_span = (today - first_day).days // 7 + 1   # 覆盖周数（从首次跑步起）
    run_weeks = {((today - day).days // 7) for day, _ in runs}
    run_weeks = {i for i in run_weeks if 0 <= i < week_span}
    year_km = sum(km for _, km in runs)
    year_avg = year_km / week_span if week_span else 0
    recent_km = sum(km for day, km in runs if day >= today - timedelta(days=27))
    recent_avg = recent_km / 4
    return {
        "run_weeks": len(run_weeks),
        "total_weeks": week_span,
        "run_week_pct": round(len(run_weeks) / week_span * 100),
        "recent_4w_avg_km": round(recent_avg, 1),
        "recent_vs_year_pct": round(recent_avg / year_avg * 100) if year_avg > 0 else 0,
    }


def _interval_type(works: list[dict], rests: list[dict], max_hr: float | None) -> str:
    """按心率/休息结构/段落长度判定整课间歇的刺激类型。

    规则（顺序即优先级）：
    1. 段落平均心率 ≥92% HRmax → 最大摄氧量型（心率是生理金标准，最高优先；
       无氧冲刺段短、段均心率读不到这么高，不会误伤）；
    2. 休息/快跑比 ≤0.55 → 短休连续刺激 → 乳酸阈值型（典型 T 巡航间歇）；
    3. 段落中位距离 ≤500m → 无氧冲刺型（R）；
    4. 段均心率落在阈值窗口 84–92% HRmax、或 >=1km 长段落无心率依据 →
       乳酸阈值型（巡航间歇/长距离重复）；
    5. 其余 → 最大摄氧量型（600–1000m 经典 I 间歇）。
    """
    hrs = [w.get("avg_hr") for w in works if w.get("avg_hr")]
    hr_ratio = (sorted(hrs)[len(hrs) // 2] / max_hr) if hrs and max_hr else None
    work_t = sum(w.get("elapsed_s") or (w.get("distance_m") or 0)
                 * (w["pace_s_km"] or 300) / 1000 for w in works)
    rest_t = sum(r.get("elapsed_s") or 0 for r in rests)
    rest_ratio = rest_t / work_t if work_t > 0 and rests else None
    dists = sorted(w["distance_m"] or 0 for w in works)
    med_dist = dists[len(dists) // 2]
    if hr_ratio is not None and hr_ratio >= INTERVAL_VO2MAX_HR_MIN:
        return "vo2max"
    if rest_ratio is not None and rest_ratio <= INTERVAL_THRESH_REST_MAX:
        return "threshold"
    if med_dist <= SPRINT_SEG_MAX_M:
        return "speed"
    if med_dist >= INTERVAL_THRESH_DIST_MIN and (
            hr_ratio is None or INTERVAL_T_HR_WIN[0] <= hr_ratio < INTERVAL_T_HR_WIN[1]):
        return "threshold"
    return "vo2max"


_PCT_BY_INTERVAL_TYPE = {"threshold": vd.T_PCT, "vo2max": vd.I_PCT, "speed": vd.R_PCT}


def interval_ability(activities: list[dict], max_hr: float | None = None,
                     as_of: date | None = None) -> dict | None:
    """间歇能力（按刺激类型 + 恢复时间变量）：逐课先按心率/休息结构/段落
    长度识别类型（乳酸阈值/最大摄氧量/无氧冲刺），再以该类型对应 %VDOT
    反算单课 VDOT；休息越短、快段越快 → 水平越高（恢复比修正）。返回总体
    加权中位 vdot（按距今半衰期衰减——近期课主导，跨类型合并抗单课噪声）
    与各类型分项 types，供分量与展示用。"""
    workouts: list[dict] = []
    by_type: dict[str, list[dict]] = {"threshold": [], "vo2max": [], "speed": []}
    for a in activities:
        structure = a.get("structure") or []
        if isinstance(structure, str):
            continue
        works, rests = [], []
        for s in structure:
            if (s.get("type") == "work" and s.get("pace_s_km")
                    and WORK_SEG_RANGE_M[0] <= (s.get("distance_m") or 0) <= WORK_SEG_RANGE_M[1]):
                works.append(s)
            elif s.get("type") == "rest" and (s.get("elapsed_s") or 0) > 0:
                rests.append(s)
        if len(works) < 2:
            continue  # 单段 work 不算间歇课
        paces = sorted(w["pace_s_km"] for w in works)
        med_pace = paces[len(paces) // 2]
        dists = sorted(w["distance_m"] or 0 for w in works)
        med_dist = dists[len(dists) // 2]
        itype = _interval_type(works, rests, max_hr)
        v = _vdot_for_pace(med_pace, _PCT_BY_INTERVAL_TYPE[itype])
        work_t = sum(w.get("elapsed_s") or (w.get("distance_m") or 0)
                     * (w["pace_s_km"] or 300) / 1000 for w in works)
        rest_t = sum(r.get("elapsed_s") or 0 for r in rests)
        # 无休息段（自动暂停吞掉休息圈）时休息比未知，不做修正——按 0 修正
        # 会误把缺数据当成"零休息"，虚高整课水平
        ratio = rest_t / work_t if work_t > 0 and rests else None
        if ratio is not None:
            v *= 1 + INTERVAL_REST_K * (INTERVAL_REST_REF - ratio)
        rec = {"vdot": v, "ratio": ratio, "pace_s_km": med_pace,
               "n": len(works), "type": itype,
               "age": _rec_age_days(a, as_of)}
        workouts.append(rec)
        by_type[itype].append(rec)
    if not workouts:
        return None
    vw = [_recency(w["age"], INTERVAL_HALF_LIFE_DAYS) for w in workouts]
    med_v = _weighted_median([w["vdot"] for w in workouts], vw)
    ratios = [w["ratio"] for w in workouts if w["ratio"] is not None]
    med_ratio = sorted(ratios)[len(ratios) // 2] if ratios else None
    med_pace = _weighted_median([w["pace_s_km"] for w in workouts], vw)

    def _type_median(key: str) -> dict | None:
        recs = by_type[key]
        if not recs:
            return None
        tw = [_recency(r["age"], INTERVAL_HALF_LIFE_DAYS) for r in recs]
        return {"vdot": round(_weighted_median([r["vdot"] for r in recs], tw), 1),
                "pace_s_km": round(_weighted_median([r["pace_s_km"] for r in recs], tw), 1),
                "n_workouts": len(recs),
                "n_segments": sum(r["n"] for r in recs)}

    types = {k: _type_median(k) for k in ("threshold", "vo2max", "speed")}
    types = {k: v for k, v in types.items() if v}
    return {"pace_s_km": round(med_pace, 1),
            "n_segments": sum(w["n"] for w in workouts),
            "n_workouts": len(workouts),
            "rest_ratio": round(med_ratio, 2) if med_ratio is not None else None,
            "vdot": round(med_v, 1), "types": types}


def cruise_ability(activities: list[dict], max_hr: float,
                   as_of: date | None = None) -> dict | None:
    """节奏跑/巡航能力：连续（非间歇、非比赛）15–60min 匀速跑，平均心率
    84–94% HRmax → 阈值课特征。多课取加权中位配速（距今半衰期衰减）按
    T（88% VDOT）反算——作为「阈值配速回归」缺失时的替代分量（同一生理
    信号的另一种测法）。"""
    if not max_hr:
        return None
    pts: list[tuple[float, float]] = []
    for a in activities:
        if not a.get("avg_pace_s_km") or not a.get("avg_hr"):
            continue
        if _is_interval_day(a.get("structure")):
            continue
        dur = a.get("duration_s") or 0
        if not (CRUISE_MIN_DURATION_S <= dur <= CRUISE_MAX_DURATION_S):
            continue
        if not (CRUISE_PACE_WIN[0] <= a["avg_pace_s_km"] <= CRUISE_PACE_WIN[1]):
            continue
        hr_ratio = a["avg_hr"] / max_hr
        if not (CRUISE_HR_MIN <= hr_ratio <= CRUISE_HR_MAX):
            continue
        name = (a.get("name") or "").lower()
        if any(h in name for h in RACE_NAME_HINTS):
            continue  # 比赛/测试跑不进巡航样本
        w = _recency(_rec_age_days(a, as_of), TREND_HALF_LIFE_DAYS)
        if (a.get("distance_m") or 0) >= 3000 and hr_ratio >= 0.88:
            for std, tol in RACE_BANDS:
                if abs(a["distance_m"] - std) / std <= tol:
                    break  # 心率够高且距离落在标准比赛带 → 按比赛剔除
            else:
                pts.append((a["avg_pace_s_km"], w))
        else:
            pts.append((a["avg_pace_s_km"], w))
    if len(pts) < CRUISE_MIN_RUNS:
        return None
    med_pace = _weighted_median([p for p, _ in pts], [w for _, w in pts])
    return {"pace_s_km": round(med_pace, 1), "n_runs": len(pts),
            "vdot": _vdot_for_pace(med_pace, vd.T_PCT)}


def hrr_ability(activities: list[dict], max_hr: float, rest_hr: float,
                as_of: date | None = None) -> dict | None:
    """储备心率(HRR)对应配速：有氧样本的 配速~HRR 加权回归 → 70% HRR 对应配速。

    HRR 剔除了静息心率的个体差异与波动，比绝对心率更可比；同一 HRR 下
    配速更快 = 有氧能力更强。%HRR≈%VO2max（Karvonen），换算 %vVO2 后反算
    VDOT。同样排除含 work 段的间歇日（整场配速被快段拉低失真）；样本按
    距今半衰期加权（加权 Theil-Sen），近期有氧跑主导。
    """
    if not max_hr or not rest_hr or max_hr <= rest_hr:
        return None
    pts = []
    for a in activities:
        age = _rec_age_days(a, as_of)
        if age > 90:
            continue
        if (not a.get("avg_pace_s_km") or not a.get("avg_hr")
                or (a.get("duration_s") or 0) < TREND_MIN_DURATION_S
                or not (TREND_PACE_MIN_S_KM <= a["avg_pace_s_km"] <= TREND_PACE_MAX_S_KM)
                or _is_interval_day(a.get("structure"))):
            continue
        pts.append((a["avg_pace_s_km"],
                    (a["avg_hr"] - rest_hr) / (max_hr - rest_hr),
                    _recency(age, TREND_HALF_LIFE_DAYS)))
    if len(pts) < TREND_MIN_RUNS:
        return None
    hrrs = [h for _, h, _ in pts]
    if max(hrrs) - min(hrrs) < HRR_MIN_SPAN:
        return None
    # x=HRR, y=配速：HRR 越高配速越快（斜率负）；异常斜率弃用
    reg = _weighted_theil_sen(hrrs, [p for p, _, _ in pts], [w for _, _, w in pts])
    if not reg or reg[0] >= 0:
        return None
    slope, intercept = reg
    if HRR_TARGET - max(hrrs) > HRR_MAX_EXTRAP:
        return None  # 样本没跑到 70% HRR 附近，外推不可靠
    pace = slope * HRR_TARGET + intercept
    if pace <= 0 or pace > TREND_PACE_MAX_S_KM:
        return None
    pct_vvo2 = HRR_TO_VVO2 * HRR_TARGET + HRR_TO_VVO2_OFFSET
    return {"pace_s_km": round(pace, 1), "n_runs": len(pts),
            "hrr_pct": HRR_TARGET, "rest_hr": round(rest_hr, 1),
            "vdot": _vdot_for_pace(pace, pct_vvo2)}


def hr_trend_ability(activities: list[dict], as_of: date | None = None) -> dict | None:
    """同配速平均心率的时期变化 → 有氧能力进步/退步检测（VDOT 调整量）。

    用 pace_bin_hr 首尾两期代表档（跑量最大档）的心率差：同一配速下
    心率下降 = 有氧能力进步。趋势是软证据，调整量保守钳制
    [-0.5, +1.5] VDOT；首尾期无可比档或样本不足返回 None。
    """
    from .workout_analysis import pace_bin_hr
    end = as_of or date.today()
    start = end - timedelta(days=HR_TREND_WINDOW_DAYS)
    out = pace_bin_hr(activities, start, end)
    s = out.get("summary") or {}
    drop = s.get("best_drop")
    if drop is None or len(out.get("periods") or []) < 2:
        return None
    adj = max(HR_TREND_ADJ_MIN, min(HR_TREND_ADJ_MAX, -drop * HR_TREND_VDOT_PER_BPM))
    return {"drop_bpm": round(drop, 1), "pace_label": s.get("best_label"),
            "adj_vdot": round(adj, 2)}


def quality_workout_evidence(activities: list[dict], max_hr: float | None,
                             as_of: date | None = None) -> dict | None:
    """近一个月专项区间强度课的配速水平（独立分量，第十七批）。

    选择「稳定跑完了的」高强度课：整场平均心率在 82–95% HRmax（心率佐证
    确实在阈值带内）、配速 3:00–8:20/km、时长 10–75 min、非间歇日。单课
    按 T 强度反算 VDOT（保守：若实际以更高强度完成，等效阈值只会更快），
    取加权中位（距今半衰期 14 天——35 天窗口内再偏最近两周）抗个别失真课。
    返回 None 表示样本不足（<QT_MIN_RUNS）——此时该分量不参与，不给旧
    比赛上限抬线。
    """
    if not max_hr:
        return None
    lo_ts = None
    if as_of is not None:
        from ..utils import dates
        lo_ts = dates.date_to_ts(as_of - timedelta(days=QT_WINDOW_DAYS))
    votes: list[float] = []
    vweights: list[float] = []
    paces: list[float] = []
    pweights: list[float] = []
    hrs: list[float] = []
    for a in activities:
        if (not a.get("avg_pace_s_km") or not a.get("avg_hr")
                or _is_interval_day(a.get("structure"))):
            continue
        if lo_ts is not None and a.get("start_ts") and a["start_ts"] < lo_ts:
            continue
        if not (QT_DUR_WIN[0] <= (a.get("duration_s") or 0) <= QT_DUR_WIN[1]):
            continue
        if not (QT_PACE_WIN[0] <= a["avg_pace_s_km"] <= QT_PACE_WIN[1]):
            continue
        pct_hr = a["avg_hr"] / max_hr
        if not (QT_HR_WIN[0] <= pct_hr <= QT_HR_WIN[1]):
            continue
        w = _recency(_rec_age_days(a, as_of), QT_HALF_LIFE_DAYS)
        votes.append(_vdot_for_pace(a["avg_pace_s_km"], vd.T_PCT))
        vweights.append(w)
        paces.append(a["avg_pace_s_km"])
        pweights.append(w)
        hrs.append(a["avg_hr"])
    if len(votes) < QT_MIN_RUNS:
        return None
    return {"vdot": _weighted_median(votes, vweights), "n_runs": len(votes),
            "pace_s_km": _weighted_median(paces, pweights),
            "hr_lo": int(min(hrs)), "hr_hi": int(max(hrs))}


def _recent_efforts_evidence(year_bests: list[dict] | None,
                             as_of: date | None = None,
                             window_days: int | None = None) -> dict | None:
    """近一月长跑最快分段（Best Effort）分量（第十八批）。

    year_bests（distance_bests 输出）中 source=="effort" 且距今 ≤
    EFFORT_WINDOW_DAYS 的分段是「当下」能力证据——比旧比赛/手表读数更贴
    现状，滑窗心率门（≥82% 最大心率）已防散步/漂移段。取加权中位
    （半衰期 14 天）抗单次失真段；无近期分段返回 None。
    window_days 可覆盖默认窗口（一键评估用 21 天，与三周口径对齐）。
    """
    if not year_bests:
        return None
    span = window_days if window_days else EFFORT_WINDOW_DAYS
    recs = []
    for r in year_bests:
        if r.get("source") != "effort" or not r.get("vdot"):
            continue
        age = _rec_age_days(r, as_of)
        if age > span:
            continue
        recs.append((r, age))
    if not recs:
        return None
    v = _weighted_median([r["vdot"] for r, _ in recs],
                         [_recency(a, EFFORT_HALF_LIFE_DAYS) for _, a in recs])
    # 取与中位值最接近的记录做展示标签
    label = min(recs, key=lambda ra: abs(ra[0]["vdot"] - v))[0]
    return {"vdot": round(v, 1), "n_efforts": len(recs),
            "distance": label.get("distance"), "age": int(min(a for _, a in recs)),
            "best_seconds": label.get("best_seconds")}


def _weighted(components: list[dict]) -> float | None:
    comps = [c for c in components if c and c.get("vdot")]
    if not comps:
        return None
    total = sum(c["weight"] for c in comps)
    return round(sum(c["vdot"] * c["weight"] for c in comps) / total, 1)


def compute_ability(activities: list[dict], vo2max: float | None,
                    profile_max_hr=None, rest_hr: float | None = None,
                    as_of: date | None = None,
                    year_bests: list[dict] | None = None,
                    plan_exec: dict | None = None,
                    weight_profile: dict | None = None,
                    effort_window_days: int | None = None,
                    personal_history: list[dict] | None = None,
                    samples_fn=None) -> dict:
    """综合各数据源输出水平预估。

    activities: 近 180 天活动（含 avg_pace_s_km/avg_hr/max_hr/distance_m/
                duration_s/start_ts/name，可选 structure 分段）。
    rest_hr: 静息心率（HRR 分量必需）；缺省时该分量跳过。
    year_bests: distance_bests() 的近一年各距离最佳（含 vdot/date/distance/
                best_seconds/source）；显著快于当前估计时给保守加分（带时间衰减）。
    plan_exec: quality_execution() 输出（课表质量课 done/total/ratio）；
               代表「课程内容 + 完成情况」——训练分量（阈值趋势/HRR/间歇/
               最快分段/完成度）合计权重占大头，近期训练主导估计；该调整
               在趋势之后、PB 加分之前施加（缺勤多 → 下调，完成好 → 小幅上调）。
    第十八批：各分量样本按距今半衰期加权（阈值回归/HRR/巡航/间歇/专项课），
    比赛权重与上限容差随距今时间衰减（60 天内 +2，180 天放宽到 +3.5），
    year_bests 中近一月 fastest segments 作为独立分量参与——预估随每次
    训练即时灵敏更新，旧数据只兜底。
    weight_profile: 分量权重覆盖表（kind → weight），覆盖默认权重与「无比赛
                重排」；给 QUICK_WEIGHTS 即「一键评估」口径（间歇/速度能力占
                大头）。None 时行为与历史完全一致。缺失分量不报错——按在场
                分量归一化（见 _weighted）；且此口径下手表读数只作兜底，有
                训练分量在场时不参与加权（否则稀疏分量一缺席就被读数带跑）。
    effort_window_days: 「近期最快分段」分量的年龄上限，默认 EFFORT_WINDOW_DAYS；
                一键评估传 21，让该分量名副其实地只看最近三周。
    personal_history: 个人误差历史（domain.personal 记录格式；服务层持久化）。
                None = 不启用个人学习（回测基线、旧口径）。
                第十九批：启用后 (1) 手表 VO2max 只在无其它分量时兜底——
                回测实证读数 68 vs 比赛等效 ~54、单用它偏差 −637s，是默认
                加权里最差的单分量；(2) 分量权重按历史误差逆误差加权
                （weight_profile 在场时跳过——一键评估口径不混用主口径权重）；
                (3) 各距离等效成绩按个人历史偏差校准（EWMA 慢学习 + 样本
                收缩 + 封顶），并给出真实误差口径的预测区间。
                批33：启用后走证据管线 _compute_with_evidence——训练表现
                证据层（间歇/专项强度课/最快分段，每节课一份证据不重复计）、
                训练→比赛个人转换、比赛分级（次全力不参与学习与锚定）、
                证据密度驱动的预测区间。None（含一键评估 weight_profile
                口径）仍走旧加权链路，行为与历史一致。
    samples_fn: 样本点读取函数（activity_id → 样本列表）；证据层的
                最快分段滑窗需要。缺省时该证据缺失（旧口径行为不变）。
    返回 {"vdot", "predictions", "zones", "evidence", "max_hr", "as_of",
         "components", "prediction_intervals", "personal_races"}；
    无任何依据时 vdot=None。证据管线另附 "ev_fitness"/"race_grades"。
    """
    if personal_history is not None and not weight_profile:
        return _compute_with_evidence(
            activities, vo2max, profile_max_hr, rest_hr, as_of, year_bests,
            plan_exec, personal_history, samples_fn)
    max_hr = estimate_max_hr(profile_max_hr, activities)
    race = best_recent_race(activities, max_hr)
    threshold = threshold_pace_from_trend(activities, max_hr, as_of) if max_hr else None
    intervals = interval_ability(activities, max_hr, as_of)
    hrr = hrr_ability(activities, max_hr, rest_hr, as_of) if max_hr and rest_hr else None
    quality = quality_workout_evidence(activities, max_hr, as_of) if max_hr else None
    efforts = _recent_efforts_evidence(year_bests, as_of, effort_window_days)
    # 阈值分量三来源按可靠性递补：心率-配速回归（整场稳定样本，最全）→
    # 节奏/巡航跑中位配速（连续匀速课）→ 乳酸阈值型间歇（短休长段）。
    # 后者即便存在也仅作替代——混入会让单一阈值槽被多次加权。
    threshold_src = None
    t_typed = (intervals.get("types") or {}).get("threshold") if intervals else None
    if not threshold:
        cruise = cruise_ability(activities, max_hr, as_of) if max_hr else None
        if cruise:
            threshold, threshold_src = cruise, "cruise"
        elif t_typed:
            threshold = {"vdot": t_typed["vdot"], "pace_s_km": t_typed["pace_s_km"],
                         "n_runs": t_typed["n_workouts"]}
            threshold_src = "t_intervals"

    # 权重重排：近期训练数据（课程内容/配速/心率/完成情况）占大头。训练分量 =
    # 阈值趋势 + 专项区间强度课（第十七批新增，近一月阈值带稳定课，比 90 天
    # 回归平均更贴当下）+ HRR 配速 + 间歇能力 + 课表完成度；比赛与手表 VO2max
    # 退居硬底数。_weighted 按权重占比归一化——缺失分量自动放大剩余占比。
    evidence: list[dict] = []
    components: list[dict] = []
    race_age = _rec_age_days(race, as_of) if race else None
    if race:
        # 比赛权重随龄衰减（第十八批）：60 天内全额 0.28，180 天线性降到
        # 0.12——旧比赛仍有资格做证据，但不再与近期比赛等权
        race_w = RACE_WEIGHT if race_age <= RACE_FULL_DAYS else max(
            RACE_WEIGHT_FLOOR,
            RACE_WEIGHT - (RACE_WEIGHT - RACE_WEIGHT_FLOOR)
            * (race_age - RACE_FULL_DAYS) / (180 - RACE_FULL_DAYS))
        race_detail = f"近期最佳比赛 {race['distance_m']}m"
        if race_age > RACE_FULL_DAYS:
            race_detail += f"（{int(race_age)} 天前，权重按时间衰减）"
        components.append({"vdot": race["vdot"], "weight": round(race_w, 3),
                           "kind": "race"})
        evidence.append({
            "source": "recent_race",
            "vdot": race["vdot"],
            "detail": race_detail,
            "race": race,
        })
    if vo2max:
        components.append({"vdot": round(float(vo2max), 1), "weight": 0.16, "kind": "vo2max"})
        evidence.append({"source": "garmin_vo2max", "vdot": round(float(vo2max), 1),
                         "detail": "手表 VO2max 读数"})
    if threshold:
        components.append({"vdot": threshold["vdot"], "weight": 0.22, "kind": "threshold"})
        if threshold_src == "cruise":
            evidence.append({"source": "cruise_ability", "vdot": threshold["vdot"],
                             "detail": f"节奏/巡航跑中位配速 {_fmt_pace(threshold['pace_s_km'])}/km"
                                       f"（{threshold['n_runs']} 课，平均心率 84–94% HRmax，"
                                       f"近期样本加权）",
                             "pace_s_km": threshold["pace_s_km"]})
        elif threshold_src == "t_intervals":
            evidence.append({"source": "t_intervals", "vdot": threshold["vdot"],
                             "detail": f"乳酸阈值型间歇（短休长段）中位配速 "
                                       f"{_fmt_pace(threshold['pace_s_km'])}/km"
                                       f"（{threshold['n_runs']} 课，近期样本加权）",
                             "pace_s_km": threshold["pace_s_km"]})
        else:
            evidence.append({"source": "threshold_trend", "vdot": threshold["vdot"],
                             "detail": f"阈值配速 {_fmt_pace(threshold['pace_s_km'])}/km"
                                       f"（{threshold['n_runs']} 次心率-配速回归，"
                                       f"近期样本加权）",
                             "pace_s_km": threshold["pace_s_km"]})
    if hrr:
        components.append({"vdot": hrr["vdot"], "weight": 0.10, "kind": "hrr"})
        evidence.append({"source": "hrr_pace", "vdot": hrr["vdot"],
                         "detail": f"{int(hrr['hrr_pct'] * 100)}% HRR 对应配速 "
                                   f"{_fmt_pace(hrr['pace_s_km'])}/km"
                                   f"（{hrr['n_runs']} 次有氧样本加权，静息心率 {hrr['rest_hr']}）",
                         "pace_s_km": hrr["pace_s_km"]})
    if intervals:
        components.append({"vdot": intervals["vdot"], "weight": 0.12, "kind": "interval"})
        detail = (f"间歇 {_fmt_pace(intervals['pace_s_km'])}/km"
                  f"（{intervals['n_workouts']} 课 {intervals['n_segments']} 段，近期样本加权")
        if intervals.get("rest_ratio") is not None:
            detail += f"，休息/快跑比 {intervals['rest_ratio']}（越短水平越高）"
        types = intervals.get("types") or {}
        if len(types) > 1:
            name_map = {"threshold": "阈值", "vo2max": "摄氧", "speed": "冲刺"}
            parts = [f"{name_map[k]}型 {v['n_workouts']} 课" for k, v in types.items()]
            detail += "，" + "、".join(parts)
        evidence.append({"source": "interval_ability", "vdot": intervals["vdot"],
                         "detail": detail + "）",
                         "pace_s_km": intervals["pace_s_km"]})
    if quality:
        components.append({"vdot": quality["vdot"], "weight": 0.16, "kind": "quality"})
        evidence.append({
            "source": "quality_workouts", "vdot": quality["vdot"],
            "detail": f"近一月专项强度课 {quality['n_runs']} 堂中位配速 "
                      f"{_fmt_pace(quality['pace_s_km'])}/km"
                      f"（平均心率 {quality['hr_lo']}–{quality['hr_hi']} bpm，"
                      f"82–95% HRmax 阈值带内稳定完成，近期样本加权）",
        })
    if efforts:
        components.append({"vdot": efforts["vdot"], "weight": EFFORT_WEIGHT,
                           "kind": "efforts"})
        evidence.append({
            "source": "recent_efforts", "vdot": efforts["vdot"],
            "detail": f"近一月长跑最快分段 {efforts['distance']} "
                      f"{_fmt_time(efforts['best_seconds'])}"
                      f"（{efforts['age']} 天前）等效 VDOT {efforts['vdot']}",
        })
    # 缺比赛时重新分配权重：训练分量（阈值趋势/HRR/间歇/最快分段）占大头
    # 绝对主导，手表 VO2max 只作底数（无比赛在场时读数的「上限」属性最
    # 值得怀疑）
    if not race and components:
        weights = {"vo2max": 0.10, "threshold": 0.30, "quality": 0.20,
                   "hrr": 0.14, "interval": 0.16, "efforts": 0.15, "race": 0.40}
        for c in components:
            c["weight"] = weights[c["kind"]]
    # 手表 VO2max 只作「底数」（第十九批起默认口径也生效）：只要有其它
    # 分量在场就不参与加权。回测实证：真实库读数 68 vs 比赛等效 ~54，单独
    # 用它预测 16 场真实比赛偏差 −637s（系统性高估用时），是默认加权里
    # 最差的单分量；去掉它时序回测 MAE 6.11 → 4.02 VDOT。只剩读数时仍
    # 保留，新手也有个数。间歇/最快分段这类分量本就稀疏（真实库三周窗口
    # 只有 1 节课带 work/rest 分段且心率失真被剔除），一旦缺席，归一化
    # 会把大头让给最不可信的读数——实测 21 天窗口算出 VDOT 58.7，而同年
    # 比赛/PB 硬证据只有 ~54。
    if any(c["kind"] != "vo2max" for c in components):
        components = [c for c in components if c["kind"] != "vo2max"]
        evidence = [e for e in evidence if e["source"] != "garmin_vo2max"]
    if weight_profile:
        # 一键评估口径：整表覆盖（含 race），让「间歇/速度能力占大头」不被
        # 默认重排冲淡；profile 未列的 kind 保留原权重，缺失分量交给
        # _weighted 归一化——没有间歇课时分量权重自然落到最快分段上
        # （vo2max 兜底规则已在上方统一处理，这里只覆盖权重）
        for c in components:
            if c["kind"] in weight_profile:
                c["weight"] = weight_profile[c["kind"]]
    # 个人误差学习（第十九批）：分量权重按该运动员自己的历史比赛误差做
    # 逆误差加权——哪个分量在他身上系统性跑偏就自动降权（慢学习：样本
    # 不足时只是轻微调整）。一键评估口径跳过（三周窗口是另一种问题设定，
    # 不混用主口径的学习历史）。
    if personal_history and not weight_profile and components:
        reweighted = personal.reweight(components, personal_history)
        if reweighted != components:
            components = reweighted
            evidence.append({
                "source": "personal_weights",
                "detail": (f"分量权重按个人历史误差调整：近 {len(personal_history)} 场"
                           f"真实比赛里误差大的分量降权（逆误差加权）"),
            })
    vdot_val = _weighted(components)
    # 同配速心率趋势调整：心率下降 = 有氧能力进步（软证据，保守幅度）。
    # 放在加权之后、比赛上限钳制之前——比赛成绩仍是最终硬上限。
    trend = hr_trend_ability(activities, as_of)
    if trend and vdot_val:
        vdot_val = round(vdot_val + trend["adj_vdot"], 1)
        up_down = "下降" if trend["drop_bpm"] < 0 else "上升"
        evidence.append({
            "source": "hr_trend",
            "vdot": vdot_val,
            "detail": f"同配速（{trend['pace_label']}/km）平均心率较早期"
                      f"{up_down} {abs(trend['drop_bpm']):.0f} bpm → "
                      f"调整 {'+' if trend['adj_vdot'] >= 0 else ''}{trend['adj_vdot']} VDOT",
        })
    # 课表质量课完成度调整（完成情况参与预估）：按计划把质量课跑下来了，
    # 说明当前估计的配速区间训练端撑得住；大量缺勤 → 现有估计整体下调，
    # 否则会按「安排了但没跑」的虚拟水平继续生成更快的课。
    # 放在心率趋势之后、PB 加分之前（执行差时 PB 是过去式，不应覆盖执行证据）
    if plan_exec and vdot_val and plan_exec.get("total"):
        ratio = plan_exec.get("ratio")
        if ratio is None:
            ratio = plan_exec["done"] / plan_exec["total"]
        adj = max(QUALITY_ADJ_MIN, min(QUALITY_ADJ_MAX,
                                       (ratio - 0.7) * QUALITY_K))
        if abs(adj) >= 0.1:
            vdot_val = round(vdot_val + adj, 1)
            evidence.append({
                "source": "plan_execution",
                "vdot": vdot_val,
                "detail": f"近 {QUALITY_WINDOW_DAYS // 7} 周课表质量课完成 "
                          f"{plan_exec['done']}/{plan_exec['total']}"
                          f"（{int(round(ratio * 100))}%）→ 执行校验 "
                          f"{'+' if adj >= 0 else ''}{adj:.1f} VDOT",
            })
    # 近一年 PB 加分：PB 是「曾经跑出过」的证据——比当前估计快时按差距的
    # 一部分加分（0.35×），随距今时间衰减（180 天内全值 → 一年衰减到 35%），
    # 单次封顶 1.5 VDOT；与近期比赛同为一条记录（等效 VDOT 相同）时不重复计。
    if year_bests and vdot_val:
        pbs = [r for r in year_bests if r.get("vdot")]
        if pbs:
            pb = max(pbs, key=lambda r: r["vdot"])
            dup_race = bool(race and pb.get("source") == "race"
                            and abs(pb["vdot"] - race["vdot"]) < 0.1)
            gap = pb["vdot"] - vdot_val
            if not dup_race and gap > PB_MIN_GAP:
                age = _days_old(pb.get("date") or pb.get("start_ts"), as_of)
                recency = 1.0 if age <= PB_FULL_RECENT_DAYS else max(
                    PB_DECAY_FLOOR,
                    1.0 - (1.0 - PB_DECAY_FLOOR) * (age - PB_FULL_RECENT_DAYS)
                    / max(PB_DECAY_DAYS - PB_FULL_RECENT_DAYS, 1))
                boost = min(gap * PB_WEIGHT * recency, PB_BOOST_CAP)
                if boost >= 0.1:
                    vdot_val = round(vdot_val + boost, 1)
                    age_txt = f"（{age} 天前）" if age > 0 else ""
                    evidence.append({
                        "source": "year_best",
                        "vdot": vdot_val,
                        "detail": f"近一年最佳 {pb['distance']} "
                                  f"{_fmt_time(pb['best_seconds'])}{age_txt}等效 "
                                  f"VDOT {pb['vdot']} 高于当前估计 → 上调 "
                                  f"+{boost:.1f}（PB 加成，按 {int(recency * 100)}% 计权）",
                    })
    # 上限钳制（条件放松）：预估不高于「已跑出的最佳比赛成绩等效 VDOT +
    # 上限容差」，防手表 VO2max 读数偏高（如 63 vs 比赛等效 48.9）时把课表
    # 配速拉到无法完成的水平。容差本身随比赛年龄放宽（第十八批）：60 天内
    # +2.0，180 天线性放宽到 +3.5——旧比赛不再把当下水平钉死。另外近一月
    # 持续稳定完成专项区间强度课（≥QT_MIN_RUNS 堂，阈值带配速心率佐证）
    # 且训练证据中位高于旧上限时，按 max(旧上限, min(未封顶加权, 训练中位
    # + QT_LIFT)) 抬线，让训练进步不被过期比赛成绩钉死；偶发快课/单堂
    # 冲刺不给抬线。
    cap_note = None
    if race and vdot_val:
        cap_slack = RACE_CAP_BASE + RACE_CAP_GROW * max(
            0.0, race_age - RACE_FULL_DAYS) / RACE_CAP_GROW_DAYS
        race_cap = race["vdot"] + cap_slack
        uncapped = vdot_val
        cand = None
        if quality:
            t_cand = threshold["vdot"] if threshold else None
            cand = max(v for v in (quality["vdot"], t_cand) if v)
        if cand is not None and cand > race_cap:
            lifted = max(race_cap, min(vdot_val, cand + QT_LIFT))
            vdot_val = round(lifted, 1)
            if lifted > race_cap + 0.05:
                cap_note = ("专项区间强度课持续达成且配速中位高于旧比赛水平 "
                            f"（{race['vdot']:.0f} 等效 → 上限 {race_cap:.1f}）"
                            f"，按训练证据抬线至 {lifted:.1f}")
        else:
            vdot_val = round(min(vdot_val, race_cap), 1)
            if quality and vdot_val >= race_cap - 0.05:
                cap_note = ("仍以近期最佳比赛等效 VDOT 为硬上限"
                            f"（{race['vdot']:.0f} 等效，上限 {race_cap:.1f}")
                if cap_slack > RACE_CAP_BASE:
                    cap_note += "，旧比赛上限已按时间放宽"
                cap_note += "）；近一月专项课训练证据未越过该线"
    if cap_note:
        evidence.append({"source": "cap_check", "vdot": vdot_val,
                         "detail": "上限校验：" + cap_note})
    # 个人偏差校准（第十九批）：各距离等效成绩按该运动员自己的历史预测
    # 误差修正（EWMA 慢学习 + 样本收缩 + 封顶），并从同一历史给出预测区间
    # （真实误差口径，不是拍出来的 ±10 秒）。zones 不动——训练区带是生理
    # 模型输出，「比赛执行/距离转换的个人偏差」不该改变训练配速依据。
    predictions = vd.equivalent_times(vdot_val) if vdot_val else None
    intervals = None
    if predictions and personal_history:
        bias_by_label = {}
        for label in predictions:
            bias = personal.bias_applied(personal_history, label)
            if bias:
                bias_by_label[label] = bias
                adj_vdot = round(vdot_val + bias, 1)
                predictions[label] = vd.predict_time(vd.DISTANCES[label], adj_vdot)
        if bias_by_label:
            evidence.append({
                "source": "personal_bias",
                "vdot": vdot_val,
                "detail": ("个人偏差校准："
                           + "、".join(f"{lab} {b:+.1f} VDOT"
                                       for lab, b in bias_by_label.items())
                           + f"（按近 {len(personal_history)} 场真实比赛的预测误差学习）"),
            })
        intervals = {label: personal.interval(personal_history, label)
                     for label in predictions}
        intervals = {k: v for k, v in intervals.items() if v}
    result = {
        "vdot": vdot_val,
        "predictions": predictions,
        "zones": vd.intensity_zones(vdot_val) if vdot_val else None,
        "evidence": evidence,
        "components": [{"kind": c["kind"], "vdot": c["vdot"],
                        "weight": c["weight"]} for c in components],
        "prediction_intervals": intervals,
        "personal_races": len(personal_history or []),
        "max_hr": round(max_hr) if max_hr else None,
        "as_of": (as_of or date.today()).isoformat(),
    }
    return result


# ---- 批33 证据管线：训练表现证据 → 当前能力 → 各距离预测 ----
# 通道按距离的专项性混合（5K 重间歇/短分段，全马重长分段/长距离课/比赛）
SPEC_WEIGHTS = {
    "5K": {"interval": 0.40, "best_effort": 0.30, "steady": 0.10, "race": 0.20},
    "10K": {"interval": 0.30, "best_effort": 0.25, "steady": 0.15, "race": 0.30},
    "半马": {"best_effort": 0.22, "steady": 0.20, "race": 0.50, "interval": 0.08},
    "全马": {"steady": 0.25, "race": 0.55, "best_effort": 0.15, "interval": 0.05},
}
# 有同距离全开比赛时的通道混合：比赛权重上调——同距离比赛是该距离最直接的
# 证据，训练证据与目标距离的转换在个人层面可能系统性偏乐观（真实库 10K：
# 训练通道 52–59 vs 比赛 46–51 的持续背离，比赛稀少时训练证据才作主）
SPEC_WEIGHTS_RACED = {
    "5K": {"interval": 0.30, "best_effort": 0.20, "steady": 0.10, "race": 0.40},
    "10K": {"interval": 0.25, "best_effort": 0.15, "steady": 0.10, "race": 0.50},
    # 半马同距离比赛在场时比赛通道接近全权：真实库 6/21 与 7/19 两场半马
    # 重复跑 53.5，比赛通道 53.5 就是真相，而同期日常最快分段（夏天轻松跑）
    # 只有 47.5——训练通道对半马的转换差系统性偏乐观/失真
    "半马": {"best_effort": 0.10, "steady": 0.12, "race": 0.70, "interval": 0.08},
    "全马": {"steady": 0.20, "race": 0.60, "best_effort": 0.15, "interval": 0.05},
}
# 各距离预测用哪几档最快分段（短=速度、中=速度耐力、长=耐力）
SPEC_BUCKETS = {"5K": ("short", "mid"), "10K": ("mid",),
                "半马": ("mid", "long"), "全马": ("long",)}
# 通道矛盾加权（E3）：训练与比赛通道差 ≥CONTRADICT_GAP 时向比赛通道倾斜，
# 差到 GAP+SPAN 时比赛权重拉满（见 _label_vdot）
# SPAN 收窄到 1.0（原 4.0）：训练通道是「证据 → 比赛」的换算，本身带着不确定
# 的转换差；同距离全开比赛不需要换算，是直读。两边差过噪声量级后，再按 4 VDOT
# 的坡度慢慢把错的一方混进来没有意义——两个互相矛盾的测量取加权平均不等于更
# 准。逐场对照（18 场真实比赛，err=预测−实际）：SPAN 4→3→2→1 时 MAE
# 83→81→76→73，到 2 以下饱和（GAP 再降到 1.5/1.0 只再动 1 s，不值得多改一个
# 常量）；受益集中在四场大误差：半马 6/21 +102→+50、7/19 +81→+38、
# 10-07 5K −93→−38、10-04 10K −53→−19；劣化最大的一场 +11 s。
CONTRADICT_GAP = 2.0
CONTRADICT_SPAN = 1.0
RACE_HALF_LIFE_DAYS = 60        # 比赛通道按距今半衰期加权
RACE_CAP_WINDOW_DAYS = 90       # 能力锚只认近 90 天比赛（再旧的不作当下能力证据）
RACE_CAP_DECAY_PER_DAY = 0.02   # 能力锚按距今折扣（每过一天 −0.02 VDOT）


def _race_channel(races_graded: list[dict], as_of: date,
                  label: str | None = None) -> tuple[float | None, list[str]]:
    """全开比赛通道：近期加权中位（60 天半衰期）+ 能力锚取高。

    label 给定时只取该距离的全开比赛——同距离比赛是预测该距离最直接的
    证据（批33 回测：运动员 5K 54.9 与 10K 49.5 并存时，混合通道会把
    10K 预测抬高 4+ VDOT）；该距离没有全开比赛时调用方退回混合通道。
    加权中位抗离群慢场，但提升期会被更早的慢比赛拖低——全力比赛是
    「跑出过」的能力下限：近 90 天内最佳成绩按距今折扣后作为能力锚，
    通道取两者较高者（真实库 6/21 半马：中位 49.5 vs 4/12 的 54.9 峰值，
    只取中位会让预测比真实慢 4 分钟）。
    次全力比赛（训练计时/状态差）不参与——它们锚住的「水平」远低于真实
    能力，会把中心值往下拖（真实库 8 月 10K 计时 48.6–48.9 vs 真实 54–55）。
    """
    def _age(r: dict) -> float:
        try:
            return float(max((as_of - date.fromisoformat(str(r["day"])[:10])).days, 0))
        except (ValueError, KeyError):
            return 0.0
    recs = [(r["actual_vdot"], _age(r), r)
            for r in races_graded if r.get("quality") != "submax"
            and (label is None or r.get("label") == label)]
    if not recs:
        return None, []
    vals = [v for v, _, _ in recs]
    weights = [_recency(a, RACE_HALF_LIFE_DAYS) for _, a, _ in recs]
    med = round(_weighted_median(vals, weights), 1)
    cap = max((v - RACE_CAP_DECAY_PER_DAY * a for v, a, _ in recs
               if a <= RACE_CAP_WINDOW_DAYS), default=None)
    channel = round(max(med, cap), 1) if cap is not None else med
    detail = [f"{r['day']} {r['label']} {r['actual_vdot']:.1f}" for _, _, r in recs]
    if cap is not None and cap > med + 0.05:
        detail.append("能力锚取近 90 天最佳")
    return channel, detail


def _race_channel_fresh(races_graded: list[dict], as_of: date,
                        label: str | None = None) -> bool:
    """通道里有没有近 RACE_CAP_WINDOW_DAYS 天的全开比赛（label 缺省=混合）。

    矛盾加权（E3）只信「近期」比赛：过期比赛与当前训练的背离不是矛盾信号
    而是隐性 PB（训练大幅超越旧比赛，预测应贴训练走——test_pipeline_
    hidden_pb_from_training_beats_old_race 的 120 天旧赛 vs 当前训练）。
    """
    for r in races_graded:
        if r.get("quality") == "submax":
            continue
        if label is not None and r.get("label") != label:
            continue
        try:
            age = (as_of - date.fromisoformat(str(r["day"])[:10])).days
        except (ValueError, KeyError):
            continue
        if age <= RACE_CAP_WINDOW_DAYS:
            return True
    return False


def _be_bucket_vdot(fitness: dict, buckets: tuple[str, ...]) -> float | None:
    """各距离预测用的最快分段档：档内记录按条数加权合并（n 大者主导）。"""
    b = fitness.get("best_effort_buckets") or {}
    found = [(b[bk]["vdot"], b[bk]["n"]) for bk in buckets if bk in b]
    if not found:
        return None
    return round(sum(v * n for v, n in found) / sum(n for _, n in found), 1)


def _compute_with_evidence(activities: list[dict], vo2max, profile_max_hr,
                           rest_hr, as_of, year_bests, plan_exec,
                           personal_history: list[dict], samples_fn) -> dict:
    """批33 证据管线（生产路径：personal_history 在场且非一键评估口径）。

    流程：训练证据提取（每节课至多一类）→ 个人训练→比赛转换差（EWMA+收缩）
    → 当前能力（近期加权中位）→ 比赛分级（次全力剔除）→ 通道混合 →
    按距离偏差校准 → 预测区间（执行噪声 + 证据密度，不重复叠加）。

    - 不做「训练配速 = 比赛配速」：先加个人/群体转换差，再按距离专项混合。
    - 比赛少≠信息少：训练证据（503 次活动 / 620k 样本点）决定中心与密度。
    - 无比赛上限锚定：次全力比赛不进通道，中心值由当前训练证据主导。
    - 隐性 PB：训练证据显示的水平高于官方 PB 时，预测就取证据水平。
    """
    as_of = as_of or date.today()
    max_hr = estimate_max_hr(profile_max_hr, activities)
    races = detect_races(activities, max_hr)
    race_ids = {r["id"] for r in races if r.get("id") is not None}
    # 训练证据覆盖整个 180 天窗口（比赛分级需要赛前 60 天的证据；旧比赛
    # 的分级也要有据可依）；「当前能力」只用近 90 天切片
    ev = evidence.extract_workout_evidence(activities, samples_fn, max_hr,
                                           as_of, window_days=180,
                                           race_ids=race_ids)
    ev90 = [r for r in ev
            if r["day"] >= (as_of - timedelta(days=evidence.EVIDENCE_WINDOW_DAYS)).isoformat()]
    deltas = evidence.conversion_deltas(personal_history or [])
    fitness = evidence.current_fitness(ev90, deltas, as_of)
    # 比赛分级：历史已判定的比赛沿用（旧记录迁移时按分量判定、新比赛按训练
    # 证据判定，同一概念）；窗口内未判定的比赛按同期训练证据分级
    q_by_day = {(e.get("day"), e.get("label")): e.get("quality")
                for e in (personal_history or []) if e.get("quality")}
    # 同距离历史比赛成绩（分级参照：连续比赛水平即该距离真实水平）
    prior_by_label: dict[str, list[float]] = {}
    for e in (personal_history or []):
        if e.get("actual_vdot") is not None and e.get("label"):
            prior_by_label.setdefault(e["label"], []).append(e["actual_vdot"])
    races_graded = []
    for r in races:
        q = q_by_day.get((r["day"], r["label"]))
        if not q:
            q = evidence.grade_race(r, ev, deltas, max_hr,
                                    prior_races=prior_by_label.get(r.get("label")))
        races_graded.append({**r, "quality": q})
    race_vdot, race_detail = _race_channel(races_graded, as_of)
    # 各距离自己的全开比赛通道（同距离比赛是该距离最直接的证据；该距离
    # 没有全开比赛时退回混合通道）。只认窗口内的比赛——单场旧比赛的加权
    # 中位就是那场比赛本身，60 天半衰期对 n=1 形同虚设，会让「近 90 天
    # 不作当下能力证据」的规则失效（真实库 10-07 5K 预测：仅剩的 4/12 5K
    # 已过 178 天，仍以 0.40 权重锚住该距离，再被矛盾加权抬到 0.76，
    # 预测比实际快 93 s；同距离无近期比赛时退回混合通道后降到 67 s）。
    # 只有旧比赛的距离仍能通过混合通道与「近一年最佳」退回路径拿到比赛证据。
    race_vdot_by_label = {}
    for _lbl in SPEC_WEIGHTS:
        _v = _race_channel(races_graded, as_of, _lbl)[0]
        if _v is not None and _race_channel_fresh(races_graded, as_of, _lbl):
            race_vdot_by_label[_lbl] = _v
    # 窗口内没有全开比赛：退回近一年最佳比赛（历史能力参考，权重同比赛通道；
    # 全马等长距离经常半年才跑一次，不能因为「近 180 天没比赛」就丢掉证据）
    if race_vdot is None and year_bests:
        pbs = [r for r in year_bests if r.get("vdot") and r.get("source") == "race"]
        if pbs:
            pb = max(pbs, key=lambda r: r["vdot"])
            race_vdot = round(float(pb["vdot"]), 1)
            race_detail = [f"近一年最佳 {pb.get('distance', '比赛')} VDOT {race_vdot}（窗口内无全开比赛）"]

    # 各通道在场值（训练证据一律经个人转换差修正）
    channels: dict[str, float] = {}
    for kind in ("interval", "steady"):
        f = fitness.get(kind)
        if f:
            channels[kind] = f["vdot"]
    be = fitness.get("best_effort")
    if be:
        channels["best_effort"] = be["vdot"]
    if race_vdot is not None:
        channels["race"] = race_vdot

    def _label_vdot(label: str) -> float | None:
        # 有同距离全开比赛：比赛权重上调 + 用该距离自己的比赛通道
        has_label_race = label in race_vdot_by_label
        wmap = (SPEC_WEIGHTS_RACED[label] if has_label_race
                else SPEC_WEIGHTS[label])
        race_v = race_vdot_by_label.get(label, channels.get("race"))
        # 长距离（半马/全马）比赛稀少，同距离通道样本不足：允许被混合通道的
        # 近期能力锚抬升（跨距离上限信息——真实库 3 场半马与同期 5K 峰值差
        # 仅 1.4–1.9 VDOT，而 10K 系列差 4–6，故 10K/5K 不池化）
        if label in ("半马", "全马") and channels.get("race") is not None:
            pooled = channels["race"]
            if race_v is None or pooled > race_v:
                race_v = pooled
        pairs = []
        for kind, w in wmap.items():
            v = (_be_bucket_vdot(fitness, SPEC_BUCKETS[label])
                 if kind == "best_effort"
                 else (race_v if kind == "race" else channels.get(kind)))
            if v is not None:
                pairs.append((kind, v, w))
        if not pairs:
            return None
        blend = sum(v * w for _, v, w in pairs) / sum(w for _, _, w in pairs)
        # 通道矛盾时向比赛通道倾斜：训练与比赛差 ≥2 VDOT 说明其中一方失真，
        # 比赛是更硬的证据（回测：10K 冬季间歇 56 vs 比赛系列 48，真相贴着
        # 比赛走；半马 7 月轻松跑 47 vs 比赛 53.5，真相也贴着比赛走）。
        # 只对「真有同距离比赛」的距离生效——退回混合通道时比赛通道可能
        # 本身被其他距离污染，不能再给它加码（11-21 5K 首场：混合通道 48.9
        # 来自 10K 系列，训练 53.7 才是真相）。
        # 且只对「近期有全开比赛」生效——过期比赛与当前训练的背离是隐性
        # PB（训练超越旧比赛），方向相反，不能按矛盾处理。has_label_race
        # 本身就要求同距离有窗口内比赛（见 race_vdot_by_label 的构造），
        # 所以这里不再单独判 freshness：先前那句 `or _race_channel_fresh(
        # races_graded, as_of)` 会因「别的距离有近期比赛」而放行过期比赛。
        if has_label_race and race_v is not None:
            train_pairs = [(v, w) for k, v, w in pairs if k != "race"]
            if train_pairs:
                train_blend = sum(v * w for v, w in train_pairs) \
                    / sum(w for _, w in train_pairs)
                gap = abs(race_v - train_blend)
                boost = min(1.0, max(0.0, gap - CONTRADICT_GAP)
                            / CONTRADICT_SPAN)
                if boost > 0:
                    # 比赛权重上调、训练权重同步下调（总权重恒为 1）——
                    # 差到 GAP+SPAN 时训练通道完全让位，预测贴住比赛通道。
                    # 只加比赛权重不加满混合比例的旧写法在 boost=1 时仍保留
                    # 四成训练权重，与「比赛权重拉满」的注释意图不符
                    base_r = wmap["race"]
                    w_race = base_r + (1.0 - base_r) * boost
                    w_train = sum(w for _, w in train_pairs) * (1.0 - boost)
                    blend = (race_v * w_race + train_blend * w_train) \
                        / (w_race + w_train)
        return round(blend, 1)

    # 课表完成度调整（与旧口径同款公式：完成率 0.7 为中性点，钳制 ±上下限）
    plan_adj = 0.0
    if plan_exec and plan_exec.get("total"):
        ratio = plan_exec.get("ratio")
        if ratio is None:
            ratio = plan_exec["done"] / plan_exec["total"]
        plan_adj = max(QUALITY_ADJ_MIN, min(QUALITY_ADJ_MAX,
                                            (ratio - 0.7) * QUALITY_K))
    if abs(plan_adj) < 0.1:
        plan_adj = 0.0
    # 同配速心率趋势调整（与旧口径同款：心率下降 = 有氧能力进步，软证据）
    trend = hr_trend_ability(activities, as_of)
    trend_adj = round(trend["adj_vdot"], 1) if trend else 0.0

    base_vo2 = round(float(vo2max), 1) if vo2max else None
    predictions = {}
    vdot_labels = {}
    for label, dist in vd.DISTANCES.items():
        v_at = _label_vdot(label)
        if v_at is None:
            # 该距离没有任何训练/比赛通道：手表 VO2max 兜底（新用户唯一依据）
            if base_vo2 is None:
                continue
            v_at = base_vo2
        # 近期执行度（课表完成）+ 心率趋势 + 按距离偏差校准
        # （偏差：全开比赛学习，收缩后应用）
        v_at = round(v_at + plan_adj + trend_adj
                     + personal.bias_applied(personal_history or [], label), 1)
        vdot_labels[label] = v_at
        predictions[label] = vd.predict_time(dist, v_at)

    # 主 VDOT（看板大字）= 各距离预测的平均——大字与四行成绩口径一致，
    # 不会再出现「VDOT 52.5 但 5K 预测 18:48」的分裂
    vdot_val = (round(sum(vdot_labels.values()) / len(vdot_labels), 1)
                if vdot_labels else None)
    if vdot_val is None:
        # 没有任何通道在场：手表 VO2max 兜底（新用户唯一依据）
        vdot_val = round(float(vo2max), 1) if vo2max else None

    # 预测区间：执行噪声（跨距离池化去偏残差）⊕ 证据密度标准误
    se = evidence.center_se(ev90, as_of)
    prediction_intervals = {}
    for label, v_at in vdot_labels.items():
        iv = personal.interval(personal_history or [], label, center_se=se)
        if iv:
            prediction_intervals[label] = iv

    # 证据明细（看板「依据」逐条展示；source 用中文直给——dashboard.js 的
    # 映射表查不到时原样显示，UI 文案保持全中文）
    details = []
    if not channels and base_vo2 is not None:
        details.append({"source": "手表 VO2max 兜底", "vdot": base_vo2,
                        "detail": "无训练与比赛证据，各距离预测按手表 VO2max 读数兜底"})
    if fitness.get("interval"):
        f = fitness["interval"]
        d = deltas["interval"]
        src = "个人转换" if d["n"] else "群体先验"
        details.append({"source": "间歇训练证据", "vdot": f["vdot"],
                        "detail": (f"间歇/重复课 {f['n']} 节（近 90 天）当前能力 "
                                   f"VDOT {f['vdot']:.1f}（训练→比赛转换 {d['delta']:+.1f}，"
                                   f"{src}）")})
    if fitness.get("steady"):
        f = fitness["steady"]
        d = deltas["steady"]
        src = "个人转换" if d["n"] else "群体先验"
        details.append({"source": "匀速强度课证据", "vdot": f["vdot"],
                        "detail": (f"连续匀速强度课 {f['n']} 节当前能力 VDOT "
                                   f"{f['vdot']:.1f}（转换 {d['delta']:+.1f}，{src}）")})
    if be:
        b = fitness.get("best_effort_buckets") or {}
        d = deltas["best_effort"]
        src = "个人转换" if d["n"] else "群体先验"
        seg = "、".join(f"{k} 档 {v['n']} 条" for k, v in b.items())
        details.append({"source": "日常跑步最快分段", "vdot": be["vdot"],
                        "detail": (f"每次跑步样本滑窗切出的最快分段 {be['n']} 条"
                                   f"（{seg}）当前能力 VDOT {be['vdot']:.1f}"
                                   f"（转换 {d['delta']:+.1f}，{src}）")})
    if race_vdot is not None:
        n_sub = sum(1 for r in races_graded if r.get("quality") == "submax")
        sub_txt = f"；另有 {n_sub} 场次全力已剔除（训练计时不作锚定）" if n_sub else ""
        lbl_txt = ""
        if len(race_vdot_by_label) > 1:
            lbl_txt = "；各距离分别锚定：" + "、".join(
                f"{l} {v:.1f}" for l, v in race_vdot_by_label.items())
        details.append({"source": "比赛通道", "vdot": race_vdot,
                        "detail": f"全开比赛 {len(race_detail)} 场（{'; '.join(race_detail)}），"
                                  f"近期加权水平 VDOT {race_vdot:.1f}{lbl_txt}{sub_txt}"})
    learned = [k for k in evidence.KINDS if (deltas.get(k) or {}).get("n")]
    if learned:
        details.append({"source": "训练-比赛个人转换",
                        "detail": "已按全开比赛学习训练表现到比赛水平的个人转换："
                                  + "、".join(f"{k} {deltas[k]['delta']:+.1f}"
                                              f"（{deltas[k]['n']} 场）" for k in learned)})
    if trend:
        up_down = "下降" if trend["drop_bpm"] < 0 else "上升"
        details.append({"source": "配速-心率趋势", "vdot": vdot_val,
                        "detail": (f"同配速（{trend['pace_label']}/km）平均心率较早期"
                                   f"{up_down} {abs(trend['drop_bpm']):.0f} bpm → 调整 "
                                   f"{'+' if trend['adj_vdot'] >= 0 else ''}"
                                   f"{trend['adj_vdot']} VDOT")})
    if plan_adj:
        ratio_shown = plan_exec.get("ratio")
        if ratio_shown is None:
            ratio_shown = plan_exec["done"] / plan_exec["total"]
        details.append({"source": "课表完成度", "vdot": vdot_val,
                        "detail": (f"近 {QUALITY_WINDOW_DAYS // 7} 周课表质量课完成 "
                                   f"{plan_exec['done']}/{plan_exec['total']}"
                                   f"（{int(round(ratio_shown * 100))}%）→ 执行校验 "
                                   f"{'+' if plan_adj >= 0 else ''}{plan_adj:.1f} VDOT")})
    applied_bias = {l: personal.bias_applied(personal_history or [], l)
                    for l in vdot_labels}
    applied_bias = {l: b for l, b in applied_bias.items() if b}
    if applied_bias:
        details.append({"source": "个人偏差校准",
                        "detail": "、".join(f"{l} {b:+.1f} VDOT"
                                            for l, b in applied_bias.items())
                                  + f"（按近 {len(personal_history or [])} 场真实比赛"
                                    f"的预测误差学习）"})
    if se > 0:
        details.append({"source": "预测区间依据",
                        "detail": (f"执行噪声与证据密度标准误 {se:.2f} 合成"
                                   f"（近 90 天训练证据决定中心估计精度），约 80% 置信")})

    return {
        "vdot": vdot_val,
        "predictions": predictions,
        "zones": vd.intensity_zones(vdot_val) if vdot_val else None,
        "evidence": details,
        "components": [{"kind": k, "vdot": channels[k], "weight": 1.0}
                       for k in channels],
        "prediction_intervals": prediction_intervals,
        "personal_races": len(personal_history or []),
        "max_hr": round(max_hr) if max_hr else None,
        "as_of": as_of.isoformat(),
        "ev_fitness": fitness,
        "ev_list": ev,
        "race_grades": races_graded,
        "race_vdot": race_vdot,
        "window_days": 180,
        "model": "evidence",
    }


def quality_execution(workouts: list[dict], today: date | None = None,
                      auto_done: set[int] | None = None) -> dict | None:
    """近 8 周课表内质量课完成比例（课程内容 + 完成情况的硬证据）。

    workouts: plan_repo.get_workouts() 行（需含 date/kind/status/id），可为空；
    本函数按 [today−QUALITY_WINDOW_DAYS, today] 再滤一次。质量课之外的
    轻松跑/休息日不算（那些缺勤不代表强度水平）；status='completed' 计入
    完成，其余按读取侧自动判定 auto_done（load_metrics.workout_auto_done，
    服务层配对当日实际跑步后传入）计入；skipped/planned 且无自动完成
    计入未完成。样本 <QUALITY_MIN_TOTAL 返回 None。
    """
    end = today or date.today()
    start = end - timedelta(days=QUALITY_WINDOW_DAYS)
    qs = [w for w in workouts
          if w.get("kind") in QUALITY_KINDS
          and isinstance(w.get("date"), str)
          and start.isoformat() <= w["date"] <= end.isoformat()]
    if len(qs) < QUALITY_MIN_TOTAL:
        return None
    done = 0
    for w in qs:
        st = w.get("status")
        if st == "completed":
            done += 1
        elif auto_done and st != "skipped" and w.get("id") in auto_done:
            done += 1
    return {"done": done, "total": len(qs), "ratio": done / len(qs)}


def _days_old(value, as_of: date | None) -> int:
    """记录日期（date/iso 字符串）距 as_of 的天数；无法解析视为 0。"""
    day = None
    if isinstance(value, str):
        try:
            day = date.fromisoformat(value[:10])
        except ValueError:
            day = None
    if day is None:
        return 0
    return max(((as_of or date.today()) - day).days, 0)


def _fmt_time(s: float) -> str:
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _fmt_pace(s: float) -> str:
    m = int(s // 60)
    sec = int(round(s % 60))
    if sec >= 60:
        m += 1
        sec -= 60
    return f"{m}:{sec:02d}"
