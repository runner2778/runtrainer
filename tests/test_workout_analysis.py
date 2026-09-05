"""训练内容分析：间歇/休息分段识别与配速-心率周聚合（纯函数）。"""
from __future__ import annotations

from datetime import date, timedelta

from runtrainer.domain.workout_analysis import (
    analyze_structure, classify_workout, estimate_max_hr,
    pace_bin_hr, summarize_structure, weekly_pace_hr,
)


def _lap(dist, dur, pace, hr=None):
    return {"distance_m": dist, "elapsed_s": dur, "pace_s_km": pace, "avg_hr": hr}


def test_interval_workout_segments():
    """快慢圈交替 → work/rest 分段。"""
    laps = [
        _lap(1000, 420, 420, 110),   # 热身
        _lap(800, 250, 312, 175),    # 快
        _lap(400, 180, 450, 150),    # 慢（休息）
        _lap(800, 250, 312, 176),    # 快
        _lap(400, 180, 450, 152),    # 慢
        _lap(800, 250, 312, 177),    # 快
        _lap(1000, 430, 430, 112),   # 冷身
    ]
    segs = analyze_structure(laps, None, None)
    types = [s["type"] for s in segs]
    assert types == ["recovery", "work", "rest", "work", "rest", "work", "recovery"]
    assert all(s["pace_s_km"] for s in segs)
    summary = summarize_structure(segs)
    assert summary["kind"] == "interval"
    assert summary["work_segments"] == 3
    assert summary["rest_segments"] == 2


def test_continuous_run_single_segment():
    """匀速圈 → continuous 单段，用整体数据。"""
    laps = [_lap(1000, 300, 300, 150), _lap(1000, 302, 302, 151),
            _lap(1000, 298, 298, 150)]
    segs = analyze_structure(laps, 900, 3000)
    assert len(segs) == 1
    assert segs[0]["type"] == "continuous"
    assert segs[0]["distance_m"] == 3000
    assert segs[0]["duration_s"] == 900
    assert summarize_structure(segs)["kind"] == "continuous"


def test_no_laps_uses_overall():
    segs = analyze_structure([], 1800, 5000)
    assert len(segs) == 1
    assert segs[0]["type"] == "continuous"
    assert segs[0]["pace_s_km"] == 360.0


def test_no_data_returns_empty():
    assert analyze_structure([], None, None) == []


def test_weekly_pace_hr_buckets_and_skips_missing():
    """按 ISO 周聚合；无配速/无心率的活动跳过。"""
    from runtrainer.utils import dates
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    acts = [
        # 本周一：两条正常活动
        {"start_ts": dates.date_to_ts(monday), "avg_pace_s_km": 300.0,
         "avg_hr": 150.0, "distance_m": 10000},
        {"start_ts": dates.date_to_ts(monday), "avg_pace_s_km": 320.0,
         "avg_hr": 140.0, "distance_m": 5000},
        # 上周一：一条正常
        {"start_ts": dates.date_to_ts(monday - timedelta(days=7)), "avg_pace_s_km": 330.0,
         "avg_hr": 135.0, "distance_m": 8000},
        # 缺心率/配速 → 跳过
        {"start_ts": dates.date_to_ts(monday), "avg_pace_s_km": None,
         "avg_hr": 150.0, "distance_m": 5000},
        {"start_ts": dates.date_to_ts(monday - timedelta(days=7)), "avg_pace_s_km": 300.0,
         "avg_hr": None, "distance_m": 5000},
    ]
    rows = weekly_pace_hr(acts, monday - timedelta(days=14), today)
    assert len(rows) == 2
    # 最新在前（周降序）
    assert rows[0]["week_start"] == monday.isoformat()
    assert rows[0]["runs"] == 2
    assert rows[0]["distance_km"] == 15.0
    # 距离加权平均：300×10 + 320×5 = 4600/15 = 306.7
    assert abs(rows[0]["avg_pace_s_km"] - 306.7) < 0.11
    assert abs(rows[0]["avg_hr"] - 145.0) < 0.01
    assert rows[1]["week_start"] == (monday - timedelta(days=7)).isoformat()
    assert rows[1]["runs"] == 1
    assert rows[1]["avg_pace_s_km"] == 330.0


