"""训练内容分析：从单圈/采样数据识别课表结构（间歇/休息分段）与周聚合。

纯函数层，不碰 DB/网络，独立单测。
"""
from __future__ import annotations

from datetime import date, timedelta

# 速度波动阈值：最快圈/最慢圈速度比 ≥ 1.25 判定为间歇类课程
INTERVAL_SPEED_RATIO = 1.25
# 快于整体配速 5% 记为「跑段」，慢于整体配速 5% 记为「休息段」
WORK_PACE_FACTOR = 0.95
REST_PACE_FACTOR = 1.05
# 单圈最短有效距离（米）：比这短的圈（如起终点碎圈）不做分段判断
MIN_LAP_DISTANCE_M = 100.0

# ---- 采样级结构识别（详情曲线可用时优先于圈级）----
SAMPLE_RUN_MIN_MPS = 1.5     # 跑动速度下限（低于视为走/停，不参与跑速基线）
SAMPLE_WORK_FACTOR = 1.12    # 快于基线 12% → 快跑候选
SAMPLE_WORK_MIN_S = 10.0     # 快段最短持续（秒）——冲刺训练 12~28s/组也要能识别
SAMPLE_WORK_MIN_M = 40.0     # 快段最短距离（米）
SAMPLE_MERGE_GAP_S = 15.0    # 快游程间 ≤ 此间隔视为同一快段（容忍瞬时掉速）
SAMPLE_BLOCK_GAP_S = 1200.0  # 两个快段间隔超过此值 = 两块独立提速，不算间歇课
SAMPLE_REST_FACTOR = 0.90    # 快段间区域平均速度必须 ≤ 基线×此值才算休息；
                             # 否则是匀速跑中的正常波动，不算间歇课
SAMPLE_REGION_MIN_S = 30.0   # 热身/冷身段最短持续（秒）
SAMPLE_GAP_REST_S = 10.0     # 快段间无采样间隙 ≥ 此值 = 暂停休息（静止）


def analyze_structure(laps: list[dict] = None, duration_s=None, distance_m=None,
                      samples: list[dict] = None) -> list[dict]:
    """识别训练内容结构。返回分段列表：

    - 均匀跑：[{type: "continuous", distance_m, duration_s, pace_s_km, avg_hr}]
    - 间歇课：[{type: "work"/"rest"/"recovery", distance_m, duration_s, pace_s_km, avg_hr}, ...]
    - 圈数 <2 或圈数据不足：[{type: "continuous", ...}]（用整体数据）

    laps: 规范圈 [{distance_m, elapsed_s, avg_hr, pace_s_km}]；
    samples: 详情采样 [{t_offset_s, hr, speed_mps, ...}]。有采样时优先用
    采样级识别（splitSummaries 圈太粗、自动暂停吞休息圈，巨段会被误判成
    间歇）；采样识别不出间歇即按连续跑处理，不再回退圈级粗分。
    """
    if samples:
        segs = _structure_from_samples(samples, duration_s, distance_m)
        if segs:
            return segs
        return _continuous(duration_s, distance_m, laps, samples)

    usable = [l for l in laps or []
              if l.get("distance_m") and l.get("distance_m") >= MIN_LAP_DISTANCE_M
              and l.get("elapsed_s")]
    if len(usable) < 2:
        return _continuous(duration_s, distance_m, laps, samples)

    paces = [l["pace_s_km"] for l in usable if l.get("pace_s_km")]
    if len(paces) < 2:
        return _continuous(duration_s, distance_m, laps, samples)
    fastest = min(paces)
    slowest = max(paces)
    # 间歇判定：圈间速度波动足够大
    if fastest <= 0 or slowest / fastest < INTERVAL_SPEED_RATIO:
        return _continuous(duration_s, distance_m, laps, samples)

    whole = _overall(duration_s, distance_m, laps, samples)
    whole_pace = whole.get("pace_s_km")
    if not whole_pace:
        return _continuous(duration_s, distance_m, laps, samples)
    segments = []
    for l in usable:
        p = l["pace_s_km"]
        if p is None:
            continue
        if p <= whole_pace * WORK_PACE_FACTOR:
            seg_type = "work"
        elif p >= whole_pace * REST_PACE_FACTOR:
            seg_type = "rest"
        else:
            seg_type = "work"  # 中间强度并入跑段
        segments.append({
            "type": seg_type,
            "distance_m": round(l["distance_m"]),
            "duration_s": round(l["elapsed_s"]),
            "pace_s_km": round(p, 1),
            "avg_hr": round(l["avg_hr"], 1) if l.get("avg_hr") is not None else None,
        })
    # 首尾热身/冷身慢段标 recovery（不属于主循环的 work/rest）
    if len(segments) > 1:
        if segments[0]["type"] == "rest":
            segments[0]["type"] = "recovery"
        if segments[-1]["type"] == "rest":
            segments[-1]["type"] = "recovery"
    return segments or _continuous(duration_s, distance_m, laps, samples)


