"""训练表现证据层（批33）：每次跑步 → 至多一条证据；训练→比赛转换；
比赛分级；证据密度驱动的中心估计标准误与预测区间收窄。"""
from __future__ import annotations

from datetime import date, timedelta

from runtrainer.domain import ability as ab
from runtrainer.domain import evidence as evm
from runtrainer.domain import personal, vdot as vd
from runtrainer.utils import dates


def _act(days_ago, pace_s_km, avg_hr, max_hr, dist_m, dur_s, name="轻松跑",
         structure=None, samples=None, aid=1, date_iso=None):
    """构造一条跑步活动（带可选结构分段与样本点）。"""
    return {
        "name": name,
        "start_ts": dates.date_to_ts(date_iso or (dates.today() - timedelta(days=days_ago))),
        "date": (date_iso or (dates.today() - timedelta(days=days_ago))).isoformat(),
        "distance_m": dist_m, "duration_s": dur_s,
        "avg_pace_s_km": pace_s_km, "avg_hr": avg_hr, "max_hr": max_hr,
        "structure": structure or [], "id": aid,
        "has_samples": samples is not None,
    }


def _samples(n=2000, speed=3.0, hr=160, dt=1.0):
    """匀速样本点：n 个点、每 dt 秒、速度 speed m/s、心率 hr。"""
    return [{"t_offset_s": i * dt, "speed_mps": speed, "hr": hr}
            for i in range(n)]


MAX_HR = 190.0


# ---- 证据提取：每节课至多一类，比赛不重复计 ----

def test_extract_interval_one_record_per_workout():
    """间歇课 → 一条 interval 证据（含重复数/段距/休息比/最后一组）。"""
    a = _act(10, 300, 150, MAX_HR, 8000, 40 * 60, name="间歇课", structure=[
        {"type": "work", "distance_m": 1000, "pace_s_km": 230, "avg_hr": 170},
        {"type": "rest", "distance_m": 400, "pace_s_km": 430, "elapsed_s": 90},
        {"type": "work", "distance_m": 1000, "pace_s_km": 232, "avg_hr": 171},
        {"type": "rest", "distance_m": 400, "pace_s_km": 432, "elapsed_s": 90},
        {"type": "work", "distance_m": 1000, "pace_s_km": 234, "avg_hr": 170},
    ])
    ev = evm.extract_workout_evidence([a], None, MAX_HR)
    assert len(ev) == 1 and ev[0]["kind"] == "interval"
    r = ev[0]
    assert r["n_reps"] == 3 and r["med_dist_m"] == 1000
    assert r["rest_ratio"] is not None and abs(r["rest_ratio"] - 0.26) < 0.01
    assert r["last_rep_pace_s_km"] == 234.0
    assert r["subtype"] in ("threshold", "vo2max", "speed")
    assert r["vdot"] > 0 and r["pace_s_km"] == 232.0


def test_extract_steady_from_continuous_tempo_run():
    """连续匀速 15–75min、心率 82–95% HRmax → 一条 steady 证据。"""
    a = _act(5, 260, 160, MAX_HR, 7000, 30 * 60, name="匀速跑")
    ev = evm.extract_workout_evidence([a], None, MAX_HR)
    assert len(ev) == 1 and ev[0]["kind"] == "steady"
    assert ev[0]["pace_s_km"] == 260.0
    assert abs(ev[0]["hr_ratio"] - 160 / MAX_HR) < 0.01


def test_extract_best_effort_from_sample_windows():
    """其余跑步用样本滑窗切最快分段（心率门 ≥82% HRmax）。"""
    samples = _samples(n=2000, speed=3.0, hr=160)
    a = _act(3, 320, 150, MAX_HR, 6000, 33 * 60, name="长距离", samples=samples)
    ev = evm.extract_workout_evidence([a], lambda aid: samples, MAX_HR)
    kinds = [r["kind"] for r in ev]
    assert kinds == ["best_effort"]               # 一次跑步只产一条证据
    assert ev[0]["subtype"] == "5K"               # 6000m 只够切出 5K 窗
    assert ev[0]["pace_s_km"] == 333.3
    assert abs(ev[0]["hr_ratio"] - 160 / MAX_HR) < 0.01


def test_extract_skips_race_activities():
    """比赛活动走比赛通道，不产训练证据（同一份数据不双计）。"""
    a = _act(2, 252, 180, 190, 10000, 42 * 60, name="10K 比赛")
    assert evm.extract_workout_evidence([a], None, MAX_HR) == []
    # 名称不含提示但被调用方标为比赛 id 的：按 id 排除
    a2 = _act(2, 252, 180, 190, 10000, 42 * 60, name="测验", aid=7)
    assert evm.extract_workout_evidence([a2], None, MAX_HR, race_ids={7}) == []