def test_weekly_pace_hr_excludes_invalid_data():
    """心率离群/GPS 漂移假快配速/传感器垃圾不参与周聚合。"""
    from runtrainer.utils import dates
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    acts = [
        {"start_ts": dates.date_to_ts(monday), "avg_pace_s_km": 300.0,
         "avg_hr": 150.0, "distance_m": 10000},
        # 心率离群
        {"start_ts": dates.date_to_ts(monday), "avg_pace_s_km": 300.0,
         "avg_hr": 85.0, "distance_m": 5000},
        # GPS 漂移：3:55/km 但心率只有 112
        {"start_ts": dates.date_to_ts(monday), "avg_pace_s_km": 235.0,
         "avg_hr": 112.0, "distance_m": 3000},
        # 传感器垃圾：max_hr < 120
        {"start_ts": dates.date_to_ts(monday), "avg_pace_s_km": 300.0,
         "avg_hr": 100.0, "max_hr": 105, "distance_m": 2000},
    ]
    rows = weekly_pace_hr(acts, monday - timedelta(days=7), today)
    assert len(rows) == 1
    assert rows[0]["runs"] == 1


def _act(ts, pace, hr, dist=10000):
    return {"start_ts": ts, "avg_pace_s_km": pace, "avg_hr": hr, "distance_m": dist}


def test_pace_bin_hr_same_pace_hr_decline_across_periods():
    """同配速档位下不同时期心率对照：近期心率更低 = 进步。"""
    from runtrainer.utils import dates
    today = date.today()
    # 90 天窗口 → 默认 2 期（round(90/40)=2）
    start = today - timedelta(days=90)
    acts = [
        # 第 1 期：300s 档 2 次，HR 150/152
        _act(dates.date_to_ts(start + timedelta(days=5)), 302.0, 150.0),
        _act(dates.date_to_ts(start + timedelta(days=15)), 315.0, 152.0),
        # 第 2 期：同档 2 次，HR 143/141（同等配速心率更低）
        _act(dates.date_to_ts(today - timedelta(days=10)), 300.0, 143.0),
        _act(dates.date_to_ts(today - timedelta(days=3)), 305.0, 141.0),
    ]
    out = pace_bin_hr(acts, start, today)
    assert len(out["periods"]) == 2
    assert out["bins"] == [{"start_s": 300, "end_s": 330}]
    # 最新在前：periods[0] = 最近期（142），periods[1] = 最初期（151）
    assert out["periods"][0]["hr"] == [142.0]
    assert out["periods"][1]["hr"] == [151.0]
    assert out["periods"][0]["runs"] == [2]
    assert out["periods"][1]["distance_km"] == [20.0]
    assert out["periods"][0]["start"] > out["periods"][1]["start"]
    # 短标签带年份 YY/MM（一年窗口跨年份，图例不换行）
    assert all(len(p["label"]) == 5 and p["label"][2] == "/"
               for p in out["periods"])
    # summary：同一配速档心率从 151 → 142，下降 9 bpm
    assert out["summary"]["best_drop"] == -9.0
    assert out["summary"]["best_label"] == "5:00"
    assert "下降 9 bpm" in out["summary"]["note"]


def test_pace_bin_hr_30s_gradient_buckets():
    """30s 梯度分档：同档合并、跨档分开、中间空档保留。"""
    from runtrainer.utils import dates
    today = date.today()
    start = today - timedelta(days=60)
    acts = [
        _act(dates.date_to_ts(start + timedelta(days=1)), 315.0, 150.0),
        _act(dates.date_to_ts(start + timedelta(days=2)), 329.0, 151.0),  # 同 300 档
        _act(dates.date_to_ts(start + timedelta(days=3)), 331.0, 148.0),  # 330 档
        _act(dates.date_to_ts(start + timedelta(days=4)), 340.0, 147.0),  # 330 档
    ]
    out = pace_bin_hr(acts, start, today)
    assert [b["start_s"] for b in out["bins"]] == [300, 330]
    p = out["periods"][0]
    assert p["hr"][0] == 150.5
    assert p["hr"][1] == 147.5


