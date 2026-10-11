"""训练表现证据层（批33 · 成绩预测精度升级）：把每一次跑步转成一条「能力
证据」，再由预测模型统一使用——一份训练只形成一份证据，杜绝同一份数据被
间歇/阈值/速度/有氧多个模块重复计数后叠加。

三类证据（每节课最多落入一类，优先级：比赛 > 间歇 > 专项强度课 > 最快分段）：
- race：正式比赛/全力测试跑（检测与分级见 ability.detect_races + grade_race）
- interval：间歇/重复课（≥2 个 work 段），按生理类型（阈值/摄氧/冲刺）以
  对应 %VDOT 反算，含重复数/段距/休息比/最后一组——速度能力的直接证据
- steady：连续匀速强度课（15–75 min，整场心率 82–95% HRmax），按 T 强度
  反算——阈值能力的直接证据
- best_effort：其余跑步（含长距离/渐速跑）用样本点滑窗切出的 1K/3K/5K/10K/
  15K 最快分段（心率门 ≥82% HRmax 防散步/下坡虚快）——有氧与速度耐力证据，
  每次跑步都产出 → 证据密度的大头

「训练表现 → 比赛表现」转换：训练里跑出的 VDOT 不等于比赛能跑出的 VDOT。
每类证据有一个群体先验转换差 Δ（比赛 − 证据），再从该运动员自己的全开
比赛历史里学习个人转换（EWMA + 样本收缩回先验）——比赛越多越个性化。

证据等级（grade_race）：正式比赛也可能不是全开（训练计时/带病跑）。无名称
提示且成绩明显低于同期训练证据（≥3 条时）的比赛判为「次全力」——不参与
偏差学习、预测区间误差与比赛锚定，防止系统偏差被假比赛污染（真实库 10K
计时 48–51 VDOT vs 同期间歇 55–58，全开比赛 54.9 的反差即此类）。

常量依据（真实库 3 场全开比赛，tools/_probe_prediction_audit.py）：
best_effort 窗内配速 vs 全开比赛 Δ = +3.0/+3.1/+2.6 → 先验 +2.9；
interval vs 比赛 Δ = −0.2/+0.4（7 月暑热 +4.0 剔除）→ 先验 +0.1；
steady 样本混杂 → 先验 +1.5（保守）。
单条证据观测噪声 σ 由同一散布标定：best_effort 0.6 / interval 1.0 / steady 1.2。
"""
from __future__ import annotations

from datetime import date, timedelta

from . import ability as ab
from . import vdot as vd

EVIDENCE_WINDOW_DAYS = 90        # 「当前能力」证据窗口
EVIDENCE_HALF_LIFE_DAYS = 30     # 证据按距今半衰期加权（近期主导）
BEST_EFFORT_SCAN_CAP = 120       # 参与分段扫描的近期跑步上限（防超大库拖慢看板）

# 采样缺口：正常采样间隔 1–5 s（真实库 467 个活动的中位 2 s、p90 3 s），
# 远大于它的间隔是停表/暂停——那段时间既不是跑步时间，也不能按「缺口后
# 那个样本的速度」外推出距离（真实库 id=233：557 s 空档被算成 0.69 m/s ×
# 557 s = 384 m，使 17.06 km 的一次跑步里「最快 15K 分段」反比全程均速还
# 慢，VDOT 被压到 41.3 而实际约 48）。阈值取 max(固定下限, 5×该活动中位间隔)：
# 兼顾固定节奏设备与个别稀疏采样的设备。参见 _best_effort_windows_for。
SAMPLE_GAP_MIN_S = 10.0
SAMPLE_GAP_MEDIAN_MULT = 5.0

# 最快分段窗只保留 5K 及以上（1K/3K 窗太短太噪，一场轻松跑里的快 1 公里
# 不是速度能力证据；与 ability.distance_bests 的段距离集合一致）
BEFFORT_DISTS = ((5000, "5K"), (10000, "10K"), (15000, "15K"))
# 各距离预测用哪几档最快分段（short=速度，mid=速度耐力，long=耐力）
BEFFORT_BUCKETS = {"short": ("5K",), "mid": ("10K",), "long": ("15K",)}

