"""丹尼尔斯 VDOT 训练表（参考页数据）：完整 VDOT × 区间配速矩阵、
每区储备心率带、每区典型课。

本模块只把**已有口径**摆成一张表，不新造任何数值或文案：
- %VDOT 锚点与配速换算 → vdot.pace_table / intensity_zones（与课表、日历、
  仪表盘同一套，改一处全表跟着变）；
- 心率带 → workout_analysis 的 HR_ZONE_KINDS 边界与 LT1/LT2 锚点；
- 典型课 → workout_catalog 里真实存在的模板（名字与说明原文照搬）。
若在这里另写一份「书上抄的」数值，这张表迟早会和课表各说各话。
"""
from __future__ import annotations

from . import vdot as vd
from .workout_analysis import HR_ZONE_KINDS

VDOT_MIN, VDOT_MAX = 30, 85      # 矩阵范围（覆盖业余到高水平，Daniels 表同量级）
VDOT_DEFAULT = 40.0              # 没有任何估计可用时的中性档位（入门跑者量级）

# 配速区间 → 储备心率带（%HRR）。对应关系是「用途」层面的：恢复跑配恢复带、
# 轻松跑配低有氧带……不是逐点换算——同一个动作在不同个体/疲劳/天气下心率会
# 整档漂移，这里给的只是带的范围。所有边界都取自 HR_ZONE_KINDS，未新造数字。
# 阈值/摄氧两区天然横跨两条 HR 带（T1=78%HRR 与 T=88%HRR 两个锚点分别落在
# 高有氧带与阈值带里），故按「取并集」展示，不硬塞进一条带。
ZONE_HR_BANDS: dict[str, tuple[str, ...]] = {
    "recovery": ("recovery",),
    "easy": ("easy",),
    "aerobic": ("aerobic",),
    "threshold": ("high_aerobic", "tempo"),
    "vo2max": ("tempo", "anaerobic"),
    "anaerobic": ("anaerobic",),
}
_HR_BY_KEY = {k: (lo, hi, name) for k, lo, hi, name in HR_ZONE_KINDS}

# 各区典型课：键指向 workout_catalog 里真实存在的模板（tests 会校验键存在），
# 展示时取模板自己的名字与说明，不重写文案。顺序＝展示顺序，挑最常用的两三个。
ZONE_SESSION_KEYS: dict[str, tuple[str, ...]] = {
    "recovery": ("rec30", "rec35"),
    "easy": ("e40", "e50", "st45"),
    "aerobic": ("lr_m", "tuneup"),          # M 配速在本应用主要来自长距离 M 段与测试跑
    "threshold": ("t2x10", "t20", "t2x12"),
    "vo2max": ("i5x1000", "i4x1200"),
    "anaerobic": ("r8x200", "r6x400"),
}

# 单值配速锚点（标记字母 → %VDOT → 名称）。这些 % 就是 vdot 模块里的校准锚，
# 不在这里重新定义；「哪个锚属于哪一区」由 % 落在区间内自然决定（见 anchors）。
SINGLE_ANCHORS = (
    ("M", vd.M_PCT, "M 马拉松配速"),
    ("T1", vd.LT1_PCT, "T1 巡航阈（双阈值日上午段）"),
    ("T", vd.T_PCT, "T 乳酸阈 LT2"),
    ("I", vd.I_PCT, "I 间歇"),
    ("R", vd.R_PCT, "R 重复"),
)


def _catalog_templates() -> dict:
    """课表模板池：模块级常量 + Q_TABLES 各阶段菜单 + 长距离模板，按 key 索引。

    模板散在轮换菜单里（i5x1000 这类是 _intervals() 现造的），只能扫出来。
    """
    from . import workout_catalog as wc
    out: dict = {}
    for name in dir(wc):
        obj = getattr(wc, name)
        if isinstance(obj, wc.Template):
            out.setdefault(obj.key, obj)
    for groups in wc.Q_TABLES.values():
        for phases in groups.values():
            for lst in phases.values():
                for t in lst:
                    out.setdefault(t.key, t)
    for phase in ("base", "final"):
        for dist in (5000, 21097):
            t = wc.lr_template(phase, 0, dist)
            out.setdefault(t.key, t)
    return out