def test_pace_bin_hr_min_runs_guard():
    """每档每期不足 2 次不出点（None），且不参与坐标轴定界。"""
    from runtrainer.utils import dates
    today = date.today()
    start = today - timedelta(days=60)
    acts = [
        _act(dates.date_to_ts(start + timedelta(days=1)), 300.0, 150.0),
        _act(dates.date_to_ts(start + timedelta(days=2)), 300.0, 152.0),
        _act(dates.date_to_ts(today - timedelta(days=1)), 360.0, 133.0),  # 后期仅 1 次
    ]
    out = pace_bin_hr(acts, start, today)
    # 360 档没有任何时期达到 2 次 → 不进坐标轴（不撑宽横轴）
    assert [b["start_s"] for b in out["bins"]] == [300]
    # 后期只有 1 次跑步（无档位达标）→ 整期被丢弃，不画死线
    assert len(out["periods"]) == 1
    assert out["periods"][0]["hr"] == [151.0]


def test_pace_bin_hr_skips_missing_fields_and_empty():
    from runtrainer.utils import dates
    today = date.today()
    start = today - timedelta(days=30)
    acts = [
        {"start_ts": dates.date_to_ts(today), "avg_pace_s_km": None,
         "avg_hr": 150.0, "distance_m": 5000},
        {"start_ts": dates.date_to_ts(today), "avg_pace_s_km": 300.0,
         "avg_hr": None, "distance_m": 5000},
    ]
    assert pace_bin_hr(acts, start, today) == {"bins": [], "periods": []}


def test_pace_bin_hr_clamps_non_running_paces():
    """坏数据/走路配速（<3:00 或 >15:00/km）不参与对照，不撑爆坐标轴。"""
    from runtrainer.utils import dates
    today = date.today()
    start = today - timedelta(days=30)
    acts = [
        # 正常跑步两条（300s 档）
        _act(dates.date_to_ts(today - timedelta(days=2)), 302.0, 150.0),
        _act(dates.date_to_ts(today - timedelta(days=1)), 306.0, 151.0),
        # 坏数据/走路：极快与极慢各两条 → 必须被钳掉
        _act(dates.date_to_ts(today - timedelta(days=3)), 120.0, 160.0, 500),
        _act(dates.date_to_ts(today - timedelta(days=4)), 150.0, 159.0, 500),
        _act(dates.date_to_ts(today - timedelta(days=5)), 2000.0, 100.0, 3000),
        _act(dates.date_to_ts(today - timedelta(days=6)), 3000.0, 99.0, 3000),
    ]
    out = pace_bin_hr(acts, start, today)
    assert [b["start_s"] for b in out["bins"]] == [300]
    # 30 天窗口 → 2 期；两条正常跑都在近期（第 2 期），空的第一期被丢弃
    assert len(out["periods"]) == 1
    assert out["periods"][0]["hr"] == [150.5]


# ---- 课程分类 ----

def test_classify_interval_sprint_vs_fast_and_rest_modes():
    """间歇：跑段细分冲刺/快跑（距离+相对配速），休息段细分走路/慢跑/静止。"""
    segs = [
        {"type": "recovery", "distance_m": 1000, "elapsed_s": 400, "pace_s_km": 400, "avg_hr": 115},
        {"type": "work", "distance_m": 800, "elapsed_s": 160, "pace_s_km": 200, "avg_hr": 176},
        {"type": "rest", "distance_m": 400, "elapsed_s": 160, "pace_s_km": 400, "avg_hr": 150},
        {"type": "work", "distance_m": 400, "elapsed_s": 72, "pace_s_km": 180, "avg_hr": 178},
        {"type": "rest", "distance_m": 200, "elapsed_s": 120, "pace_s_km": 600, "avg_hr": 145},
        {"type": "rest", "distance_m": None, "elapsed_s": 90, "pace_s_km": None, "avg_hr": 140},
    ]
    w = classify_workout(segs, duration_s=2000, distance_m=8000)
    assert w["kind"] == "interval"
    assert w["work"] == {"fast": 1, "sprint": 1}
    assert w["rest"] == {"walk": 1, "jog": 1, "stand": 1}
    assert w["seg_kinds"] == ["warmup", "fast", "jog", "sprint", "walk", "stand"]
    assert "快跑段 ×1" in w["label"] and "冲刺段 ×1" in w["label"]
    assert "走路" in w["label"] and "慢跑" in w["label"] and "静止" in w["label"]