# 群体先验：训练证据 VDOT → 比赛 VDOT 的转换差（Δ = 比赛 − 证据）。
# best_effort +5.7：真实库 6 场全开比赛赛前 60 天全部窗口的转换差总体中位
# +6.4~+7.9（tools/_probe_beffort_calib.py）——此前 +2.9 只取了单场最佳窗口，
# 低估了「日常跑步里的最快分段 ≠ 比赛表现」的差距。
CONV_PRIOR = {"interval": 0.1, "steady": 1.5, "best_effort": 5.7}
CONV_HR_REF = 0.92               # 参考努力程度（接近比赛的努力）
CONV_HR_GRAD = {"interval": 0.0, "steady": 15.0, "best_effort": 15.0}
#   按心率修正：窗口/强度课跑得越不费力，离比赛水平越远——转换差每低 1% 心率
#   加大 0.15 VDOT（真实库分档中位 0.82→+7.9、0.88→+6.4、0.93→+5.9 的斜率）。
CONV_ALPHA = 0.3                 # 个人转换 EWMA 学习率
CONV_CAP = 3.0                   # 个人转换封顶（VDOT）
CONV_SHRINK_K = 2.0              # 向先验收缩：Δ = 先验 + (学习值−先验)×n/(n+K)

# 比赛分级：无名称提示且成绩低于同期证据中位 3.0 VDOT 以上 → 次全力
RACE_SUBMAX_GAP = 3.0
RACE_EVIDENCE_MIN_N = 3          # 同期证据 ≥3 条才分级（不足不下结论）
RACE_EVIDENCE_DAYS = 30          # 同期参考窗口：只比最近 30 天训练证据——
#                                  60 天会把上个月巅峰期的快段带进来，把
#                                  「巅峰已过的真全力比赛」（12 月 48.0 vs
#                                  11 月峰值证据 53+）误判成次全力
RACE_CONSISTENT_GAP = 3.0        # 不比个人同距离最好成绩差 3 VDOT 以上 →
#                                  成绩在真实比赛水平带上，直接判全开。
#                                  依据（真实库 10K 系列）：48.6–51.2 的
#                                  连续比赛 vs 训练通道 52–59 的持续背离——
#                                  训练证据对个别距离系统性偏乐观时，比赛的
#                                  自我一致性比训练证据更可信；训练计时（
#                                  距最好成绩 8+ VDOT）仍会被拦在门外

# 不确定性：单条证据观测噪声（VDOT，标定见模块注释）
OBS_SIGMA = {"interval": 1.0, "best_effort": 0.6, "steady": 1.2}
OBS_CAP = {"interval": 12, "best_effort": 18, "steady": 8}   # 计入 n_eff 的上限
OBS_RECENT_DAYS = 90             # n_eff 只数近期证据

KINDS = ("interval", "steady", "best_effort")

# ---- 环境折算：把「实测配速」换算成「平路户外等效配速」 -------------------
# 训练证据是拿配速当能力用的，但同一个配速在不同环境里代表的能力不同。
# 系数由真实库 440 条 ≥3km 的有氧课做 OLS 标定（tools/probe_env_adjust.py），
# 模型 pace_s_km ~ avg_hr + grade_m_per_km + is_treadmill + days_ago，
# R²=0.59（days_ago 项系数≈0，说明年内体能漂移已被心率项吸收）：
#   爬升         +1.230 s/km 每 m/km   (se 0.223, t=+5.53, n=424 仅户外)
#   跑步机       −19.24 s/km           (se 6.50,  t=−2.96, n=16；同心率分档
#                                        中位差 +20 s/km 独立复核一致)
# 折算方向：上坡同心率更慢 → 不折算会低估水平；跑步机同心率更快（无风阻、
# 皮带助力）→ 不折算会高估水平。
#
# 回测拆分（tools/probe_env_split.py，17 场真实比赛，四项对照）：
#   两项都开 MAE 83 / 只关坡度 86 / 只关跑步机 83 / 两项都关 86
#   → 收益全部来自坡度项，跑步机项在回测里中性（库里只有 16 条跑步机活动，
#     且都不在喂给那 17 场比赛的窗口里）。跑步机项暂保留（OLS 本身显著，
#     t=−2.96），待真机攒够跑步机标签后重估。
GRADE_PACE_S_KM_PER_M = 1.23
GRADE_ADJ_CAP_S_KM = 60.0        # 极端爬升不无限放大
TREADMILL_PACE_DELTA_S_KM = 19.0
TREADMILL_NAME_HINTS = ("treadmill", "跑步机", "跑步機", "indoor run", "室内跑",
                        "履带", "履帶")