def test_extract_no_sample_no_best_effort():
    """无样本的长跑不产 best_effort（宁缺毋滥，不编造分段）。"""
    a = _act(3, 320, 150, MAX_HR, 12000, 64 * 60, name="长距离")
    assert evm.extract_workout_evidence([a], None, MAX_HR) == []


# ---- 训练→比赛转换：先验 + 个人学习（EWMA + 收缩 + 封顶）----

def test_conversion_deltas_priors_without_history():
    """无个人历史 → 群体先验（真实库标定：训练最快分段 ≠ 比赛水平）。"""
    d = evm.conversion_deltas([])
    assert d["interval"]["delta"] == 0.1 and d["interval"]["n"] == 0
    assert d["steady"]["delta"] == 1.5
    assert d["best_effort"]["delta"] == 5.7


def test_conversion_deltas_ewma_shrink_and_submax_excluded():
    """个人转换学习：Δ 样本按日期 EWMA（α=0.3）、按样本数向先验收缩
    n/(n+2)；次全力比赛不参与；单样本 Δ 封顶 ±3。"""
    hist = [
        {"day": "2026-05-01", "label": "5K", "actual_vdot": 53.0,
         "quality": "all_out", "ev_feats": {"interval": 50.0}},
        {"day": "2026-06-01", "label": "5K", "actual_vdot": 54.0,
         "quality": "all_out", "ev_feats": {"interval": 50.5}},
        {"day": "2026-07-01", "label": "5K", "actual_vdot": 47.0,
         "quality": "submax", "ev_feats": {"interval": 51.0}},
    ]
    d = evm.conversion_deltas(hist)
    # 样本 Δ = +3.0、+3.5(封顶 3.0) → EWMA 0.9 → 1.53；收缩 0.1 + (1.53−0.1)×2/4 ≈ 0.81
    assert d["interval"]["n"] == 2
    assert abs(d["interval"]["delta"] - 0.81) < 0.01
    # 极端样本封顶：Δ +20 → 3.0 → EWMA 0.9 → 收缩 0.1+(0.9−0.1)/3 ≈ 0.37
    big = [{"day": "2026-06-01", "label": "5K", "actual_vdot": 70.0,
            "quality": "all_out", "ev_feats": {"interval": 50.0}}]
    d2 = evm.conversion_deltas(big)
    assert abs(d2["interval"]["delta"] - 0.37) < 0.01


# ---- 当前能力：努力修正 + 近期加权 + 分档 ----

def test_current_fitness_effort_adjustment_and_buckets():
    """心率越低 → 离比赛水平越远（按与 0.92 的差距外推）；best_effort 分档
    供各距离专项混合取用。"""
    ev = [
        {"day": "2026-09-01", "kind": "best_effort", "subtype": "5K", "dist": "5K",
         "vdot": 48.0, "hr_ratio": 0.84, "quality": 1.0},
        {"day": "2026-09-05", "kind": "best_effort", "subtype": "10K", "dist": "10K",
         "vdot": 47.0, "hr_ratio": 0.92, "quality": 1.0},
    ]
    f = evm.current_fitness(ev, None, date(2026, 9, 10))
    be = f["best_effort"]
    assert be["n"] == 2
    assert be["raw"] == 47.0                     # 原始加权中位
    assert be["eff"] == 47.0                     # 努力修正后（0.84 的 +1.2 被中位吸收）
    assert be["vdot"] == 52.7                    # + 群体先验 5.7
    buckets = f["best_effort_buckets"]
    assert buckets["short"]["vdot"] == 54.9      # 5K 档：48 + 15×0.08 + 5.7
    assert buckets["mid"]["vdot"] == 52.7        # 10K 档：47 + 0 + 5.7
    assert buckets["short"]["n"] == 1 and buckets["mid"]["n"] == 1


# ---- 比赛分级 ----

def test_grade_race_name_hint_all_out_and_evidence_gap_submax():
    """名称提示 → 全开；成绩低于同期训练证据中位 ≥3 VDOT（证据 ≥3 条）→
    次全力（不参与学习与锚定）；证据不足不下结论。"""
    assert evm.grade_race({"name": "10K 比赛", "day": "2026-08-01"}, [], None,
                          MAX_HR) == "all_out"
    ev = [
        {"day": "2026-07-20", "kind": "interval", "vdot": 54.0, "hr_ratio": 0.95},
        {"day": "2026-07-25", "kind": "best_effort", "vdot": 53.0, "hr_ratio": 0.90},
        {"day": "2026-07-28", "kind": "steady", "vdot": 55.0, "hr_ratio": 0.88},
    ]
    deltas = evm.conversion_deltas([])
    race = {"name": "操场计时", "day": "2026-08-01", "actual_vdot": 49.0}
    assert evm.grade_race(race, ev, deltas, MAX_HR) == "submax"
    race_ok = dict(race, actual_vdot=54.5)
    assert evm.grade_race(race_ok, ev, deltas, MAX_HR) == "all_out"
    # 同期证据不足 3 条：按全开（证据不足不下结论）
    assert evm.grade_race(race, ev[:2], deltas, MAX_HR) == "all_out"