def _structure_from_samples(samples, duration_s, distance_m) -> list[dict] | None:
    """采样级结构识别：速度基线 + 快窗口 → 间歇分段；不满足则返回 None。

    splitSummaries 圈粒度太粗（真实数据中单圈 2~17km），圈级速度比会把
    长距离匀速跑里的单段提速误判成间歇课；秒级采样曲线可精确切分快跑段/
    休息段，自动暂停的间隙也能识别为静止休息。
    """
    rows = sorted((s for s in samples or []
                   if s.get("t_offset_s") is not None and s.get("speed_mps")),
                  key=lambda s: s["t_offset_s"])
    if len(rows) < 60:
        return None  # 采样太短/太稀，退回匀速路径
    run_spds = [s["speed_mps"] for s in rows if s["speed_mps"] >= SAMPLE_RUN_MIN_MPS]
    if len(run_spds) < 30:
        return None
    baseline = sorted(run_spds)[len(run_spds) // 2]
    if baseline <= 0:
        return None
    n = len(rows)
    # 不做滚动平滑：2s/行的稀疏采样下，7~11 点窗口会把 12~18s 的冲刺段
    # 衰减到阈值以下；单点 GPS 毛刺（1~3 个采样）由 SAMPLE_WORK_MIN_S 过滤
    fast = [s["speed_mps"] >= baseline * SAMPLE_WORK_FACTOR for s in rows]
    # 快采样游程 → 合并间隔 ≤15s 的相邻游程（快段内部短暂掉速）
    runs = []
    i = 0
    while i < n:
        if fast[i]:
            j = i
            # 游程不跨采样间隙：自动暂停处两快段间没有非快采样行，
            # 只按 fast 标志会把两段并成一段
            while (j + 1 < n and fast[j + 1]
                   and rows[j + 1]["t_offset_s"] - rows[j]["t_offset_s"] <= SAMPLE_MERGE_GAP_S):
                j += 1
            runs.append([i, j])
            i = j + 1
        else:
            i += 1
    merged = []
    for r in runs:
        if merged and rows[r[0]]["t_offset_s"] - rows[merged[-1][1]]["t_offset_s"] <= SAMPLE_MERGE_GAP_S:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    works = []
    for lo, hi in merged:
        seg = _seg_from_rows(rows, lo, hi)
        if ((seg["duration_s"] or 0) >= SAMPLE_WORK_MIN_S
                and (seg["distance_m"] or 0) >= SAMPLE_WORK_MIN_M):
            works.append((lo, hi, seg))
    if len(works) < 2:
        return None  # 单段提速不是间歇课
    between_spds = []
    for (a_lo, a_hi, _), (b_lo, b_hi, _) in zip(works, works[1:]):
        if rows[b_lo]["t_offset_s"] - rows[a_hi]["t_offset_s"] > SAMPLE_BLOCK_GAP_S:
            return None  # 两块相距过远的提速是两次独立安排，不算间歇
        between = _seg_from_rows(rows, a_hi + 1, b_lo - 1)
        if between["distance_m"] > 0 and between["duration_s"] > 0:
            between_spds.append(between["distance_m"] / between["duration_s"])
    if between_spds:
        # 快段间区域的速度中位数必须真慢（走路/慢跑）才算间歇课的休息；
        # 匀速跑中的正常波动（如缓下坡提速）之间仍接近基线 → 不算间歇。
        # 用中位数而非全部：热身段的快慢波动混入时仍能识别出主间歇块
        med = sorted(between_spds)[len(between_spds) // 2]
        if med > baseline * SAMPLE_REST_FACTOR:
            return None
    # 组装：首快段前=recovery，快段间=rest（含暂停间隙），末快段后=recovery
    segments = []
    prev_hi = -1
    for k, (lo, hi, seg) in enumerate(works):
        if k == 0:
            before = _seg_from_rows(rows, 0, lo - 1)
            if before["duration_s"] >= SAMPLE_REGION_MIN_S:
                segments.append({**before, "type": "recovery"})
        else:
            gap = rows[lo]["t_offset_s"] - rows[prev_hi]["t_offset_s"]
            between = _seg_from_rows(rows, prev_hi + 1, lo - 1)
            if between["duration_s"] < 1 and gap >= SAMPLE_GAP_REST_S:
                # 自动暂停：两快段间没有任何采样 → 静止休息
                between = {"distance_m": 0.0, "duration_s": round(gap, 1),
                           "pace_s_km": None, "avg_hr": None}
            if between["duration_s"] >= 1:
                segments.append({**between, "type": "rest"})
        segments.append({**seg, "type": "work"})
        prev_hi = hi
    after = _seg_from_rows(rows, prev_hi + 1, n - 1)
    if after["duration_s"] >= SAMPLE_REGION_MIN_S:
        segments.append({**after, "type": "recovery"})
    return segments


def _seg_from_rows(rows, lo, hi) -> dict:
    """rows[lo..hi]（含）合成段：距离积分（梯形 dt）、时长、配速、平均心率。"""
    r = rows[lo:hi + 1]
    if not r:
        return {"distance_m": 0.0, "duration_s": 0.0, "pace_s_km": None, "avg_hr": None}
    dist = 0.0
    for i, s in enumerate(r):
        if i == 0:
            dt = (r[1]["t_offset_s"] - r[0]["t_offset_s"]) / 2 if len(r) > 1 else 0
        elif i == len(r) - 1:
            dt = (r[i]["t_offset_s"] - r[i - 1]["t_offset_s"]) / 2
        else:
            dt = (r[i + 1]["t_offset_s"] - r[i - 1]["t_offset_s"]) / 2
        dist += s["speed_mps"] * dt
    dur = r[-1]["t_offset_s"] - r[0]["t_offset_s"]
    hrs = [s.get("hr") for s in r if s.get("hr")]
    return {"distance_m": round(dist, 1),
            "duration_s": round(dur, 1),
            "pace_s_km": round(dur / (dist / 1000.0), 1) if dist > 1 else None,
            "avg_hr": round(sum(hrs) / len(hrs), 1) if hrs else None}


def _continuous(duration_s, distance_m, laps, samples=None) -> list[dict]:
    whole = _overall(duration_s, distance_m, laps, samples)
    if not whole.get("duration_s") and not whole.get("distance_m"):
        return []
    return [whole]


def _overall(duration_s, distance_m, laps, samples=None) -> dict:
    """整体摘要：优先整体字段，缺项时依次用采样曲线/圈数据补算。"""
    dur = duration_s
    dist = distance_m
    hr = None
    if (not dur or not dist) and samples:
        rows = sorted((s for s in samples
                       if s.get("t_offset_s") is not None and s.get("speed_mps")),
                      key=lambda s: s["t_offset_s"])
        if rows:
            seg = _seg_from_rows(rows, 0, len(rows) - 1)
            dur = dur or seg["duration_s"]
            dist = dist or seg["distance_m"]
    if (not dur or not dist) and laps:
        laps_ok = [l for l in laps if l.get("elapsed_s")]
        dur = sum(l.get("elapsed_s") or 0 for l in laps_ok) or dur
        dist = sum(l.get("distance_m") or 0 for l in laps_ok) or dist
    if samples:
        hrs = [s.get("hr") for s in samples if s.get("hr")]
        if hrs:
            hr = round(sum(hrs) / len(hrs), 1)
    if hr is None and laps:
        hrs = [l.get("avg_hr") for l in laps if l.get("avg_hr")]
        if hrs:
            hr = round(sum(hrs) / len(hrs), 1)
    pace = None
    if dur and dist and dist > 0:
        pace = round(dur / (dist / 1000.0), 1)
    return {"type": "continuous", "distance_m": round(dist) if dist else None,
            "duration_s": round(dur) if dur else None, "pace_s_km": pace, "avg_hr": hr}


def summarize_structure(segments: list[dict]) -> dict:
    """分段结构的人类可读摘要（活动列表/详情展示用）。"""
    if not segments:
        return {"label": "", "kind": "unknown", "work_segments": 0, "rest_segments": 0}
    types = [s["type"] for s in segments]
    if "rest" in types:
        works = [s for s in segments if s["type"] == "work"]
        rests = [s for s in segments if s["type"] == "rest"]
        work_km = sum((s.get("distance_m") or 0) for s in works) / 1000
        return {
            "label": f"间歇：{len(works)} 组跑段共 {work_km:.1f} km，{len(rests)} 段休息",
            "kind": "interval",
            "work_segments": len(works),
            "rest_segments": len(rests),
        }
    pace = segments[0].get("pace_s_km")
    return {"label": "匀速跑", "kind": "continuous",
            "work_segments": 0, "rest_segments": 0, "pace_s_km": pace}


HR_VALID_MIN = 90.0      # 心率有效性钳制：腕式脱落/误录的平均心率不参与对照
HR_VALID_MAX = 220.0
FAST_PACE_S = 240.0      # 4:00/km：更快配速必须伴随足够心率，否则视为 GPS 漂移
FAST_PACE_HR_MIN = 130.0


def _valid_pace_hr(a: dict) -> bool:
    """一行活动是否可参与配速-心率对照（无效数据排除）：

    - 有配速/心率/时间戳
    - 心率 90–220（腕式传感器脱落/误录剔除）
    - max_hr 与 avg_hr 自洽（与课程分类同一套传感器垃圾保护）
    - 快于 4:00/km 的配速要求心率 ≥130（GPS 漂移假快配速心率异常低）
    """
    p = a.get("avg_pace_s_km")
    hr = a.get("avg_hr")
    if not p or not hr or not a.get("start_ts"):
        return False
    if not (HR_VALID_MIN <= hr <= HR_VALID_MAX):
        return False
    mx = a.get("max_hr")
    if mx and (mx < 120 or mx < hr):
        return False
    if p < FAST_PACE_S and hr < FAST_PACE_HR_MIN:
        return False
    return True


def weekly_pace_hr(activities: list[dict], start: date, end: date) -> list[dict]:
    """各周平均配速与平均心率（ISO 周聚合），供「配速-心率变化曲线」。

    activities: 含 start_ts/avg_pace_s_km/avg_hr/distance_m 的活动（任意顺序）。
    返回按周降序（最新在前）：[{week_start, avg_pace_s_km, avg_hr, runs,
    distance_km}]，仅包含有效数据（_valid_pace_hr）的活动周。
    """
    from ..utils import dates as dutil
    buckets: dict[str, list[dict]] = {}
    for a in activities:
        if not _valid_pace_hr(a):
            continue
        day = dutil.ts_to_date(a["start_ts"])
        if day < start or day > end:
            continue
        week_start = day - timedelta(days=day.weekday())
        buckets.setdefault(week_start.isoformat(), []).append(a)
    out = []
    for week in sorted(buckets, reverse=True):
        rows = buckets[week]
        # 距离加权平均配速与平均心率（同口径）：10km 长跑与 3km 放松跑
        # 不该等权——短慢跑会把周平均心率/配速拉向错误方向
        dists = [(a.get("distance_m") or 0) for a in rows]
        total_dist = sum(dists)
        wsum = sum(d or 1 for d in dists)
        pace = (sum((a["avg_pace_s_km"] or 0) * (d or 1) for a, d in zip(rows, dists))
                / wsum) if rows else None
        out.append({
            "week_start": week,
            "avg_pace_s_km": round(pace, 1) if pace else None,
            "avg_hr": round(sum((a["avg_hr"] or 0) * (d or 1)
                                for a, d in zip(rows, dists)) / wsum, 1) if rows else None,
            "runs": len(rows),
            "distance_km": round(total_dist / 1000, 1),
        })
    return out


PACE_BIN_S = 30          # 配速梯度档宽（s/km）
PACE_BIN_MIN_S = 180     # 3:00/km：更快的是坏数据/短冲刺测试，不参与对照
PACE_BIN_MAX_S = 900     # 15:00/km：更慢的不是跑步（走路/通勤），不参与对照
MIN_BIN_RUNS = 2         # 每档每期至少 2 次才出点（单次跑噪声太大）

# ---- 课程分类 ----
SPRINT_MAX_DIST_M = 500.0    # ≤500m 的跑段才可能算冲刺段（R 距离区间）
SPRINT_PACE_FACTOR = 0.85    # 快于整体配速 15% 才算冲刺（相对强度）
WALK_MIN_PACE_S_KM = 480.0   # 休息段慢于 8:00/km = 走路，否则慢跑
# 心率区 → 课程类别；由轻到重。有静息心率时按 %HRR（储备心率）解释，
# 否则按 %HRmax。区带以两个乳酸阈值为生理界标（第十四批整改细化）：
#   LT1（≈78% HRR）以下 = 纯有氧（恢复 + 低/中有氧）；
#   LT1 ~ LT2（≈88% HRR）= 高强度有氧（耐力上限建设区）；
#   ≥ LT2 = 阈值与无氧（质量训练区）。
# 低/中/高有氧的细分依据：LT1 线下是「轻松跑得动」的基础有氧，线上到
# LT2 是高强度有氧（长时间维持会快速累积疲劳的顶格有氧）。
# recovery 线取 60%（Z1 顶）：训练有素者恢复跑通常压在 65%HRmax 以下，
# 62%HRR 会把轻松慢跑（用户实测 121-137）误划成恢复。
LT1_HRR = 0.78
LT2_HRR = 0.88
HR_ZONE_KINDS = [
    ("recovery", 0.0, 0.60, "恢复跑"),
    ("easy", 0.60, 0.70, "低有氧跑"),
    ("aerobic", 0.70, LT1_HRR, "中有氧跑"),
    ("high_aerobic", LT1_HRR, LT2_HRR, "高有氧跑"),
    ("tempo", LT2_HRR, 0.96, "阈值跑"),
    ("anaerobic", 0.96, 1.0, "无氧/冲刺"),
]
# 区带公开元数据（供前端画「本次训练相对 LT1/LT2 的位置」条带图）
ZONE_META = [{"key": k, "name": n, "lo": lo, "hi": hi}
             for k, lo, hi, n in HR_ZONE_KINDS]
SEG_KIND_LABELS = {
    "sprint": "冲刺段", "fast": "快跑段", "walk": "休息·走路",
    "jog": "休息·慢跑", "stand": "休息·静止", "warmup": "热身/冷身",
}


def estimate_max_hr(birth_year: int | None) -> float | None:
    """无手表/手动 max_hr 时的经验估计（Tanaka 公式）。"""
    if not birth_year:
        return None
    age = max(0, date.today().year - birth_year)
    return round(208 - 0.7 * age, 1)


def infer_max_hr(peaks: list[float], min_n: int = 20) -> dict | None:
    """从活动采样峰值推断真实最大心率（比年龄公式准一个量级）。

    peaks: 每条活动的采样最大心率（一次训练可能摸不到 max_hr，
    但多活动峰值的前 5 均值是公认的稳健估计，单次腕式心率毛刺被稀释）。
    返回 {value, n, p99, top5_mean}；样本不足或数值离群（<160 / >220）
    时返回 None（宁缺毋滥，不拿垃圾数据覆盖档案）。
    """
    peaks = [p for p in peaks if p and 60 <= p <= 220]
    if len(peaks) < min_n:
        return None
    ps = sorted(peaks, reverse=True)
    top5 = ps[:5]
    value = round(sum(top5) / len(top5), 1)
    if value < 160:
        return None
    return {"value": value, "n": len(ps),
            "p99": ps[max(0, int(len(ps) * 0.01))],
            "top5_mean": value}


def pace_bin_hr(activities: list[dict], start: date, end: date,
                bin_s: int = PACE_BIN_S, periods: int | None = None) -> dict:
    """同配速不同时期的平均心率对照：配速按 bin_s 梯度分桶 × 时期等宽切分。

    目的：直接回答「同一配速下，不同时期的平均心率是多少」——
    同一配速档心率更低 = 有氧能力进步。比「周平均配速 vs 周平均心率」
    更直观（后者两条轴都在变，无法对照）。

    activities: 含 start_ts/avg_pace_s_km/avg_hr/distance_m 的活动。
    返回 {bins: [{start_s, end_s}], periods: [{label, start, end,
           hr: [每档心率或 None], runs: [次数], distance_km: [跑量]}]}，
    hr/runs/distance_km 与 bins 对齐；不足 MIN_BIN_RUNS 次的格子为 None。
    """
    from ..utils import dates as dutil
    rows = []
    for a in activities:
        if not _valid_pace_hr(a):
            continue  # 无效数据排除：心率离群/传感器垃圾/GPS 漂移假快配速
        p = a["avg_pace_s_km"]
        if p < PACE_BIN_MIN_S or p > PACE_BIN_MAX_S:
            continue  # 钳制：只对照真实跑步配速，垃圾值不撑爆坐标轴
        day = dutil.ts_to_date(a["start_ts"])
        if day < start or day > end:
            continue
        rows.append((day, a))
    if not rows:
        return {"bins": [], "periods": []}

    days = (end - start).days + 1
    n_periods = periods or max(2, min(6, round(days / 40)))
    per = days / n_periods
    # 时期边界（含起含止）：第 i 期 = [start + i*per, start + (i+1)*per - 1]
    p_start = [start + timedelta(days=int(i * per)) for i in range(n_periods)]
    p_end = [start + timedelta(days=int((i + 1) * per) - 1) for i in range(n_periods)]
    p_end[-1] = end

    # 先按「时期 × 配速档」聚合（档位用起始秒数做键）
    # cells: (period_idx, bin_start) -> [dist_sum, hr_dist_wsum, runs]
    cells: dict[tuple[int, int], list[float]] = {}
    for day, a in rows:
        pi = min(int((day - start).days / per), n_periods - 1)
        b = int(a["avg_pace_s_km"] // bin_s) * bin_s
        dist = a.get("distance_m") or 1
        cell = cells.setdefault((pi, b), [0.0, 0.0, 0])
        cell[0] += dist
        cell[1] += a["avg_hr"] * dist
        cell[2] += 1

    # 坐标轴只覆盖「可见档」：某个时期达到 MIN_BIN_RUNS 的档位。
    # 各档每个时期都只有 1 次跑步的散步档不参与定轴，避免撑爆横轴。
    visible = sorted({b for (pi, b), c in cells.items() if c[2] >= MIN_BIN_RUNS})
    if not visible:
        return {"bins": [], "periods": []}
    lo, hi = visible[0], visible[-1]
    bin_starts = list(range(lo, hi + bin_s, bin_s))

    periods_out = []
    for pi in range(n_periods):
        hr_arr, runs_arr, dist_arr = [], [], []
        for b in bin_starts:
            cell = cells.get((pi, b))
            if cell and cell[2] >= MIN_BIN_RUNS:
                hr_arr.append(round(cell[1] / cell[0], 1))
                runs_arr.append(cell[2])
                dist_arr.append(round(cell[0] / 1000, 1))
            else:
                hr_arr.append(None)
                runs_arr.append(0 if not cell else int(cell[2]))
                dist_arr.append(0.0 if not cell else round(cell[0] / 1000, 1))
        periods_out.append({
            # 短标签带年份（YY/MM）：一年窗口跨两个年份，纯 MM-DD 有歧义；
            # 长格式 6 项会让图例换行压住 x 轴
            "label": f"{p_start[pi].year % 100:02d}/{p_start[pi].month:02d}",
            "start": p_start[pi].isoformat(), "end": p_end[pi].isoformat(),
            "hr": hr_arr, "runs": runs_arr, "distance_km": dist_arr,
        })
    # 没有有效数据点（无档位达到 MIN_BIN_RUNS）的时期整行是死线，
    # 图例里徒增噪声（真实数据：一年里前 3 期零跑步）→ 丢弃
    periods_out = [p for p in periods_out if max(p["runs"]) >= MIN_BIN_RUNS]
    # 最新在前：对照从最近的时期往回看，最近一条加粗高亮最直观
    periods_out.reverse()
    return {
        "bins": [{"start_s": b, "end_s": b + bin_s} for b in bin_starts],
        "periods": periods_out,
        "summary": _pace_hr_summary(periods_out, bin_starts),
    }


def _pace_hr_summary(periods: list[dict], bin_starts: list[int]) -> dict:
    """对照摘要：最近期 vs 最初期在同一配速档的平均心率差。

    periods 最新在前（periods[0]=最近）。只对比首尾两期都达到
    MIN_BIN_RUNS 的档；取两期跑量合计最大的档作为代表档（最常跑的
    配速档，避免单次极端档——如 GPS 漂移假快配速——带偏头条结论）。"""
    if len(periods) < 2:
        return {"note": "", "best_drop": None, "best_label": None}
    latest, earliest = periods[0], periods[-1]
    best: tuple[float, int] | None = None
    for bi, b in enumerate(bin_starts):
        l, e = latest["hr"][bi], earliest["hr"][bi]
        if l is None or e is None:
            continue
        w = latest["runs"][bi] + earliest["runs"][bi]
        drop = l - e
        if best is None or w > best[1]:
            best = (drop, w, b)
    if best is None:
        return {"note": "首尾时期缺少同一配速档的对照数据", "best_drop": None, "best_label": None}
    drop, _w, b = best
    m, sec = divmod(b, 60)
    label = f"{m}:{sec:02d}"
    if drop <= -3:
        return {"note": f"同一配速（{label}/km）下平均心率较最初时期下降 {abs(drop):.0f} bpm"
                        f"——有氧能力进步",
                "best_drop": round(drop, 1), "best_label": label}
    if drop >= 3:
        return {"note": f"同一配速（{label}/km）下平均心率较最初时期上升 {drop:.0f} bpm"
                        f"——注意疲劳/恢复不足或天气影响",
                "best_drop": round(drop, 1), "best_label": label}
    return {"note": "同一配速下平均心率与最初时期基本持平（±3 bpm 内）",
            "best_drop": round(drop, 1), "best_label": label}


def _seg_kind(seg: dict, whole_pace: float | None) -> str | None:
    """单段细分：work → 冲刺段/快跑段；rest → 走路/慢跑/静止；recovery → 热身冷身。"""
    t = seg.get("type")
    if t == "recovery":
        return "warmup"
    if t == "rest":
        p = seg.get("pace_s_km")
        if not seg.get("distance_m") or not p:
            return "stand"
        return "walk" if p >= WALK_MIN_PACE_S_KM else "jog"
    if t == "work":
        p = seg.get("pace_s_km")
        dist = seg.get("distance_m") or 0
        if (whole_pace and p and dist <= SPRINT_MAX_DIST_M
                and p <= whole_pace * SPRINT_PACE_FACTOR):
            return "sprint"
        return "fast"
    return None


def classify_workout(segments: list[dict], duration_s=None, distance_m=None,
                     avg_hr=None, max_hr=None, rest_hr=None) -> dict:
    """课程级识别（不止单一平均配速）：
    - 间歇跑：跑段细分快跑段/冲刺段，休息段细分走路/慢跑/静止
    - 重复跑：只有跑段没有可识别休息段（自动暂停切掉了休息圈）
    - 匀速跑按心率区归类：恢复/低有氧/中有氧/高有氧（LT1 界）/阈值（LT2 界）/
      无氧冲刺 六带（HR_ZONE_KINDS，带 LT1/LT2 生理锚点）。
      静息心率已知时用 Karvonen 储备心率（%HRR）——训练有素者储备大，
      用 %HRmax 会把「基础有氧」课（实测 73-77%HRmax）整体误升一档；
      rest_hr 缺失时回退 %HRmax。

    返回 {kind, label, work: {fast, sprint}, rest: {walk, jog, stand},
          hr_pct, hr_metric: "hrr"|"hrmax"|None, lt1, lt2,
          zones: [区带元数据（前端画强度条带）],
          seg_kinds: [每段细分]（与 segments 对齐）}。
    """
    segs = segments or []
    whole_pace = _overall(duration_s, distance_m, segs).get("pace_s_km")
    seg_kinds = [_seg_kind(s, whole_pace) for s in segs]
    work_n = sum(1 for s in segs if s.get("type") == "work")
    rest_n = sum(1 for s in segs if s.get("type") == "rest")
    work_kinds = [k for k in seg_kinds if k in ("fast", "sprint")]
    rest_kinds = [k for k in seg_kinds if k in ("walk", "jog", "stand")]
    # 传感器异常保护：max_hr < 120 或 max_hr < avg_hr（不可能组合）说明
    # 心率数据不可信（腕式传感器脱落/误录），不参与心率区归类，否则
    # 一条 avg=52 的垃圾记录会被判成「恢复跑」污染统计
    if max_hr and (max_hr < 120 or (avg_hr and max_hr < avg_hr)):
        avg_hr = None
    hr_pct = None
    hr_metric = None
    if avg_hr and max_hr:
        if rest_hr and max_hr > rest_hr and avg_hr > rest_hr:
            # Karvonen 储备心率
            hr_pct = round((avg_hr - rest_hr) / (max_hr - rest_hr), 3)
            hr_metric = "hrr"
        else:
            hr_pct = round(avg_hr / max_hr, 3)
            hr_metric = "hrmax"

    def _counts(kinds):
        return {k: kinds.count(k) for k in dict.fromkeys(kinds)}

    if work_n and rest_n:
        wc = _counts(work_kinds)
        label = f"间歇跑：{work_n} 组跑段"
        if wc.get("sprint"):
            label += f"（冲刺段 ×{wc['sprint']}）"
        if wc.get("fast"):
            label += f"（快跑段 ×{wc['fast']}）"
        rc = _counts(rest_kinds)
        label += f"，休息 {rest_n} 段"
        if rc:
            names = {"walk": "走路", "jog": "慢跑", "stand": "静止"}
            label += "（" + "/".join(names[k] for k in rc) + "）"
        return {"kind": "interval", "label": label,
                "work": {"fast": wc.get("fast", 0), "sprint": wc.get("sprint", 0)},
                "rest": {"walk": rc.get("walk", 0), "jog": rc.get("jog", 0),
                         "stand": rc.get("stand", 0)},
                "hr_pct": hr_pct, "hr_metric": hr_metric,
                "lt1": LT1_HRR, "lt2": LT2_HRR, "zones": ZONE_META,
                "seg_kinds": seg_kinds}

    if work_n >= 2:
        # 只有快段没有休息段：间歇课但休息圈被自动暂停吞掉（Garmin 常见）
        wc = _counts(work_kinds)
        label = f"重复跑：{work_n} 组"
        if wc.get("sprint"):
            label += f"（冲刺段 ×{wc['sprint']}）"
        if wc.get("fast"):
            label += f"（快跑段 ×{wc['fast']}）"
        label += "，休息未记录（自动暂停）"
        return {"kind": "repeats", "label": label,
                "work": {"fast": wc.get("fast", 0), "sprint": wc.get("sprint", 0)},
                "rest": {"walk": 0, "jog": 0, "stand": 0},
                "hr_pct": hr_pct, "hr_metric": hr_metric,
                "lt1": LT1_HRR, "lt2": LT2_HRR, "zones": ZONE_META,
                "seg_kinds": seg_kinds}

    # 匀速跑：按心率区归类；缺心率/缺 max_hr 时无法归类
    kind, label = "unknown", "匀速跑（缺心率数据）"
    if hr_pct is not None:
        for k, lo, hi, name in HR_ZONE_KINDS:
            if lo <= hr_pct < hi:
                kind, label = k, name
                break
    return {"kind": kind, "label": label,
            "work": {"fast": 0, "sprint": 0},
            "rest": {"walk": 0, "jog": 0, "stand": 0},
            "hr_pct": hr_pct, "hr_metric": hr_metric,
            "lt1": LT1_HRR, "lt2": LT2_HRR, "zones": ZONE_META,
            "seg_kinds": seg_kinds}