def is_treadmill(a: dict) -> bool:
    """是否跑步机/室内跑（按名称；佳明的运动类型字段同步时被统一成 running）。"""
    name = (a.get("name") or "").lower()
    return any(h in name for h in TREADMILL_NAME_HINTS)


def env_adjusted_pace(a: dict, pace_s_km: float | None) -> float | None:
    """实测配速 → 平路户外等效配速（s/km）。比赛不折算（那是真值，见调用点）。"""
    if not pace_s_km:
        return pace_s_km
    out = pace_s_km
    dist = a.get("distance_m") or 0
    gain = a.get("elevation_gain_m")
    if gain and dist >= 1000:
        grade = gain / (dist / 1000.0)
        out -= min(GRADE_ADJ_CAP_S_KM, grade * GRADE_PACE_S_KM_PER_M)
    if is_treadmill(a):
        out += TREADMILL_PACE_DELTA_S_KM
    return out


# ---- 主观努力：佳明 Connect 的 feel(1-5) / rpe(1-10) ---------------------
# 主观用力是比心率更直接的努力信号（心率受暑热/咖啡因/疲劳干扰）。
# 真实库暂无这两列数据（用户还没打过标签），所以这里只做「保守降权」：
# 只会削弱「自述很轻松」的证据，绝不会凭主观分抬高预测——等真机同步到
# 标签后再用回测验证升级方向。
RPE_EASY_MAX = 5                 # RPE ≤5 视为「不算强度课」
RPE_EASY_WEIGHT = 0.75           # 降权系数（不归零：配速与心率仍是事实）
RPE_VERY_EASY_MAX = 3
RPE_VERY_EASY_WEIGHT = 0.5
# 比赛分级用 RPE（10 分制常用刻度：9=极吃力、10=极限）。阈值取保守值——
# 【证据不足】真实库还没有 RPE 标签，暂无法回测校准；等标签攒够再调。
RACE_RPE_ALL_OUT = 9
RACE_RPE_SUBMAX = 5


def rpe_weight(rpe) -> float:
    """主观用力 → 证据权重系数（≤1，只降不升）。无标签 = 1.0（中性）。"""
    if not rpe:
        return 1.0
    if rpe <= RPE_VERY_EASY_MAX:
        return RPE_VERY_EASY_WEIGHT
    if rpe <= RPE_EASY_MAX:
        return RPE_EASY_WEIGHT
    return 1.0


def _day(a: dict, as_of: date) -> date | None:
    from ..utils import dates
    try:
        d = dates.ts_to_date(a["start_ts"]) if a.get("start_ts") else None
    except Exception:
        d = None
    if d is None and a.get("date"):
        try:
            d = date.fromisoformat(str(a["date"])[:10])
        except ValueError:
            d = None
    if d is None or (as_of and d > as_of):
        return None
    return d


def looks_like_race(a: dict, max_hr: float | None) -> bool:
    """该活动是否被当作比赛处理（与 ability 的判定同款：距离带内且
    [名称提示 | 平均心率 ≥90% 最大心率 | 对照当场峰值 ≥94% 且无休息段]）。
    比赛不算训练证据——避免同一份数据在比赛通道与训练通道重复计数。"""
    dist, dur = a.get("distance_m"), a.get("duration_s")
    if not dist or not dur or dist < 3000 or dist > 43000:
        return False
    in_band = any(abs(dist - std) / std <= tol for std, tol in ab.RACE_BANDS)
    if not in_band:
        return False
    name = (a.get("name") or "").lower()
    if any(h in name for h in ab.RACE_NAME_HINTS):
        return True
    avg_hr = a.get("avg_hr")
    if not avg_hr:
        return False
    if max_hr and avg_hr / max_hr >= ab.RACE_HR_MIN_REF:
        return True
    struct = a.get("structure") or []
    return bool(a.get("max_hr") and avg_hr / a["max_hr"] >= ab.OWN_RACE_MIN_REF
                and not any(s.get("type") == "rest" for s in struct))


