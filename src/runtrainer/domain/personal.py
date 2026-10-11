"""个人误差学习（第十九批 · 成绩预估精度升级；批33 · 不确定性重构）：
从「模型历史预测 vs 实际比赛」的误差记录里学习分量权重与按距离偏差，
并为预测区间提供真实误差基准。

历史记录条目（服务层持久化到 app_state，纯函数层只读写 list[dict]）：
  {"day": "2026-04-12", "label": "5K",
   "actual_vdot": 54.9, "pred_vdot": 55.2,
   "quality": "all_out" | "submax",        # 批33：比赛分级
   "feats": {"race": 54.9, "threshold": 52.0, ...},   # 当时各分量
   "ev_feats": {"interval": 55.1, ...}}     # 批33：当时各类训练证据（原始）
pred/actual 均为 VDOT；error = actual − pred（正 = 模型低估水平）。

学习内容全部从历史记录即时推导（不存衍生状态——可回放、可重建）：
- 分量逆误差加权：w_k ∝ 先验_k / (MAE_FLOOR + 历史平均绝对误差_k)。
  某分量在某人身上系统性跑偏（如手表读数 68 vs 比赛等效 54）就自动降权；
  误差来自该运动员自己的真实比赛，不是拍脑袋的固定修正。
- 按距离偏差校准：该距离历史误差的 EWMA（BIAS_ALPHA，±BIAS_CAP 封顶），
  应用时按样本数收缩 n/(n+BIAS_SHRINK_K)——一两场比赛翻不了盘，样本
  攒够了才学得动；学习是平滑的，不会因一场比赛跳 20–30 秒。
- 次全力比赛（quality == "submax"，训练计时/状态差）不参与任何学习——
  真实库 10K 计时 48–51 VDOT vs 同期间歇 55–58 的假误差曾把 10K 偏差与
  区间都带坏（详见 domain/evidence.py 的比赛分级）。

批33 预测区间重构（不再「多个误差重复叠加」）：
  旧口径：该距离原始误差 RMSE 向 4.0 VDOT 先验收缩——原始误差里含系统
  偏差，而偏差已被 bias_applied 单独校正过，等于把偏差计了两遍；4.0 的
  先验本身也来自带偏差模型的 RMSE。真实库 10K 区间 ±4 分钟就是这么来的。
  新口径：σ²_total = σ²_exec + SE²_center
    σ_exec = 全开比赛「去偏残差」（残差 = 误差 − 当时已应用的偏差，逐条
    时序重构）跨距离池化后的 RMSE，向执行噪声先验收缩（跨距离可比性由
    VDOT 归一化保证；某距离没比赛也能借其它距离的执行波动）。
    SE_center = 中心估计标准误（由证据密度决定，见 evidence.center_se）：
    训练证据越丰富 → SE 越小 → 区间收窄；最近数据互相矛盾（散布大）时
    中心不确定性不降反升。
  半宽 = INTERVAL_Z × σ_total（≈80% 覆盖）；无全开比赛也无证据密度数据
  时不给区间（数据不足就不装精确）。

依据（真实库 16 场真实比赛时序严格回测，tools/backtest_experiment.py）：
S0 生产口径 MAE 6.11 VDOT → S7（逆误差加权 + 校准）MAE 2.89、偏差 6.11 → 2.38；
批33 复测与区间标定见 tools/backtest_predict_v2.py。ACWR（7d/28d 跑量比）
与残差相关性 r=0.41 < 0.5，疲劳特征不进模型——【证据不足】，不硬凑。
"""
from __future__ import annotations

MAE_FLOOR = 0.5          # 逆误差加权分母下限（防单分量权重爆炸）
BIAS_ALPHA = 0.2         # 按距离偏差 EWMA 学习率（慢学习）
BIAS_CAP = 1.5           # 偏差封顶（VDOT）
BIAS_SHRINK_K = 3.0      # 偏差应用时的样本收缩：bias × n/(n+k)
INTERVAL_Z = 1.28        # 预测区间半宽系数（正态假设下 ≈80% 覆盖）
RESID_PRIOR_RMSE = 2.5   # 执行噪声先验（VDOT）：全开比赛的跨距离等效波动
#                           （批33 时序回测实现去偏残差 ~2.6 VDOT，原 1.5 过乐观
#                           → 区间覆盖仅 56%；取 2.5 使覆盖回到 ~80%，
#                           见 tools/backtest_predict_v2.py）
RESID_SHRINK_K = 4.0     # 执行噪声 RMSE 的样本收缩（向先验）
HISTORY_MAX = 40         # 历史记录上限：学习窗口够长，又不会无限增长