# ---- 中心估计标准误：证据越密 → 区间越窄 ----

def test_center_se_density_and_caps():
    """SE² = Σ σ²_kind / n_eff（n_eff 封顶——再多只是重复信息）。"""
    day = "2026-09-20"
    ev = [{"day": day, "kind": "interval", "vdot": 50.0} for _ in range(30)]
    se = evm.center_se(ev, date(2026, 9, 25))
    assert abs(se - 0.289) < 0.01                # 1.0/sqrt(12)
    # 超过 90 天的证据不计入密度
    old = [{"day": "2026-05-01", "kind": "interval", "vdot": 50.0}]
    assert evm.center_se(old, date(2026, 9, 25)) == 0.0


# ---- 管线集成：证据密度收窄预测区间（同一历史，不同证据量）----

def _interval_acts(n, start_days_ago):
    acts = []
    for i in range(n):
        acts.append(_act(start_days_ago - i * 3, 300, 150, MAX_HR, 8000,
                         40 * 60, name="间歇课", structure=[
            {"type": "work", "distance_m": 1000, "pace_s_km": 230, "avg_hr": 170},
            {"type": "rest", "distance_m": 400, "pace_s_km": 430, "elapsed_s": 90},
            {"type": "work", "distance_m": 1000, "pace_s_km": 232, "avg_hr": 171},
            {"type": "rest", "distance_m": 400, "pace_s_km": 432, "elapsed_s": 90},
            {"type": "work", "distance_m": 1000, "pace_s_km": 234, "avg_hr": 170},
        ], aid=100 + i))
    return acts


HIST_1 = [{"day": "2026-03-01", "label": "半马", "actual_vdot": 52.0,
           "pred_vdot": 50.0, "feats": {}}]


def test_pipeline_interval_narrows_with_evidence_density():
    """同一历史、同一水平：窗口内证据越密 → SE_center 越小 → 半宽越窄；
    证据少不装窄区间（诚实的不确定性）。"""
    sparse = ab.compute_ability(_interval_acts(1, 10), None, 190.0,
                                personal_history=HIST_1)
    dense = ab.compute_ability(_interval_acts(13, 10), None, 190.0,
                               personal_history=HIST_1)
    iv_s = sparse["prediction_intervals"]["5K"]
    iv_d = dense["prediction_intervals"]["5K"]
    assert iv_s["se_center"] > iv_d["se_center"] > 0
    assert iv_d["half_vdot"] < iv_s["half_vdot"]
    # 预测存在且训练证据进入通道（间歇证据在 5K 权重最高）
    assert sparse["predictions"]["5K"] > 0
    assert any(e["source"] == "间歇训练证据" for e in sparse["evidence"])


def test_pipeline_predicts_without_history():
    """无个人历史（新用户）：群体先验转换 + 训练证据照样给出预测；区间
    没有执行噪声基准时用先验 RMSE，但有证据密度数据（center_se>0）就给
    区间——完全无比赛且无证据密度数据时才不装 ±。"""
    est = ab.compute_ability(_interval_acts(3, 10), None, 190.0,
                             personal_history=[])
    assert est["predictions"]["5K"] > 0
    iv = est["prediction_intervals"]["5K"]
    assert iv["n"] == 0 and iv["rmse"] == personal.RESID_PRIOR_RMSE
    assert iv["se_center"] > 0 and iv["half_vdot"] > 0


def test_pipeline_hidden_pb_from_training_beats_old_race():
    """隐性 PB：训练证据（间歇水平）高于窗口内旧比赛时，预测取证据水平，
    不被过期比赛锚死。"""
    old_race = _act(120, 300, 185, 190, 10000, 50 * 60, name="10K 比赛")
    acts = _interval_acts(6, 10) + [old_race]
    est = ab.compute_ability(acts, None, 190.0, personal_history=[])
    iv_vdot = _interval_vdot_est()
    race_vdot = vd.estimate_vdot(10000, 50 * 60)
    assert iv_vdot > race_vdot                 # 训练证据确实更快
    assert est["predictions"]["10K"] > 0
    # 预测中心不受旧比赛压制：10K 预测对应的 VDOT 高于旧比赛 VDOT
    pred_v = vd.estimate_vdot(10000, est["predictions"]["10K"])
    assert pred_v > race_vdot + 0.5