def zone_hr_band(zone_key: str) -> dict | None:
    """该配速区对应的储备心率带（%HRR 小数，0–1）。无对应返回 None。"""
    keys = ZONE_HR_BANDS.get(zone_key) or ()
    bands = [_HR_BY_KEY[k] for k in keys if k in _HR_BY_KEY]
    if not bands:
        return None
    lo = min(b[0] for b in bands)
    hi = max(b[1] for b in bands)
    return {"lo": round(lo, 3), "hi": round(hi, 3),
            "label": " + ".join(b[2] for b in bands)}


def typical_workouts(zone_key: str) -> list[dict]:
    """该区的典型课（本应用课表里真实用到的模板）：名称 + 原文说明。"""
    pool = _catalog_templates()
    out = []
    for key in ZONE_SESSION_KEYS.get(zone_key, ()):
        t = pool.get(key)
        if t:
            out.append({"key": t.key, "name": t.name, "detail": t.description})
    return out


def _anchor_owner(pct: float) -> str | None:
    """单值锚点归属哪个配速区：取最后一个下界严格小于它的区。

    M(0.82) 正好是 aerobic 的上界、又是 threshold 的下界，两边都「包含」它。
    用严格小于比较，就把落在区界上的锚点归给**较慢的那一区**（它在那里是快
    端锚点）：M→有氧跑、T1/T→阈值、I→最大摄氧量、R→无氧冲刺，每个锚点在
    表里只出现一次。若某锚点比所有区下界都小，归首区。
    """
    owner = None
    for z in vd.PACE_ZONES:
        if z["pct_lo"] < pct:
            owner = z["key"]
    return owner if owner is not None else vd.PACE_ZONES[0]["key"]


def anchors_for(vdot: float) -> dict[str, list[dict]]:
    """按区归好的单值锚点：{区key: [{mark, label, pace_s_km}, ...]}。"""
    out: dict[str, list[dict]] = {}
    for mark, pct, label in SINGLE_ANCHORS:
        key = _anchor_owner(pct)
        if key:
            out.setdefault(key, []).append(
                {"mark": mark, "label": label, "pace_s_km": round(vd.pace_s_km(vdot, pct))})
    return out


# 矩阵里附带的等效成绩列：直接用 vdot.DISTANCES 的四个常用距离（5K/10K/半马/
# 全马），与页首等效成绩、向导预览同源——加距离只改那一处。
MATRIX_RACES = tuple(vd.DISTANCES.items())


def vdot_matrix(lo: int = VDOT_MIN, hi: int = VDOT_MAX, step: int = 1) -> list[dict]:
    """完整 VDOT × 区间配速矩阵（每行一档 VDOT，配速 s/km + 代表等效成绩）。"""
    rows = []
    for v in range(int(lo), int(hi) + 1, max(int(step), 1)):
        t = vd.pace_table(float(v))
        rows.append({
            "vdot": v,
            "E": [t["E"]["slow_s_km"], t["E"]["fast_s_km"]],
            "M": t["M"], "T1": t["T1"], "T": t["T"], "I": t["I"], "R": t["R"],
            "races": {label: round(vd.predict_time(dist, float(v)))
                      for label, dist in MATRIX_RACES},
        })
    return rows


def daniels_table(vdot: float, rest_hr: float | None = None,
                  max_hr: float | None = None) -> dict:
    """整页数据：当前档区间明细（含心率带/锚点/典型课）+ 等效成绩 + 完整矩阵。

    心率带只有百分比是普适的；给了静息/最大心率才补 bpm（Karvonen 反算，
    与 workout_analysis 的 %HRR 口径一致）。
    """
    v = float(vdot)
    anchors = anchors_for(v)
    zones = []
    for z in vd.intensity_zones(v) or []:
        hr = zone_hr_band(z["key"])
        if hr and rest_hr and max_hr and max_hr > rest_hr:
            span = max_hr - rest_hr
            hr = {**hr,
                  "bpm_lo": round(rest_hr + hr["lo"] * span),
                  "bpm_hi": round(rest_hr + hr["hi"] * span)}
        zones.append({**z, "hr": hr,
                      "anchors": anchors.get(z["key"], []),
                      "sessions": typical_workouts(z["key"])})
    return {
        "vdot": round(v, 1),
        "zones": zones,
        "equivalent": vd.equivalent_times(v),
        "matrix": vdot_matrix(),
        "matrix_min": VDOT_MIN, "matrix_max": VDOT_MAX,
        "rest_hr": rest_hr, "max_hr": max_hr,
    }