def test_classify_repeats_when_rest_laps_missing():
    """自动暂停吞掉休息圈：只有跑段 → 重复跑，不误报匀速。"""
    segs = [
        {"type": "work", "distance_m": 400, "elapsed_s": 75, "pace_s_km": 188, "avg_hr": 175},
        {"type": "work", "distance_m": 400, "elapsed_s": 74, "pace_s_km": 185, "avg_hr": 177},
        {"type": "work", "distance_m": 400, "elapsed_s": 76, "pace_s_km": 190, "avg_hr": 176},
    ]
    w = classify_workout(segs, duration_s=900, distance_m=3600)
    assert w["kind"] == "repeats"
    assert w["work"]["sprint"] == 3  # 400m 且远快于整体 → 冲刺
    assert "休息未记录" in w["label"]


def test_classify_continuous_by_hr_zone():
    """匀速跑按 心率/最大心率 归类；缺心率时无法归类。"""
    assert classify_workout([], 1800, 5000, avg_hr=115, max_hr=200)["kind"] == "recovery"
    assert classify_workout([], 1800, 5000, avg_hr=130, max_hr=200)["kind"] == "easy"
    assert classify_workout([], 1800, 5000, avg_hr=150, max_hr=200)["kind"] == "aerobic"
    assert classify_workout([], 1800, 5000, avg_hr=170, max_hr=200)["kind"] == "tempo"
    assert classify_workout([], 1800, 5000, avg_hr=190, max_hr=200)["kind"] == "anaerobic"
    w = classify_workout([], 1800, 5000, avg_hr=None, max_hr=200)
    assert w["kind"] == "unknown" and "缺心率" in w["label"]
    w = classify_workout([], 1800, 5000, avg_hr=150, max_hr=None)
    assert w["kind"] == "unknown"
    # 传感器垃圾保护：max_hr < 120 或 max < avg 不参与心率区归类
    w = classify_workout([], 1800, 5000, avg_hr=52, max_hr=52)
    assert w["kind"] == "unknown" and w["hr_pct"] is None
    w = classify_workout([], 1800, 5000, avg_hr=140, max_hr=90)
    assert w["kind"] == "unknown" and w["hr_pct"] is None


def test_classify_karvonen_hrr():
    """静息心率已知 → Karvonen 储备心率区（训练有素者不再整体升档）。

    max=200 rest=42：HRR=158。60%HRR=136.8、72%=155.8、82%=171.6、92%=187.4。
    真实案例：基础有氧课 avg 145-155（73-77%HRmax，%HRmax 会误判 aerobic）
    → %HRR 65-72% 落 easy。
    """
    # %HRmax 视角的 aerobic（150/200=75%）在 %HRR 视角是 easy（108/158=68%）
    assert classify_workout([], 1800, 5000, avg_hr=150, max_hr=200)["kind"] == "aerobic"
    w = classify_workout([], 1800, 5000, avg_hr=150, max_hr=200, rest_hr=42)
    assert w["kind"] == "easy"
    assert abs(w["hr_pct"] - 0.684) < 0.01
    # 恢复跑 134 = 58%HRR → recovery（%HRmax 67% 会判 easy）
    w = classify_workout([], 1800, 5000, avg_hr=134, max_hr=200, rest_hr=42)
    assert w["kind"] == "recovery"
    # 高强度：190 → 94%HRR → anaerobic
    w = classify_workout([], 1800, 5000, avg_hr=190, max_hr=200, rest_hr=42)
    assert w["kind"] == "anaerobic"
    # avg <= rest（不可能但防除零/负值）→ 回退 %HRmax
    w = classify_workout([], 1800, 5000, avg_hr=40, max_hr=200, rest_hr=42)
    assert w["kind"] == "recovery" and abs(w["hr_pct"] - 0.2) < 0.01


def test_estimate_max_hr():
    assert estimate_max_hr(None) is None
    # 19 岁 → 208 - 0.7*19 ≈ 194.7
    assert abs(estimate_max_hr(2007) - 194.7) < 0.1


def test_infer_max_hr():
    from runtrainer.domain.workout_analysis import infer_max_hr
    # 样本不足 → None
    assert infer_max_hr([200.0, 195.0], min_n=20) is None
    # 前 5 均值：毛刺被稀释
    peaks = [200, 200, 199, 198, 198] + [170] * 30
    out = infer_max_hr(peaks, min_n=20)
    assert out["value"] == 199.0
    assert out["n"] == 35
    # 全垃圾（<160）→ None
    assert infer_max_hr([120.0] * 30, min_n=20) is None
    # 离群毛刺（230）被钳制丢弃；top5 从钳制后的峰值里取
    out = infer_max_hr([230, 200, 200, 199, 198] + [170] * 30, min_n=20)
    assert out["value"] == round((200 + 200 + 199 + 198 + 170) / 5, 1)  # 193.4


