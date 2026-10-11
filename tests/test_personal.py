"""个人误差学习（第十九批）：分量逆误差加权 / 按距离偏差校准 / 预测区间 /
历史记录。纯函数层——从历史记录即时推导，不存衍生状态。"""
from __future__ import annotations

from runtrainer.domain import personal


HIST = [
    {"day": "2026-01-01", "label": "5K", "actual_vdot": 50.0, "pred_vdot": 49.0,
     "feats": {"race": 50.0, "threshold": 48.0, "vo2max": 60.0}},
    {"day": "2026-02-01", "label": "5K", "actual_vdot": 51.0, "pred_vdot": 50.0,
     "feats": {"race": 51.0, "threshold": 49.0, "vo2max": 60.0}},
]


def test_component_mae_only_counts_present_components():
    """分量误差只统计该分量在场的比赛——稀疏分量的误差不会被缺席稀释。"""
    mae = personal.component_mae(HIST)
    assert abs(mae["race"] - 0.0) < 1e-9
    assert abs(mae["threshold"] - 2.0) < 1e-9
    assert abs(mae["vo2max"] - 9.5) < 1e-9


def test_reweight_downgrades_errorprone_component():
    """逆误差加权：历史误差大的分量相对权重降得更多（race 无误差 → 相对
    上升；手表读数误差 9.5 → 相对大降）。不改输入列表。"""
    components = [
        {"kind": "race", "vdot": 50.0, "weight": 0.28},
        {"kind": "threshold", "vdot": 48.0, "weight": 0.22},
        {"kind": "vo2max", "vdot": 60.0, "weight": 0.16},
    ]
    out = personal.reweight(components, HIST)
    assert out is not components
    w = {c["kind"]: c["weight"] for c in out}
    # race: 0.28/(0.5+0.0)=0.56；threshold: 0.22/2.5=0.088；vo2max: 0.16/10=0.016
    assert abs(w["race"] - 0.56) < 1e-9
    assert abs(w["threshold"] - 0.088) < 1e-9
    assert abs(w["vo2max"] - 0.016) < 1e-9
    # 相对调整方向：race 对 threshold 的权重比 0.28/0.22 → 0.56/0.088
    assert w["race"] / w["threshold"] > components[0]["weight"] / components[1]["weight"]


def test_reweight_without_history_returns_unchanged():
    """无历史（或历史里没有分量记录）→ 不调整，返回原列表。"""
    comps = [{"kind": "race", "vdot": 50.0, "weight": 0.28}]
    assert personal.reweight(comps, []) == comps
    assert personal.reweight(comps, [{"day": "2026-01-01", "label": "5K",
                                      "actual_vdot": 50.0, "feats": {}}]) == comps


def test_distance_stats_ewma_bias_in_date_order_and_capped():
    """偏差沿日期序做 EWMA（α=0.2），与逐步在线更新等价；封顶 ±1.5。"""
    h = [
        {"day": "2026-04-01", "label": "10K", "actual_vdot": 51.0,
         "pred_vdot": 49.0},                      # err +2.0（后发生）
        {"day": "2026-03-01", "label": "10K", "actual_vdot": 50.0,
         "pred_vdot": 52.0},                      # err −2.0（先发生）
    ]
    s = personal.distance_stats(h, "10K")
    # EWMA：0 → 0.2×(−2) = −0.4 → −0.4 + 0.2×(2−(−0.4)) = 0.08
    assert s["n"] == 2
    assert abs(s["bias"] - 0.08) < 1e-9
    assert abs(s["rmse"] - 2.0) < 1e-9
    # 大误差封顶
    big = [{"day": "2026-05-01", "label": "10K", "actual_vdot": 60.0,
            "pred_vdot": 50.0}]
    assert personal.distance_stats(big, "10K")["bias"] == 1.5


def test_bias_applied_shrinks_with_small_n():
    """应用偏差按样本数收缩 n/(n+3)：一两场比赛翻不了盘。"""
    assert personal.bias_applied(HIST, "10K") == 0.0   # 该距离无历史 → 0
    h = [{"day": "2026-05-01", "label": "10K", "actual_vdot": 60.0,
          "pred_vdot": 50.0}]
    # bias 封顶 1.5 → 应用 1.5×1/4 = 0.375 → 0.38
    assert abs(personal.bias_applied(h, "10K") - 0.38) < 1e-9
    assert personal.bias_applied([], "10K") == 0.0


def test_interval_shrinks_residuals_toward_execution_prior():
    """预测区间（批33）：无历史 → None；有全开比赛时半宽 = 1.28 × 向执行
    噪声先验收缩的跨距离去偏残差 RMSE（小样本不装宽区间——先验 2.5 VDOT
    按回测实现去偏残差标定，覆盖回到 ~80%）。"""
    assert personal.interval([], "5K") is None
    h = [{"day": "2026-03-01", "label": "半马", "actual_vdot": 52.0,
          "pred_vdot": 50.0}]                     # err +2.0，去偏残差 2.0，n=1
    iv = personal.interval(h, "半马")
    # 收缩：sqrt((2²×1 + 2.5²×4)/5) = sqrt(5.8) ≈ 2.41；半宽 1.28×2.41 ≈ 3.08
    assert iv["n"] == 1
    assert abs(iv["rmse"] - 2.41) < 0.01
    assert abs(iv["half_vdot"] - 3.08) < 0.01


def test_record_dedupes_sorts_and_trims():
    """追加记录：(day, label) 去重、按日期排序、裁剪到 HISTORY_MAX。"""
    h = personal.record([], {"day": "2026-05-01", "label": "5K",
                             "actual_vdot": 50.0, "pred_vdot": 50.0})
    h = personal.record(h, {"day": "2026-05-01", "label": "5K",
                            "actual_vdot": 49.0, "pred_vdot": 50.0})
    h = personal.record(h, {"day": "2026-04-01", "label": "5K",
                            "actual_vdot": 48.0, "pred_vdot": 50.0})
    assert [e["day"] for e in h] == ["2026-04-01", "2026-05-01"]
    assert h[1]["actual_vdot"] == 49.0              # 同日同距离被替换
    # 超过上限：最旧的被裁掉（52 条 → 保留最近 40 条）
    for i in range(personal.HISTORY_MAX + 10):
        h = personal.record(h, {"day": f"2026-{7 + i // 28:02d}-{i % 28 + 1:02d}",
                                "label": "5K", "actual_vdot": 50.0,
                                "pred_vdot": 50.0})
    assert len(h) == personal.HISTORY_MAX
    assert h[0]["day"] == "2026-07-11"