def extract_workout_evidence(activities: list[dict], samples_fn=None,
                             max_hr: float | None = None,
                             as_of: date | None = None,
                             window_days: int | None = None,
                             race_ids: set | None = None) -> list[dict]:
    """把窗口内每次跑步转成证据记录（每节课至多一类）。

    activities：含 structure（分段）/has_samples/start_ts/date/avg_hr/
    avg_pace_s_km/distance_m/duration_s/name 的跑步活动（调用方已切窗口）。
    samples_fn(activity_id) -> 按时间序的样本 dict 列表（t_offset_s/hr/
    speed_mps）；无样本的活动不产 best_effort 证据。
    race_ids：比赛活动 id 集合——比赛成绩走比赛通道，不得再作为训练证据
    （同一份数据不能被两条通道各计一次）。
    分段扫描按最近优先消耗预算（近期跑步决定当前能力，旧课只兜底）。
    返回按日期升序的 [{day, kind, subtype, vdot, pace_s_km, n_reps,
    rest_ratio, dist, hr_ratio, quality}]；无证据返回 []。
    """
    as_of = as_of or date.today()
    span = window_days if window_days is not None else EVIDENCE_WINDOW_DAYS
    lo = as_of - timedelta(days=span)
    out: list[dict] = []
    scan_budget = BEST_EFFORT_SCAN_CAP
    for a in sorted(activities, key=lambda x: x.get("start_ts") or 0,
                    reverse=True):
        day = _day(a, as_of)
        if day is None or day < lo:
            continue
        if race_ids and a.get("id") in race_ids:
            continue
        struct = a.get("structure") or []
        if isinstance(struct, str):
            struct = []
        if looks_like_race(a, max_hr):
            continue
        works = [s for s in struct if s.get("type") == "work"
                 and s.get("pace_s_km")
                 and ab.WORK_SEG_RANGE_M[0] <= (s.get("distance_m") or 0)
                 <= ab.WORK_SEG_RANGE_M[1]]
        rests = [s for s in struct if s.get("type") == "rest"
                 and (s.get("elapsed_s") or 0) > 0]
        is_interval = len(works) >= 2
        if is_interval:
            rec = _interval_record(a, works, rests, max_hr, day)
            if rec:
                out.append(rec)
            continue
        # 连续匀速强度课（15–75 min，心率 82–95% HRmax，非比赛非间歇）
        avg_hr = a.get("avg_hr")
        dur = a.get("duration_s") or 0
        pace = a.get("avg_pace_s_km")
        if (max_hr and avg_hr and pace
                and ab.QT_DUR_WIN[0] <= dur <= ab.QT_DUR_WIN[1]
                and ab.QT_PACE_WIN[0] <= pace <= ab.QT_PACE_WIN[1]
                and ab.QT_HR_WIN[0] <= avg_hr / max_hr <= ab.QT_HR_WIN[1]):
            pace_adj = env_adjusted_pace(a, pace)
            out.append({
                "day": day.isoformat(), "kind": "steady", "subtype": "steady",
                "vdot": round(ab._vdot_for_pace(pace_adj, vd.T_PCT), 1),
                "pace_s_km": round(pace, 1),
                "pace_adj_s_km": round(pace_adj, 1),
                "env_adj": round(pace_adj - pace, 1),
                "rpe": a.get("rpe"), "feel": a.get("feel"),
                "hr_ratio": round(avg_hr / max_hr, 2),
                "quality": 1.0,
            })
            continue
        # 其余跑步：样本点滑窗最快分段（含长距离/渐速跑的快段）
        if not samples_fn or scan_budget <= 0 or not a.get("has_samples"):
            continue
        if a.get("id") is None or (a.get("distance_m") or 0) < 4850:
            continue
        scan_budget -= 1
        try:
            samples = samples_fn(a["id"]) or []
        except Exception:
            continue
        wins = _best_effort_windows_for(samples, BEFFORT_DISTS, max_hr)
        std_by_label = {lbl: m for m, lbl in BEFFORT_DISTS}
        for label, (pace_s_m, hr_avg) in wins.items():
            std_m = std_by_label[label]
            raw_pace_s_km = pace_s_m * 1000
            # 滑窗配速也按环境折算成平路户外等效（跑步机的速度无风阻、上坡的
            # 速度被坡度拖慢，用实测值直接反推 VDOT 会偏）
            adj_pace_s_km = env_adjusted_pace(a, raw_pace_s_km)
            proj_s = adj_pace_s_km / 1000.0 * std_m
            out.append({
                "day": day.isoformat(), "kind": "best_effort", "subtype": label,
                "dist": label,
                "vdot": round(vd.estimate_vdot(std_m, proj_s), 1),
                "pace_s_km": round(raw_pace_s_km, 1),
                "pace_adj_s_km": round(adj_pace_s_km, 1),
                "env_adj": round(adj_pace_s_km - raw_pace_s_km, 1),
                "rpe": a.get("rpe"), "feel": a.get("feel"),
                "hr_ratio": round(hr_avg / max_hr, 2) if (hr_avg and max_hr) else None,
                "quality": 1.0,
            })
    out.sort(key=lambda r: r["day"])
    return out


