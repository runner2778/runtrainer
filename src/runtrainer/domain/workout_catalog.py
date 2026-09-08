"""质量课模板菜单：按阶段×强度轮换（周索引取模，同阶段内不重样）。

纯数据 + 纯计算（依赖 vdot 换算距离/时长），不碰 DB/网络。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

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
    tempo_zone: str = "T"          # 主体段目标带：普通阈值 T（LT2）；双阈值上午段 T1（LT1 有氧阈）
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
# I 组间恢复≈等时长慢跑：组越长慢跑越远（800→400m、1000→500m、1200→600m、
# 1600→700m 慢跑；Seiler & Hetlelid 2005：4′组休息 1–4′ 效果相当，但组间
# 慢跑保持心率在高位是 VO2max 刺激的一部分，过长休息会掉出训练带）。
_I_REST_M = {800: 400, 1000: 500, 1200: 600, 1600: 700}


def _intervals(key, n, m):
    rest = _I_REST_M.get(m, 500)
    return _t(key, "I", f"间歇 {n}×{m}m",
              f"热身 15 分钟轻松跑 + {n}×{m}m 间歇（组间 {rest}m 慢跑恢复）+ 冷身 10 分钟。"
              "间歇段在 I 配速（约 97–100% VO2max），组间必须慢跑不能停。",
              "I", reps=((n, m),), rest_m=rest)


def _reps(key, n, m):
    return _t(key, "R", f"重复跑 {n}×{m}m", f"热身 15 分钟轻松跑 + {n}×{m}m 重复跑（组间 {400}m 慢跑）+ 冷身 10 分钟。"
               "R 配速快但距离短，练速度与经济性，组间完全恢复。", "R", reps=((n, m),))


def _tempo(key, sets, minutes, rest=2):
    name = f"阈值跑 {sets}×{minutes} 分钟" if sets > 1 else f"连续阈值跑 {minutes} 分钟"
    desc = (f"热身 15 分钟轻松跑 + {sets}×{minutes} 分钟阈值跑"
            + (f"（组间 {rest} 分钟慢跑）" if sets > 1 else "")
            + " + 冷身 10 分钟。T 配速为“舒适地费力”，提升乳酸阈值。")
    return _t(key, "T", name, desc, "T", tempo_sets=((sets, minutes),), tempo_rest_min=rest)


# ---------- 阶段轮换菜单（按目标距离差异化）----------
# base/early 全距离类相同（有氧基础、跑姿经济性先行——丹尼尔斯阶段 I/II 结构
# 一致）；transition/final/taper 按距离专项化：
#   · 5K/10K 后期保留短 I/R 速度刺激（赛前仍要 VO2max 维持与跑姿速度），
#     taper 用 200m R 保持速度；10K final 阈值课不可断（T 巡航带为成绩之本）
#   · HM/FM 后期以长 T 巡航为主（30–40′ 巡航量窗：t25/t2x12/t30），R 极少；
#     全马 final 长距离每周嵌 M 段（lr_template 单独处理），Q2 让位轻松跑
# 依据：丹尼尔斯各距离阶段配比 + 双阈值/距离专项调研（T 巡航 30–40′、短距离
# 赛季近端 VO2max 维持）。休息科学见下（I 约等时长慢跑；T 组间≈组长 1/4–1/3）。
_Q1_BASE = [_strides(40), _strides(45), _strides(50)]
_Q2_BASE = [_strides(40), _strides(45), _reps("r8x200", 8, 200)]
_Q1_EARLY = [_intervals("i6x800", 6, 800), _intervals("i5x1000", 5, 1000),
             _intervals("i4x1200", 4, 1200)]
_Q2_EARLY = [_reps("r8x200", 8, 200), _reps("r6x400", 6, 400), _reps("r10x200", 10, 200)]
_Q1_TRANS = [_tempo("t2x10", 2, 10), _tempo("t3x8", 3, 8), _tempo("t20", 1, 20)]
_Q2_TRANS = [_intervals("i4x1200", 4, 1200), _intervals("i3x1600", 3, 1600),
             _intervals("i5x1000", 5, 1000)]
_Q1_TAPER = [_tempo("t2x8", 2, 8), _reps("r6x200", 6, 200)]
# 减量期 Q2 保持低强度但轮换时长（Mujika & Padilla 2003：减量保持强度/频率、
# 只降总量）——固定 40 分钟重复整个减量期会让减量周之间毫无区分
_Q2_TAPER = [E_40, REC_35, E_30]
_Q2_FINAL = [_reps("r6x200", 6, 200), _reps("r4x300", 4, 300), _reps("r5x200", 5, 200)]
# HM 基线 final Q1：20′ 连续 + 2×12′（组间 3′，巡航规则：每 4–5′ 练 ~1′ 休）
# + 25′ 连续巡航（T 巡航 30–40′ 量窗的进阶）
_Q1_FINAL_HM = [_tempo("t20", 1, 20), _tempo("t2x12", 2, 12, rest=3), _tempo("t25", 1, 25)]

Q_TABLES = {
    "5K": {
        "q1": {"base": _Q1_BASE, "early": _Q1_EARLY, "transition": _Q1_TRANS,
               "final": [_tempo("t2x8", 2, 8), _intervals("i4x800", 4, 800),
                         _intervals("i5x600", 5, 600)],
               "taper": [_tempo("t2x8", 2, 8), _reps("r10x200", 10, 200)]},
        "q2": {"base": _Q2_BASE, "early": _Q2_EARLY, "transition": _Q2_TRANS,
               "final": [_reps("r6x200", 6, 200), _reps("r4x300", 4, 300),
                         _reps("r10x200", 10, 200)],
               "taper": _Q2_TAPER},
    },
    "10K": {
        "q1": {"base": _Q1_BASE, "early": _Q1_EARLY, "transition": _Q1_TRANS,
               "final": [_tempo("t2x10", 2, 10), _intervals("i4x1200", 4, 1200),
                         _tempo("t20", 1, 20)],
               "taper": [_tempo("t2x8", 2, 8), _reps("r10x200", 10, 200)]},
        "q2": {"base": _Q2_BASE, "early": _Q2_EARLY, "transition": _Q2_TRANS,
               "final": _Q2_FINAL, "taper": _Q2_TAPER},
    },
    "HM": {
        "q1": {"base": _Q1_BASE, "early": _Q1_EARLY, "transition": _Q1_TRANS,
               "final": list(_Q1_FINAL_HM), "taper": _Q1_TAPER},
        "q2": {"base": _Q2_BASE, "early": _Q2_EARLY, "transition": _Q2_TRANS,
               "final": _Q2_FINAL, "taper": _Q2_TAPER},
    },
    "FM": {
        "q1": {"base": _Q1_BASE, "early": _Q1_EARLY,
               "transition": [_tempo("t20", 1, 20), _tempo("t2x12", 2, 12, rest=3),
                              _tempo("t3x10", 3, 10)],
               "final": [_tempo("t2x12", 2, 12, rest=3), _tempo("t25", 1, 25),
                         _tempo("t30", 1, 30)],
               "taper": _Q1_TAPER},
        "q2": {"base": _Q2_BASE, "early": _Q2_EARLY, "transition": _Q2_TRANS,
               "final": [_tempo("t2x8", 2, 8), _reps("r6x200", 6, 200)],
               "taper": _Q2_TAPER},
    },
}

TUNEUP = _t("tuneup", "TUNEUP", "测试跑", "热身 15 分钟轻松跑 + 测试段（目标比赛配速）+ 冷身 10 分钟。"
            "赛前 2–3 周的短测试，检验状态并熟悉配速，不全力。", None, tuneup=True)

# ---------- 一天两练（挪威双阈值法）----------
# 科学依据（Seiler 80/20、Marius Bakken 创立的挪威模式）：双阈值日一天两练——
# 上午 LT1 有氧阈（文献锚 ≈2 mmol 血乳酸、70–80% HRmax、约 84% VDOT、比 T 慢
# 5–10 秒/公里）：组段长、重复少，立有氧阈的“量”；下午 LT2 乳酸阈（≈2–4.5
# mmol 带内、T 配速“舒适地费力”）：组段短、重复多，磨乳酸阈的“强度”。两练
# 间隔 4–10 小时（Talsnes 等 2024 crossover：拆分后心率漂移与 RPE 显著下降——
# 6×10′ 单场有漂移、拆两场 3×10′ 无）。
# 形态参考：Jakob Ingebrigtsen 型周 = 周二 AM 5×6′ + 周四 AM 6×5′（≤2.5 mmol），
# PM 10–12×1km / 20–25×400m（≤3.5 mmol）（来源：Marathon Handbook、shuichi-
# running、Kelemen & Tóth 2024 系统综述）。业余须缩量（精英 ~160km/周，非
# 精英跑者单段主体 ≤35 分钟）、每周 ≤1–2 个双阈值日。
# 注：「4×8′+5×5′ 经典配对」无文献出处，仅为本应用早期模板；本批起按已记录
# 形态多样化并按目标距离调节长短（全马教练版改编：AM 长段优先，PM 短组/巡航）。
_SUBT_AM_DESC = ("挪威双阈值法上午段 LT1（≈2 mmol 有氧阈，约 84% VDOT，比 T 慢 5–10 秒/公里，"
                 "心率 70–80% HRmax，体感“稳定而克制”）。热身 12 分钟轻松跑 + {body} + "
                 "冷身 8 分钟。与下午 LT2 段间隔 ≥5 小时，两段间补水补碳水。")
_SUBT_PM_DESC = ("挪威双阈值法下午段 LT2（2–4.5 mmol 乳酸阈带，T 配速“舒适地费力”，心率 80–90% "
                 "HRmax）。以轻热身为宜：热身 8 分钟轻松跑 + {body} + 冷身 8 分钟。"
                 "两段都不要上到力竭。")


def _subt_am(key, n, m, rest=1, note="") -> Template:
    """LT1 上午段分钟制模板（时长随配速缩放，主体 n×m 分钟 ≤35 安全）。"""
    body = f"{n}×{m} 分钟 LT1（组间慢跑 {rest} 分钟）"
    return _t(f"subt_am_{key}", "T1", f"双阈值·上（LT1 有氧阈 {n}×{m}′）",
              _SUBT_AM_DESC.format(body=body) + (f"（{note}）" if note else ""),
              "T1", tempo_sets=((n, m),), tempo_zone="T1", tempo_rest_min=rest,
              wu_min=12, cd_min=8)


def _subt_pm_tempo(key, n, m, rest) -> Template:
    """LT2 分钟制巡航（时长随配速缩放，低跑力也安全）。"""
    return _t(f"subt_pm_{key}", "T", f"双阈值·下（LT2 乳酸阈 {n}×{m}′）",
              _SUBT_PM_DESC.format(body=f"{n}×{m} 分钟 T 巡航（组间慢跑 {rest} 分钟）"),
              "T", tempo_sets=((n, m),), tempo_zone="T", tempo_rest_min=rest,
              wu_min=8, cd_min=8)


def _subt_pm_reps(key, n, m_m, rest_m, label, note="") -> Template:
    """LT2 距离制间歇（文献形态：1km 组慢跑约 1 分钟 / 400m 组 30–45 秒）。
    主体时长随配速变化——引擎按 VDOT 钳制 ≤35 分钟。"""
    return _t(f"subt_pm_{key}", "T", f"双阈值·下（LT2 乳酸阈 {label}）",
              _SUBT_PM_DESC.format(
                  body=f"{label}（T 配速，组间 {rest_m}m 慢跑约 {note}）"),
              "T", reps=((n, m_m),), tempo_zone="T", rest_m=rest_m,
              wu_min=8, cd_min=8)


# LT1 菜单（全部主体 ≤35′；注：4×8′ ≈ 记录形态 4×2km 的分钟制近似）——
# 按目标距离重排（全马教练版改编 AM 长段优先）。每菜单 3 形独立轮换。
SUBT_AM_MENU = {
    "5K": [_subt_am("5x6", 5, 6, 1, "Ingebrigtsen 经典周二晨段形态"),
           _subt_am("6x5", 6, 5, 1, "多组短段形态"),
           _subt_am("4x8", 4, 8, 1, "4×2km 的分钟制近似")],
    "10K": [_subt_am("5x6", 5, 6, 1, "Ingebrigtsen 经典周二晨段形态"),
            _subt_am("4x8", 4, 8, 1, "4×2km 的分钟制近似"),
            _subt_am("3x10", 3, 10, 1.5, "长段巡航形态")],
    "HM": [_subt_am("4x8", 4, 8, 1, "4×2km 的分钟制近似"),
           _subt_am("3x10", 3, 10, 1.5, "长段巡航形态"),
           _subt_am("5x6", 5, 6, 1, "Ingebrigtsen 经典周二晨段形态")],
    "FM": [_subt_am("3x10", 3, 10, 1.5, "长段巡航形态，全马改编常用"),
           _subt_am("4x8", 4, 8, 1, "4×2km 的分钟制近似"),
           _subt_am("6x5", 6, 5, 1, "多组短段形态")],
}
# LT2 菜单：分钟制巡航 2 形 + 距离制间歇 3 形（1km 组 ≈ 记录形态 10–12×1km
# 的业余缩量 6×1km；400m 组 ≈ 20–25×400m 缩量 10×400m；800m 组 ≈ 文献出现的
# 8×800m/1′ 阈值间歇）。每距离类各取 4 形、重排长短——AM 3 × PM 4 互素，
# idx 轮换 12 种组合一轮不重复。
SUBT_PM_MENU = {
    "5K": [_subt_pm_reps("10x400", 10, 400, 150, "10×400m", "30–45 秒"),
           _subt_pm_tempo("5x5", 5, 5, 1),
           _subt_pm_reps("8x800", 8, 800, 200, "8×800m", "约 1 分钟"),
           _subt_pm_reps("6x1000", 6, 1000, 200, "6×1000m", "约 1 分钟")],
    "10K": [_subt_pm_tempo("5x5", 5, 5, 1),
            _subt_pm_reps("6x1000", 6, 1000, 200, "6×1000m", "约 1 分钟"),
            _subt_pm_reps("8x800", 8, 800, 200, "8×800m", "约 1 分钟"),
            _subt_pm_reps("10x400", 10, 400, 150, "10×400m", "30–45 秒")],
    "HM": [_subt_pm_tempo("5x5", 5, 5, 1),
           _subt_pm_tempo("4x6", 4, 6, 1.5),
           _subt_pm_reps("6x1000", 6, 1000, 200, "6×1000m", "约 1 分钟"),
           _subt_pm_reps("8x800", 8, 800, 200, "8×800m", "约 1 分钟")],
    "FM": [_subt_pm_tempo("4x6", 4, 6, 1.5),   # 全马：短组×中等巡航为主
           _subt_pm_tempo("5x5", 5, 5, 1),
           _subt_pm_reps("6x1000", 6, 1000, 200, "6×1000m", "约 1 分钟"),
           _subt_pm_reps("8x800", 8, 800, 200, "8×800m", "约 1 分钟")],
}


def double_threshold_pair(cls: str, idx: int) -> tuple[Template, Template]:
    """双阈值日第 idx 天的模板对：(上午 LT1, 下午 LT2)。

    idx 递增轮换：AM 菜单长 3、PM 长 4（互素），(idx%3, idx%4) 组合周期 12、
    一轮内 12 种组合不重复。距离类各自重排：5K/10K 短组与 400m 高频多，
    HM/FM 长段/巡航多（全马改编方向）。
    """
    am_menu = SUBT_AM_MENU[cls]
    pm_menu = SUBT_PM_MENU[cls]
    return am_menu[idx % len(am_menu)], pm_menu[idx % len(pm_menu)]


def subt_main_min(t: Template, vdot_val: float) -> float:
    """主体段总时长（分钟）：分钟制段 + 距离制组按该带配速换算。"""
    main = sum(sets * minutes for sets, minutes in t.tempo_sets)
    for n, m_m in t.reps:
        main += n * m_m / 1000.0 * zone_pace(t.pace_zone or "T", vdot_val) / 60.0
    return main


def clamp_subt_main(t: Template, vdot_val: float, cap_min: float = 35.0) -> Template:
    """LT2 距离制组按跑力缩量（挪威法业余缩放）：任何跑力下主体 ≤ cap_min。

    精英 PM 形态（10–12×1km / 20–25×400m）对低跑力跑者按比例减组；分钟制段
    （tempo_sets）时长本就随配速缩放，无需处理。缩量同时改写名称/描述里的
    组数，避免「标题 6×1000m、实际 5 组」的错位。
    """
    if not t.reps or t.tempo_sets or subt_main_min(t, vdot_val) <= cap_min:
        return t
    n, m_m = t.reps[0]
    per = m_m / 1000.0 * zone_pace(t.pace_zone or "T", vdot_val) / 60.0
    k = max(4 if m_m >= 800 else 8, int(cap_min / per))
    if k >= n:
        return t
    old = f"{n}×{m_m}m"
    new = f"{k}×{m_m}m"
    return replace(t, reps=((k, m_m),),
                   name=t.name.replace(old, new),
                   description=t.description.replace(old, new))


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
    """长距离模板：final 期嵌入 M 段——半马隔周、全马每周（2Q 式：全马后段
    LR-M 是核心刺激，替代同日 Q2 强度课；半马隔周防过载）。"""
    cls = distance_class(distance_m)
    lr_m = phase == "final" and distance_m >= 21097 and \
        (pi % 2 == 0 if distance_m == 21097 else True)
    if lr_m:
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
            "E": easy_pace(vdot_val), "M": table["M"], "T1": table["T1"],
            "T": table["T"], "I": table["I"], "R": table["R"]}[zone]


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
    # tempo 主体按该课的段带配速（普通阈值课=T(LT2)；双阈值上段=T1(LT1 有氧阈)）
    tp_kpm = 60.0 / zone_pace(t.tempo_zone, vdot_val)
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
        segs.append({"type": "tempo", "zone": t.tempo_zone, "duration_min": minutes, "reps": sets,
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