COMP_KINDS = ("race", "threshold", "interval", "hrr", "quality", "efforts",
              "vo2max")

# 旧记录升级：无 quality 字段时按 feats 判定次全力（比赛成绩比当时非比赛
# 分量中位低 3.0 VDOT 以上——同 evidence.RACE_SUBMAX_GAP 口径）
UPGRADE_SUBMAX_GAP = 3.0


def _sorted_entries(history: list[dict]) -> list[dict]:
    return sorted(history, key=lambda e: e.get("day") or "")


def _all_out(history: list[dict]) -> list[dict]:
    """只留全开比赛条目（次全力不参与学习——训练计时不是「比赛执行」的样本）。"""
    return [e for e in history if e.get("quality") != "submax"]


def component_mae(history: list[dict]) -> dict[str, float]:
    """每个分量的历史平均绝对误差（vs 实际比赛 VDOT，全开比赛）。

    只统计该分量在场（feats 里有值）的比赛——稀疏分量的误差估计不会
    被它缺席的比赛稀释。
    """
    errs: dict[str, list[float]] = {}
    for e in _all_out(history):
        actual = e.get("actual_vdot")
        if actual is None:
            continue
        for kind, v in (e.get("feats") or {}).items():
            if v is not None:
                errs.setdefault(kind, []).append(abs(float(v) - actual))
    return {k: sum(v) / len(v) for k, v in errs.items() if v}


def reweight(components: list[dict], history: list[dict]) -> list[dict]:
    """分量逆误差加权：w_k ∝ w_k / (MAE_FLOOR + 历史平均绝对误差_k)。

    无历史的分量保持原权重；返回新列表，不改输入。_weighted 按权重
    归一化，整体缩放会被除回去——起作用的是分量之间的相对调整。
    """
    mae = component_mae(history)
    if not mae:
        return components
    out = []
    for c in components:
        m = mae.get(c["kind"])
        if m is not None:
            c2 = dict(c)
            c2["weight"] = round(c["weight"] / (MAE_FLOOR + m), 3)
            out.append(c2)
        else:
            out.append(c)
    return out


def distance_stats(history: list[dict], label: str) -> dict:
    """该距离（全开比赛）的历史误差统计：n、EWMA 偏差、原始 RMSE 与
    去偏残差 RMSE（VDOT 单位）。

    偏差沿日期序做 EWMA（与在线更新等价——历史记录完整时推导值
    等于当时逐步更新值），封顶 BIAS_CAP。去偏残差逐条时序重构：
    resid_i = err_i − 当时已应用的偏差（收缩后的 EWMA_{i−1}）——区间
    不该再为「已被校正的系统偏差」付费（批33）。
    """
    entries = [e for e in _sorted_entries(_all_out(history))
               if e.get("label") == label and e.get("actual_vdot") is not None
               and e.get("pred_vdot") is not None]
    if not entries:
        return {"n": 0, "bias": 0.0, "rmse": None, "rmse_resid": None}
    errs = [e["actual_vdot"] - e["pred_vdot"] for e in entries]
    bias = 0.0
    resids = []
    for err in errs:
        applied = bias * len(resids) / (len(resids) + BIAS_SHRINK_K)
        resids.append(err - applied)
        bias = max(-BIAS_CAP, min(BIAS_CAP, bias + BIAS_ALPHA * (err - bias)))
    rmse = (sum(e * e for e in errs) / len(errs)) ** 0.5
    rmse_resid = (sum(r * r for r in resids) / len(resids)) ** 0.5
    return {"n": len(errs), "bias": round(bias, 2), "rmse": round(rmse, 2),
            "rmse_resid": round(rmse_resid, 2)}


