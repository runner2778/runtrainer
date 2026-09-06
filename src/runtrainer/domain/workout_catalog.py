"""质量课模板菜单：按阶段×强度轮换（周索引取模，同阶段内不重样）。

纯数据 + 纯计算（依赖 vdot 换算距离/时长），不碰 DB/网络。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import vdot as vd


@dataclass(frozen=True)
class Template:
    key: str
    kind: str                       # E/M/T/I/R/LR/RECOVERY/TUNEUP
    name: str
    description: str
    pace_zone: str | None = None    # 目标带：E/RECOVERY 存区间，M/T/I/R 存单值；TUNEUP/RACE/STRENGTH 为 None
    is_quality: bool = True
    easy_min: int = 0               # 主体轻松跑时长（E/RECOVERY）
    strides: int = 0                # 跨步跑组数（100m/组）
    reps: tuple[tuple[int, int], ...] = ()   # ((组数, 距离m), ...)，可多组不同距离
    rest_m: int = 400               # 组间慢跑恢复距离
    tempo_sets: tuple[tuple[int, int], ...] = ()  # ((组数, 分钟), ...)
    tempo_rest_min: int = 2
    wu_min: int = 15
    cd_min: int = 10
    lr: bool = False                # 长距离（距离由引擎分配）
    lr_m: bool = False              # 长距离含马拉松配速段（M 段距离由引擎分配）
    tuneup: bool = False            # 测试赛（距离由引擎分配）
    minutes: int = 0                # 固定时长课（力量训练等，无跑步公里数）


def _t(key, kind, name, desc, zone=None, **kw) -> Template:
    return Template(key=key, kind=kind, name=name, description=desc,
                    pace_zone=zone, **kw)


# ---------- E / RECOVERY ----------
E_30 = _t("e30", "E", "轻松跑 30 分钟", "保持轻松有氧，可对话强度，用于恢复与有氧基础。", "E",
          easy_min=30, wu_min=0, cd_min=0, is_quality=False)
E_40 = _t("e40", "E", "轻松跑 40 分钟", "保持轻松有氧，可对话强度，用于恢复与有氧基础。", "E",
          easy_min=40, wu_min=0, cd_min=0, is_quality=False)
E_50 = _t("e50", "E", "轻松跑 50 分钟", "保持轻松有氧，可对话强度，用于恢复与有氧基础。", "E",
          easy_min=50, wu_min=0, cd_min=0, is_quality=False)
REC_30 = _t("rec30", "RECOVERY", "恢复跑 30 分钟", "非常轻松，比 E 更慢；放松跑姿，促进恢复。", "RECOVERY",
             easy_min=30, wu_min=0, cd_min=0, is_quality=False)
REC_35 = _t("rec35", "RECOVERY", "恢复跑 35 分钟", "非常轻松，比 E 更慢；放松跑姿，促进恢复。", "RECOVERY",
             easy_min=35, wu_min=0, cd_min=0, is_quality=False)


def _strides(easy_min: int) -> Template:
    return _t(f"st{easy_min}", "E",
              f"轻松跑 {easy_min} 分钟 + 6×100m 跨步",
              f"轻松跑 {easy_min} 分钟后做 6×100m 跨步跑（约 85% 最快速度，组间充分放松），保持跑姿流畅。",
              "E", easy_min=easy_min, strides=6, wu_min=0, cd_min=0, is_quality=False)


# ---------- 质量课 ----------
def _intervals(key, n, m):
    return _t(key, "I", f"间歇 {n}×{m}m", f"热身 15 分钟轻松跑 + {n}×{m}m 间歇（组间 {400}m 慢跑恢复）+ 冷身 10 分钟。"
               "间歇段在 I 配速（约 97–100% VO2max），组间必须慢跑不能停。", "I", reps=((n, m),))


def _reps(key, n, m):
    return _t(key, "R", f"重复跑 {n}×{m}m", f"热身 15 分钟轻松跑 + {n}×{m}m 重复跑（组间 {400}m 慢跑）+ 冷身 10 分钟。"
               "R 配速快但距离短，练速度与经济性，组间完全恢复。", "R", reps=((n, m),))


def _tempo(key, sets, minutes, rest=2):
    name = f"阈值跑 {sets}×{minutes} 分钟" if sets > 1 else f"连续阈值跑 {minutes} 分钟"
    desc = (f"热身 15 分钟轻松跑 + {sets}×{minutes} 分钟阈值跑"
            + (f"（组间 {rest} 分钟慢跑）" if sets > 1 else "")
            + " + 冷身 10 分钟。T 配速为“舒适地费力”，提升乳酸阈值。")
    return _t(key, "T", name, desc, "T", tempo_sets=((sets, minutes),), tempo_rest_min=rest)


# ---------- 阶段轮换菜单 ----------
Q1_BASE = [_strides(40), _strides(45), _strides(50)]
Q2_BASE = [_strides(40), _strides(45), _reps("r8x200", 8, 200)]
Q1_EARLY = [_intervals("i6x800", 6, 800), _intervals("i5x1000", 5, 1000),
            _intervals("i4x1200", 4, 1200)]
Q2_EARLY = [_reps("r8x200", 8, 200), _reps("r6x400", 6, 400), _reps("r10x200", 10, 200)]
Q1_TRANSITION = [_tempo("t2x10", 2, 10), _tempo("t3x8", 3, 8), _tempo("t20", 1, 20)]
Q2_TRANSITION = [_intervals("i4x1200", 4, 1200), _intervals("i3x1600", 3, 1600),
                 _intervals("i5x1000", 5, 1000)]
Q1_FINAL = [_tempo("t3x8", 3, 8), _tempo("t20", 1, 20), _tempo("t2x12", 2, 12)]
Q2_FINAL = [_reps("r6x200", 6, 200), _reps("r4x300", 4, 300), _reps("r5x200", 5, 200)]
Q1_TAPER = [_tempo("t2x8", 2, 8), _reps("r6x200", 6, 200)]
Q2_TAPER = [E_40]

TUNEUP = _t("tuneup", "TUNEUP", "测试跑", "热身 15 分钟轻松跑 + 测试段（目标比赛配速）+ 冷身 10 分钟。"
            "赛前 2–3 周的短测试，检验状态并熟悉配速，不全力。", None, tuneup=True)

# ---------- 一天两练（挪威双阈值法）----------
SUBT_AM = _t("subt_am", "T", "双阈值·上（3×8' 亚阈）",
             "挪威双阈值法上午段：热身 15 分钟轻松跑 + 3×8 分钟亚阈跑（T 配速下缘，比 T 慢 3–5 秒，"
             "组间慢跑 1 分钟）+ 冷身 10 分钟。与下午段间隔 ≥5 小时，两段间注意补水补碳水。",
             "T", tempo_sets=((3, 8),), tempo_rest_min=1, wu_min=15, cd_min=10)
SUBT_PM = _t("subt_pm", "T", "双阈值·下（5×5' 亚阈）",
             "挪威双阈值法下午段：热身 10 分钟轻松跑 + 5×5 分钟亚阈跑（T 配速下缘，组间慢跑 1 分钟）"
             "+ 冷身 10 分钟。全天阈值总量约 49 分钟，两段都不要上到力竭。",
             "T", tempo_sets=((5, 5),), tempo_rest_min=1, wu_min=10, cd_min=10)
DBL_EASY = _t("dbl_easy", "RECOVERY", "放松晚跑 30 分钟（二练）",
              "高强度课后的放松晚跑：非常轻松，帮助代谢清除、促进恢复。与第一练间隔 ≥5 小时。",
              "RECOVERY", easy_min=30, wu_min=0, cd_min=0, is_quality=False)
STRENGTH = _t("strength", "STRENGTH", "力量训练 40 分钟",
              "跑步专项力量：核心 + 臀腿（深蹲、弓步、单腿硬拉、提踵、臀桥、平板支撑），"
              "每个动作 8–12 次 × 3 组，动作稳定优先于重量。",
              None, minutes=40, wu_min=0, cd_min=0, is_quality=False)

# 长距离菜单（按目标距离分级，周索引取模轮换）
LR_MENU = {
    "5K": [10, 12, 14],
    "10K": [12, 14, 16],
    "HM": [16, 18, 20, 22],
    "FM": [20, 22, 24, 26, 28, 30],
}
# 长距离含 M 段菜单：(LR 公里数, M 段公里数)，HM/FM 最终强度期隔周使用
LRM_MENU = {
    "HM": [(18, 8), (20, 10), (22, 12)],
    "FM": [(22, 12), (26, 14), (30, 16)],
}


def distance_class(distance_m: int) -> str:
    return {5000: "5K", 10000: "10K", 21097: "HM", 42195: "FM"}.get(distance_m, "10K")


def lr_template(phase: str, pi: int, distance_m: int) -> Template:
    """长距离模板：final 期 HM/FM 隔周嵌入 M 段（该周即强度长跑）。"""
    cls = distance_class(distance_m)
    if phase == "final" and distance_m >= 21097 and pi % 2 == 0:
        return _t("lr_m", "LR", "长距离（含马拉松配速段）",
                  "长距离轻松跑，中后段嵌入马拉松配速段，模拟比赛后半程。", "M",
                  lr=True, lr_m=True, wu_min=15, cd_min=10)
    return _t("lr", "LR", "长距离", "长距离轻松跑，E 配速，磨有氧耐力与脂肪供能。", "E",
              lr=True, wu_min=0, cd_min=0, is_quality=False)


# ---------- 计算 ----------
def easy_pace(vdot_val: float) -> float:
    """E 中值配速（s/km），用于轻松段的距离/时长换算。"""
    return vd.pace_s_km(vdot_val, (vd.E_LOW + vd.E_HIGH) / 2)


def zone_pace(zone: str, vdot_val: float) -> float:
    table = vd.pace_table(vdot_val)
    return {"RECOVERY": vd.pace_s_km(vdot_val, (vd.REC_LOW + vd.REC_HIGH) / 2),
            "E": easy_pace(vdot_val), "M": table["M"], "T": table["T"],
            "I": table["I"], "R": table["R"]}[zone]


def session_stats(t: Template, vdot_val: float, *, lr_km: float = 0.0,
                  m_block_km: float = 0.0, tuneup_km: float = 0.0) -> dict:
    """计算一次课的距离（km）/时长（分钟）/强度距离（hard_km）。"""
    ep = easy_pace(vdot_val)          # s/km
    easy_kpm = 60.0 / ep              # km/min（轻松配速）
    hard = 0.0
    total = 0.0
    # 热身/冷身/轻松主体/组间恢复均按 E 配速换算
    easy_min = t.wu_min + t.cd_min + t.easy_min
    for sets, minutes in t.tempo_sets:
        easy_min += (sets - 1) * t.tempo_rest_min
    total += easy_min * easy_kpm
    for n, m in t.reps:
        hard += n * m / 1000.0
        total += n * m / 1000.0 + max(0, n - 1) * t.rest_m / 1000.0
    total += t.strides * 0.1
    # tempo 主体按 T 配速
    tp_kpm = 60.0 / zone_pace("T", vdot_val)
    tempo_total_min = sum(sets * minutes for sets, minutes in t.tempo_sets)
    total += tempo_total_min * tp_kpm
    hard += tempo_total_min * tp_kpm
    if t.lr:
        total += lr_km
        hard += m_block_km
    if t.tuneup:
        total += tuneup_km
        hard += tuneup_km
    # 时长按分段配速加总
    duration = easy_min + tempo_total_min
    for n, m in t.reps:
        zp = zone_pace(t.pace_zone or "I", vdot_val)
        duration += n * m / 1000.0 * zp / 60.0 + max(0, n - 1) * t.rest_m / 1000.0 * ep / 60.0
    if t.strides:
        duration += t.strides * 0.1 * zone_pace("R", vdot_val) / 60.0
    if t.lr:
        e_km = lr_km - m_block_km
        duration += e_km * ep / 60.0
        if m_block_km > 0:
            duration += m_block_km * zone_pace("M", vdot_val) / 60.0
    if t.tuneup:
        duration += tuneup_km * zone_pace("T", vdot_val) / 60.0
    if t.minutes:
        duration += t.minutes   # 固定时长课（力量）：不计公里数与强度距离
    return {"hard_km": hard, "total_km": total, "duration_min": duration}


def _body_zone(t: Template) -> str:
    """主体轻松段的目标带：恢复课（kind=RECOVERY）落恢复带，其余落 E 带。"""
    return "RECOVERY" if t.kind == "RECOVERY" else "E"


def build_segments(t: Template, *, lr_km: float = 0.0, m_block_km: float = 0.0,
                   tuneup_km: float = 0.0, easy_min: float | None = None,
                   filler_km: float | None = None) -> list[dict]:
    """生成结构化详情段，供日历弹窗与 AI 提示词。

    filler_km：填充跑（引擎动态分配距离）用。
    """
    segs: list[dict] = []
    if t.wu_min:
        segs.append({"type": "warmup", "zone": "E", "duration_min": t.wu_min})
    if t.lr:
        if m_block_km > 0:
            segs.append({"type": "continuous", "zone": "E", "distance_km": round(lr_km - m_block_km, 1)})
            segs.append({"type": "continuous", "zone": "M", "distance_km": round(m_block_km, 1)})
        else:
            segs.append({"type": "continuous", "zone": "E", "distance_km": round(lr_km, 1)})
    elif t.tuneup:
        segs.append({"type": "continuous", "zone": "race", "distance_km": round(tuneup_km, 1)})
    elif easy_min is not None:
        # 引擎动态时长覆盖（填充跑），优先于模板自带时长
        segs.append({"type": "continuous", "zone": _body_zone(t), "duration_min": easy_min})
    elif filler_km is not None:
        segs.append({"type": "continuous", "zone": _body_zone(t), "distance_km": round(filler_km, 1)})
    elif t.minutes:
        segs.append({"type": "continuous", "zone": "strength", "duration_min": t.minutes})
    elif t.easy_min:
        segs.append({"type": "continuous", "zone": _body_zone(t), "duration_min": t.easy_min})
    for sets, minutes in t.tempo_sets:
        segs.append({"type": "tempo", "zone": "T", "duration_min": minutes, "reps": sets,
                     "rest_min": t.tempo_rest_min if sets > 1 else 0,
                     "rest_mode": "jog" if sets > 1 else None})
    for n, m in t.reps:
        # 间歇休息方式：R 重复跑要求完全恢复（走路/慢跑/静止均可）；
        # I 间歇组间必须慢跑不停
        segs.append({"type": "reps", "zone": t.pace_zone, "reps": n, "rep_m": m,
                     "rest_m": t.rest_m,
                     "rest_mode": "any" if t.pace_zone == "R" else "jog"})
    if t.strides:
        segs.append({"type": "strides", "zone": "R", "reps": t.strides, "rep_m": 100})
    if t.cd_min:
        segs.append({"type": "cooldown", "zone": "E", "duration_min": t.cd_min})
    return segs