def _interval_vdot_est():
    """3 组 1000m @3:52/km、休息比 0.26 → 阈值型反算的 VDOT 估算。"""
    v = ab._vdot_for_pace(232, vd.T_PCT)
    return v * (1 + ab.INTERVAL_REST_K * (ab.INTERVAL_REST_REF - 0.26))


# ---- 批35：三处真实误差归因（停表空档 / 过期比赛当锚 / 矛盾加权坡度）----

def test_best_effort_window_excludes_pause_gap():
    """最快分段窗口不吃停表空档：空档里没有样本，「缺口后那个样本的速度」
    不能替整段空档补出距离，空档时长也不能算进窗口用时。真实库 id=233：
    557 s 自动暂停被算成 0.69 m/s × 557 s = 384 m 跑步，使 17.06 km 的
    一次跑步里「最快 15K 分段」反比全程均速还慢（VDOT 41.3 vs 实际约 47）。"""
    def _run(with_pause: bool):
        out, t = [], 0.0
        for _ in range(600):                      # 快跑段 6000 m（5 m/s）
            out.append((t, 5.0))
            t += 2.0
        if with_pause:
            t += 600.0                            # 停表 600 s：这段没有样本
        for _ in range(5):                        # 恢复期慢走 10 m
            out.append((t, 1.0))
            t += 2.0
        for _ in range(1200):                     # 快跑段 12000 m
            out.append((t, 5.0))
            t += 2.0
        return [{"t_offset_s": a, "speed_mps": b, "hr": 170} for a, b in out]

    dists = ((15000, "15K"),)
    plain = evm._best_effort_windows_for(_run(False), dists, MAX_HR)["15K"][0]
    paused = evm._best_effort_windows_for(_run(True), dists, MAX_HR)["15K"][0]
    assert abs(plain * 1000 - 200.5) < 1.0        # 5 m/s → 200 s/km（含慢走）
    assert abs(paused - plain) * 1000 < 1.0       # 停表不改变最快分段的配速


def test_pipeline_stale_same_distance_race_does_not_anchor():
    """过期（>90 天）的同距离比赛不再当该距离的锚：单场比赛的近期加权中位
    就是那场本身，60 天半衰期对 n=1 形同虚设。真实库 10-07 5K：仅剩的
    4/12 5K 已过 178 天，仍以 0.40 权重锚住 5K 并被矛盾加权抬到 0.76，
    预测比实际快 93 s。别的距离有近期比赛不能替它放行。"""
    base = _interval_acts(6, 12)
    fresh_hm = _act(10, 300, 180, 190, 21097, 88 * 60, name="半马 比赛", aid=7)
    stale_5k = _act(150, 200, 190, 190, 5000, 16 * 60, name="5K 比赛", aid=8)
    without = ab.compute_ability(base + [fresh_hm], None, 190.0,
                                 personal_history=[])
    with_old = ab.compute_ability(base + [fresh_hm, stale_5k], None, 190.0,
                                  personal_history=[])
    p0 = vd.estimate_vdot(5000, without["predictions"]["5K"])
    p1 = vd.estimate_vdot(5000, with_old["predictions"]["5K"])
    stale_v = vd.estimate_vdot(5000, 16 * 60)
    assert stale_v > p0 + 5.0                     # 旧比赛确实远快于训练证据
    assert abs(p1 - p0) < 0.5                     # 有它没它，5K 预测一样


def test_pipeline_fresh_race_dominates_when_disagreeing():
    """同距离近期全开比赛与训练通道差到 GAP+SPAN（2+1=3 VDOT）时预测贴住
    比赛通道：训练通道是「证据 → 比赛」的换算，带着不确定的转换差，同距离
    全开比赛是直读。差过噪声量级后再按 4 VDOT 的坡度慢慢把训练混回来没有
    意义（回测：SPAN 4→3→2→1 时 MAE 83→81→76→73）。"""
    base = _interval_acts(6, 12)
    no_race = ab.compute_ability(base, None, 190.0, personal_history=[])
    train_v = vd.estimate_vdot(5000, no_race["predictions"]["5K"])
    # 差 3.0 VDOT = 回测标定的 CONTRADICT_GAP + CONTRADICT_SPAN。写死而
    # 不从常量取，是为了让 SPAN 被改回 4.0（同样差 3.0 却只给 0.25 坡度、
    # 预测落后比赛通道 0.9 VDOT）时这条测试立刻失败。
    dur = float(vd.predict_time(5000, train_v - 3.0))
    race_v = round(vd.estimate_vdot(5000, dur), 1)
    race = _act(10, dur / 5.0, 188, 190, 5000, dur, name="5K 比赛", aid=9)
    est = ab.compute_ability(base + [race], None, 190.0, personal_history=[])
    pred_v = vd.estimate_vdot(5000, est["predictions"]["5K"])
    assert abs(pred_v - race_v) < 0.1             # SPAN=4 时这里差 0.9 VDOT