def residual_stats(history: list[dict]) -> dict:
    """跨距离池化的执行波动：全开比赛去偏残差 RMSE（VDOT 归一化使不同
    距离可比——5K 的残差同样校准 10K 的区间）。某距离没有比赛时也拿得到
    区间基准（比赛少 ≠ 关于执行波动的信息少）。"""
    entries = _sorted_entries(_all_out(history))
    bias_by_label: dict[str, float] = {}
    n_by_label: dict[str, int] = {}
    resids = []
    for e in entries:
        if e.get("actual_vdot") is None or e.get("pred_vdot") is None:
            continue
        label = e.get("label") or ""
        b = bias_by_label.get(label, 0.0)
        n_l = n_by_label.get(label, 0)
        applied = b * n_l / (n_l + BIAS_SHRINK_K)
        err = e["actual_vdot"] - e["pred_vdot"]
        resids.append(err - applied)
        bias_by_label[label] = max(-BIAS_CAP, min(
            BIAS_CAP, b + BIAS_ALPHA * (err - b)))
        n_by_label[label] = n_l + 1
    if not resids:
        return {"n": 0, "rmse_resid": None}
    return {"n": len(resids),
            "rmse_resid": round((sum(r * r for r in resids) / len(resids)) ** 0.5, 2)}


def bias_applied(history: list[dict], label: str) -> float:
    """按距离偏差的应用值：EWMA 偏差按样本数收缩 n/(n+3)。"""
    s = distance_stats(history, label)
    if not s["n"]:
        return 0.0
    return round(s["bias"] * s["n"] / (s["n"] + BIAS_SHRINK_K), 2)


def interval(history: list[dict], label: str, center_se: float = 0.0) -> dict | None:
    """该距离的预测区间（半宽，VDOT）：1.28 × sqrt(σ²_exec + SE²_center)。

    σ_exec = 跨距离池化的去偏残差 RMSE 向先验收缩（全开比赛；执行噪声）。
    center_se = 中心估计标准误（evidence.center_se，训练证据密度决定）。
    无全开比赛且无证据密度数据 → None：数据不足以支持区间，就别装 ±。
    """
    s = residual_stats(history)
    if s["rmse_resid"] is None:
        if center_se <= 0:
            return None
        rmse = RESID_PRIOR_RMSE
    else:
        n = s["n"]
        rmse = ((s["rmse_resid"] ** 2 * n + RESID_PRIOR_RMSE ** 2 * RESID_SHRINK_K)
                / (n + RESID_SHRINK_K)) ** 0.5
    total = (rmse ** 2 + max(center_se, 0.0) ** 2) ** 0.5
    return {"n": distance_stats(history, label)["n"], "n_pooled": s["n"],
            "rmse": round(rmse, 2), "rmse_resid": s["rmse_resid"],
            "se_center": round(center_se, 2),
            "half_vdot": round(INTERVAL_Z * total, 2)}


def upgrade_entries(history: list[dict]) -> list[dict]:
    """旧记录升级（一次性迁移）：无 quality 字段的条目按 feats 判定——
    比赛成绩比当时非比赛分量（threshold/interval/hrr/quality/efforts，
    不含 race/vo2max）中位低 3.0 VDOT 以上 → 次全力。返回新列表。"""
    out = []
    for e in history:
        if e.get("quality"):
            out.append(e)
            continue
        e2 = dict(e)
        feats = e.get("feats") or {}
        vals = [float(v) for k, v in feats.items()
                if k not in ("race", "vo2max") and v is not None]
        if len(vals) >= 2:
            med = sorted(vals)[len(vals) // 2]
            if e.get("actual_vdot") is not None and e["actual_vdot"] < med - UPGRADE_SUBMAX_GAP:
                e2["quality"] = "submax"
            else:
                e2["quality"] = "all_out"
        else:
            e2["quality"] = "all_out"
        out.append(e2)
    return out


def record(history: list[dict], entry: dict) -> list[dict]:
    """追加一条误差记录：按 (day, label) 去重、按日期排序、裁剪上限。"""
    entries = [e for e in history
               if not (e.get("day") == entry.get("day")
                       and e.get("label") == entry.get("label"))]
    entries.append(entry)
    return _sorted_entries(entries)[-HISTORY_MAX:]