# ---- 采样级结构识别 ----

def _samples(t0, blocks):
    """blocks: [(duration_s, speed_mps, hr), ...] → 1s 采样列表；gap 块（speed=None）
    产生时间间隙（模拟自动暂停，无采样行）。"""
    out = []
    t = t0
    for dur, spd, hr in blocks:
        if spd is None:
            t += dur
            continue
        for i in range(int(dur)):
            out.append({"t_offset_s": t + i, "speed_mps": spd, "hr": hr})
        t += dur
    return out


def test_structure_from_samples_interval_with_rests():
    """采样曲线识别间歇：快慢交替 → recovery/work/rest，休息方式可细分。"""
    samples = _samples(0.0, [
        (300, 2.8, 120),                       # 热身
        (90, 4.5, 178), (90, 1.2, 140),        # 400m 快段 + 走路休息
        (90, 4.5, 179), (90, 1.2, 141),
        (90, 4.5, 180), (90, 1.2, 140),
        (300, 2.8, 115),                       # 冷身
    ])
    segs = analyze_structure([], None, None, samples=samples)
    assert [s["type"] for s in segs] == ["recovery", "work", "rest",
                                         "work", "rest", "work", "recovery"]
    works = [s for s in segs if s["type"] == "work"]
    assert all(abs(s["distance_m"] - 400) < 20 for s in works)
    rest = segs[2]
    assert rest["pace_s_km"] and rest["pace_s_km"] > 500  # 走路
    w = classify_workout(segs, avg_hr=150, max_hr=200)
    assert w["kind"] == "interval"
    # 末快段后的休息并入冷身（trailing recovery），只计快段间的 2 段休息
    assert w["rest"] == {"walk": 2, "jog": 0, "stand": 0}


def test_structure_from_samples_continuous_run():
    """匀速采样 → 单个 continuous 段（不再被圈级速度比误判成间歇）。"""
    samples = _samples(0.0, [(1800, 3.0, 145)])
    segs = analyze_structure([], None, None, samples=samples)
    assert len(segs) == 1
    assert segs[0]["type"] == "continuous"
    assert 5000 < segs[0]["distance_m"] < 5600
    assert abs(segs[0]["avg_hr"] - 145) < 0.1


def test_structure_from_samples_autopause_stand_rest():
    """自动暂停：快段间没有采样行 → 静止休息段（距离 0、按时间差计时）。"""
    samples = _samples(0.0, [
        (120, 2.8, 125),
        (60, 4.2, 175), (60, None, None),      # 暂停 60s
        (60, 4.2, 176),
        (120, 2.8, 118),
    ])
    segs = analyze_structure([], None, None, samples=samples)
    assert [s["type"] for s in segs] == ["recovery", "work", "rest", "work", "recovery"]
    rest = segs[2]
    assert rest["distance_m"] == 0.0
    assert 55 <= rest["duration_s"] <= 65
    w = classify_workout(segs, avg_hr=145, max_hr=200)
    assert w["rest"] == {"walk": 0, "jog": 0, "stand": 1}


def test_structure_from_samples_single_fast_block_is_continuous():
    """长距离跑中单段提速（如渐加速/节奏段）不是间歇课 → continuous。"""
    samples = _samples(0.0, [(600, 2.8, 130), (600, 4.5, 170), (600, 2.8, 128)])
    segs = analyze_structure([], None, None, samples=samples)
    assert len(segs) == 1
    assert segs[0]["type"] == "continuous"


def test_structure_from_samples_far_apart_blocks_not_interval():
    """两块提速相距 >20 分钟 = 两次独立安排，不算间歇课。"""
    samples = _samples(0.0, [(300, 2.8, 125), (120, 4.5, 172),
                             (1500, 2.8, 135),            # 中间 25 分钟匀速
                             (120, 4.5, 173), (300, 2.8, 120)])
    segs = analyze_structure([], None, None, samples=samples)
    assert len(segs) == 1
    assert segs[0]["type"] == "continuous"