def _interval_record(a: dict, works: list[dict], rests: list[dict],
                     max_hr: float | None, day: date) -> dict | None:
    """一节间歇课 → 一条证据：类型识别 + 中位快段配速按对应 %VDOT 反算，
    休息越短、快段越快 → 水平越高（恢复比修正，与 ability.interval_ability
    同款）。附重复数/段距/最后一组配速——最后一组不掉速是「撑得住」的信号。"""
    paces = sorted(w["pace_s_km"] for w in works)
    med_pace = paces[len(paces) // 2]
    # VDOT 用「平路户外等效配速」；课表/记录里仍显示实测配速
    med_pace_adj = env_adjusted_pace(a, med_pace)
    dists = sorted(w["distance_m"] or 0 for w in works)
    med_dist = dists[len(dists) // 2]
    itype = ab._interval_type(works, rests, max_hr)
    pct = ab._PCT_BY_INTERVAL_TYPE[itype]
    v = ab._vdot_for_pace(med_pace_adj, pct)
    work_t = sum(w.get("elapsed_s") or (w.get("distance_m") or 0)
                 * (w["pace_s_km"] or 300) / 1000 for w in works)
    rest_t = sum(r.get("elapsed_s") or 0 for r in rests)
    ratio = rest_t / work_t if work_t > 0 and rests else None
    if ratio is not None:
        v *= 1 + ab.INTERVAL_REST_K * (ab.INTERVAL_REST_REF - ratio)
    hrs = [w.get("avg_hr") for w in works if w.get("avg_hr")]
    hr_ratio = (sorted(hrs)[len(hrs) // 2] / max_hr) if hrs and max_hr else None
    last = works[-1].get("pace_s_km")
    return {
        "day": day.isoformat(), "kind": "interval", "subtype": itype,
        "vdot": round(v, 1), "pace_s_km": round(med_pace, 1),
        "pace_adj_s_km": round(med_pace_adj, 1),
        "env_adj": round(med_pace_adj - med_pace, 1),
        "rpe": a.get("rpe"), "feel": a.get("feel"),
        "n_reps": len(works), "med_dist_m": round(med_dist),
        "total_work_km": round(sum(w.get("distance_m") or 0 for w in works) / 1000, 2),
        "rest_ratio": round(ratio, 2) if ratio is not None else None,
        "last_rep_pace_s_km": round(last, 1) if last else None,
        "hr_ratio": round(hr_ratio, 2) if hr_ratio else None,
        "quality": 1.0 if hr_ratio else 0.7,   # 无心率依据 → 类型识别靠结构，稍降权
    }


def vdot_for_pace_proj(std_m: float, pace_s_m: float) -> float:
    """窗口每米用时 → 目标距离投影用时 → VDOT（标定/调试共用口径）。"""
    return round(vd.estimate_vdot(std_m, pace_s_m * std_m), 1)


def _best_effort_windows_for(samples: list[dict], dists: tuple,
                             max_hr: float | None) -> dict[str, tuple[float, float | None]]:
    """一次活动内为每个目标距离滑窗求最快分段（与 ability._best_effort_windows
    同算法，距离集合可配）。返回 {label: (每米用时 s/m, 窗口平均心率)}；
    窗口覆盖 [0.97, 1.12]×目标距离、平均心率 ≥82% HRmax（无心率窗口不参与）。

    采样缺口（停表/暂停）不计距离也不计时间：缺口只说明「那段时间没在跑」，
    若照常累加，窗口用时被撑大、距离又被缺口后那个样本的速度凭空补上，越快
    的长窗口越吃亏（见 SAMPLE_GAP_MIN_S）。
    """
    n = len(samples)
    if n < 2:
        return {}
    gaps: list[float] = []
    t_prev = None
    for s in samples:
        t = float(s.get("t_offset_s") or 0)
        if t_prev is not None and t > t_prev:
            gaps.append(t - t_prev)
        t_prev = t
    gap_limit = SAMPLE_GAP_MIN_S
    if gaps:
        med = sorted(gaps)[len(gaps) // 2]
        gap_limit = max(gap_limit, SAMPLE_GAP_MEDIAN_MULT * med)
    pd = [0.0] * (n + 1)
    pt = [0.0] * (n + 1)
    ph = [0.0] * (n + 1)
    pc = [0] * (n + 1)
    t_prev = None
    for i, s in enumerate(samples):
        t = float(s.get("t_offset_s") or 0)
        spd = float(s.get("speed_mps") or 0)
        dt = (t - t_prev) if t_prev is not None else 0
        if dt < 0:
            dt = 0
        if dt > gap_limit:
            dt = 0.0
        pd[i + 1] = pd[i] + (max(spd, 0) * dt if t_prev is not None else 0)
        pt[i + 1] = pt[i] + (dt if t_prev is not None else 0)
        hr = s.get("hr")
        ph[i + 1] = ph[i] + (float(hr) if hr else 0)
        pc[i + 1] = pc[i] + (1 if hr else 0)
        t_prev = t
    out: dict[str, tuple[float, float | None]] = {}
    for std, label in dists:
        lo, hi = std * 0.97, std * 1.12
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
                continue
            t = pt[j] - pt[i]
            if t <= 0:
                continue
            hr_n = pc[j] - pc[i]
            hr_avg = (ph[j] - ph[i]) / hr_n if hr_n > 0 else None
            if max_hr and (hr_avg is None or hr_avg < max_hr * ab.SEG_MIN_HR_RATIO):
                continue
            pace_s_m = t / d if d > 0 else 0
            cur = out.get(label)
            if cur is None or pace_s_m < cur[0]:
                out[label] = (pace_s_m, hr_avg)
    return out


def conversion_deltas(history: list[dict]) -> dict[str, dict]:
    """个人训练→比赛转换差（Δ_kind = 比赛 VDOT − 证据 VDOT，全开比赛样本）。

    history 条目需带 ev_feats（比赛当时各类证据的努力修正水平 eff）与
    quality（submax 不参与学习）。EWMA 慢学习（α=0.3，封顶 ±3），按样本数
    向群体先验收缩 n/(n+K)——一两场比赛翻不了盘，攒够才学得动。
    返回 {kind: {"delta": float, "n": int}}；无样本的 kind 直接给先验（n=0）。
    """
    out: dict[str, dict] = {}
    for kind in KINDS:
        samples: list[float] = []
        for e in sorted(history, key=lambda x: x.get("day") or ""):
            if e.get("quality") == "submax":
                continue
            ev = (e.get("ev_feats") or {}).get(kind)
            if ev is None or e.get("actual_vdot") is None:
                continue
            samples.append(e["actual_vdot"] - ev)
        if not samples:
            out[kind] = {"delta": round(CONV_PRIOR[kind], 1), "n": 0}
            continue
        ewma = 0.0
        for d in samples:
            d = max(-CONV_CAP, min(CONV_CAP, d))
            ewma = ewma + CONV_ALPHA * (d - ewma)
        ewma = max(-CONV_CAP, min(CONV_CAP, ewma))
        n = len(samples)
        delta = CONV_PRIOR[kind] + (ewma - CONV_PRIOR[kind]) * n / (n + CONV_SHRINK_K)
        out[kind] = {"delta": round(delta, 2), "n": n}
    return out


def _eff_adjusted(vdot: float, kind: str, hr_ratio) -> float:
    """按努力程度修正：跑得越不费力（心率越低），离比赛水平越远。
    证据只报了「这次跑出来的速度」，真水平按与参考心率的差距外推。"""
    if hr_ratio is None:
        return vdot
    grad = CONV_HR_GRAD.get(kind, 0.0)
    if not grad:
        return vdot
    return vdot + grad * max(0.0, CONV_HR_REF - hr_ratio)


def current_fitness(evidence: list[dict], deltas: dict[str, dict] | None,
                    as_of: date | None = None,
                    half_life: float = EVIDENCE_HALF_LIFE_DAYS) -> dict:
    """各类证据的「当前能力」：转换后的 VDOT 按距今半衰期加权取中位。

    每条证据先按努力程度修正（eff），再加个人/群体转换差（vdot = eff + Δ）；
    返回 {kind: {"vdot": 转换后加权中位, "raw": 原始加权中位, "eff": 努力
    修正加权中位, "n": 条数, "mad": 散布, "last_day": 最近一条}}；
    best_effort 额外给按距离档 {"short"/"mid"/"long": {...}}。
    deltas 缺省时按群体先验（无个人历史的新用户）。
    """
    as_of = as_of or date.today()
    deltas = deltas or {k: {"delta": CONV_PRIOR[k], "n": 0} for k in KINDS}
    out: dict[str, dict] = {}
    by_kind: dict[str, list[dict]] = {k: [] for k in KINDS}
    for r in evidence:
        if r.get("kind") in by_kind:
            by_kind[r["kind"]].append(r)
    for kind, recs in by_kind.items():
        if not recs:
            continue
        delta = (deltas.get(kind) or {}).get("delta", CONV_PRIOR[kind])
        vdots = [r["vdot"] for r in recs]
        effs = [_eff_adjusted(r["vdot"], kind, r.get("hr_ratio")) for r in recs]
        ages = [_age_days(r["day"], as_of) for r in recs]
        weights = [ab._recency(a, half_life) * rpe_weight(r.get("rpe"))
                   for a, r in zip(ages, recs)]
        med = ab._weighted_median(vdots, weights)
        eff = ab._weighted_median(effs, weights)
        conv = round(eff + delta, 1)
        # MAD 散布（对加权中位的平均绝对偏差，加权）
        mad = ab._weighted_median([abs(v - med) for v in vdots], weights) \
            if len(vdots) > 1 else 0.0
        out[kind] = {"vdot": conv, "raw": round(med, 1), "eff": round(eff, 1),
                     "n": len(recs), "mad": round(mad, 1),
                     "last_day": recs[-1]["day"]}
    # best_effort 分档
    be = by_kind.get("best_effort") or []
    if be:
        buckets: dict[str, dict] = {}
        for bname, dists in BEFFORT_BUCKETS.items():
            recs = [r for r in be if r.get("dist") in dists]
            if not recs:
                continue
            delta = (deltas.get("best_effort") or {}).get("delta",
                                                          CONV_PRIOR["best_effort"])
            vdots = [r["vdot"] for r in recs]
            effs = [_eff_adjusted(r["vdot"], "best_effort", r.get("hr_ratio"))
                    for r in recs]
            ages = [_age_days(r["day"], as_of) for r in recs]
            weights = [ab._recency(a, half_life) for a in ages]
            med = ab._weighted_median(vdots, weights)
            eff = ab._weighted_median(effs, weights)
            buckets[bname] = {"vdot": round(eff + delta, 1),
                              "raw": round(med, 1), "eff": round(eff, 1),
                              "n": len(recs),
                              "last_day": recs[-1]["day"]}
        out["best_effort_buckets"] = buckets
    return out


def grade_race(race: dict, evidence: list[dict], deltas: dict[str, dict] | None,
               max_hr: float | None = None,
               prior_races: list[float] | None = None) -> str:
    """比赛分级：all_out（全开）或 submax（次全力——训练计时/状态差）。

    名称含比赛标记 → 全开（真实赛事，执行好坏都是真实比赛分布的一部分）。
    成绩不低于个人同距离最好成绩 3.0 VDOT 以上 → 全开（比赛系列的自一致性
    优先于训练证据——训练证据对个别距离系统性偏乐观时，连续比赛水平才是
    该距离的真实水平）。否则看成绩是否明显低于同期训练证据：赛前 30 天
    证据（转换后）≥3 条且比赛 VDOT 低于其中位 3.0 以上 → 次全力。
    证据不足不下结论（按全开）。
    """
    name = (race.get("name") or "").lower()
    if any(h in name for h in ab.RACE_NAME_HINTS):
        return "all_out"
    # 主观用力：比赛完自述还有余力 → 次全力；自述拼到极限 → 全开。
    # 只在有标签时生效（无标签完全退回原有心率/证据判据，行为不变）。
    rpe = race.get("rpe")
    if rpe:
        if rpe >= RACE_RPE_ALL_OUT:
            return "all_out"
        if rpe <= RACE_RPE_SUBMAX:
            return "submax"
    actual = race.get("actual_vdot")
    if (actual is not None and prior_races
            and (race.get("hr_ref") or 0) >= ab.OWN_RACE_EST_MAX_MIN):
        if actual >= max(prior_races) - RACE_CONSISTENT_GAP:
            return "all_out"
    day = race.get("day")
    if not day:
        return "all_out"
    try:
        race_day = date.fromisoformat(str(day)[:10])
    except ValueError:
        return "all_out"
    lo = race_day - timedelta(days=RACE_EVIDENCE_DAYS)
    deltas = deltas or {k: {"delta": CONV_PRIOR[k], "n": 0} for k in KINDS}
    vals = []
    for r in evidence:
        if not (lo <= date.fromisoformat(r["day"]) <= race_day):
            continue
        if r["kind"] not in KINDS:
            continue
        delta = (deltas.get(r["kind"]) or {}).get("delta", CONV_PRIOR[r["kind"]])
        vals.append(_eff_adjusted(r["vdot"], r["kind"], r.get("hr_ratio")) + delta)
    if len(vals) < RACE_EVIDENCE_MIN_N:
        return "all_out"
    med = sorted(vals)[len(vals) // 2]
    if race.get("actual_vdot", 0) < med - RACE_SUBMAX_GAP:
        return "submax"
    return "all_out"


def center_se(evidence: list[dict], as_of: date | None = None) -> float:
    """证据密度决定的中心估计标准误：SE² = Σ_kind σ_kind² / n_eff_kind。

    n_eff = 近 90 天证据条数（封顶 OBS_CAP——再多只是重复信息）；σ 为单条
    证据观测噪声（标定见模块注释）。证据越丰富、越一致 → SE 越小 → 预测
    区间自然收窄；没有证据时 SE=0（区间退回执行噪声先验，见 personal）。
    """
    as_of = as_of or date.today()
    lo = as_of - timedelta(days=OBS_RECENT_DAYS)
    counts: dict[str, int] = {k: 0 for k in KINDS}
    for r in evidence:
        if r["kind"] in counts and r["day"] >= lo.isoformat():
            counts[r["kind"]] += 1
    var = 0.0
    for kind in KINDS:
        n_eff = min(counts[kind], OBS_CAP[kind])
        if n_eff > 0:
            var += OBS_SIGMA[kind] ** 2 / n_eff
    return round(var ** 0.5, 3)


def _age_days(day_iso: str, as_of: date) -> float:
    try:
        return float(max((as_of - date.fromisoformat(day_iso[:10])).days, 0))
    except ValueError:
        return 0.0
