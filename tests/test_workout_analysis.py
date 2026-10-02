"""训练内容分析：间歇/休息分段识别与配速-心率周聚合（纯函数）。"""
from __future__ import annotations

import json
from datetime import date, timedelta

from runtrainer.domain.workout_analysis import (
    analyze_structure, classify_workout, estimate_max_hr,
    pace_bin_hr, pace_bin_weekly, pace_hr_segments, summarize_structure,
    weekly_pace_hr,
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
    # 距离加权平均（配速与心率同口径）：300×10 + 320×5 = 4600/15 = 306.7；
    # 心率 (150×10 + 140×5)/15 = 146.7（简单平均会是 145——短慢跑被高估权重）
    assert abs(rows[0]["avg_pace_s_km"] - 306.7) < 0.11
    assert abs(rows[0]["avg_hr"] - 146.7) < 0.11
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


# ---- 周化同配速心率（pace_bin_weekly）----

END = date(2026, 10, 2)   # 周五；周刻度按 ISO 自然周（周一为起点）


def _mondays(end, weeks):
    end_mon = end - timedelta(days=end.weekday())
    return [end_mon - timedelta(weeks=weeks - 1 - i) for i in range(weeks)]


def test_pace_bin_weekly_iso_week_buckets_and_min_runs():
    """周化：按自然周切桶（每周一点），每档每周不足 2 次不出点。"""
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = [
        # 最早一周：300 档 2 次（10km+30km，距离加权）；330 档仅 1 次
        _act(dates.date_to_ts(mons[0]), 302.0, 150.0, 10000),
        _act(dates.date_to_ts(mons[0] + timedelta(days=2)), 310.0, 152.0, 30000),
        _act(dates.date_to_ts(mons[0] + timedelta(days=3)), 335.0, 140.0),
        # 本周（部分周）：300 档 2 次，心率更低
        _act(dates.date_to_ts(mons[15]), 300.0, 143.0),
        _act(dates.date_to_ts(mons[15] + timedelta(days=1)), 305.0, 145.0),
    ]
    out = pace_bin_weekly(acts, END, weeks=16)
    assert out["bin_s"] == 20          # 3:00/3:20/3:40… 把 3:20 做成档界
    assert [w["start"] for w in out["weeks"]] == [m.isoformat() for m in mons]
    assert out["weeks"][-1]["end"] == END.isoformat()
    assert out["weeks"][-1]["partial"] is True    # 本周只到周五
    assert out["weeks"][0]["partial"] is False

    b300 = next(b for b in out["bins"] if b["start_s"] == 300)
    # 距离加权：(150×10k + 152×30k) / 40k = 151.5
    assert b300["hr"][0] == 151.5
    assert b300["hr"][15] == 144.0
    assert all(v is None for v in b300["hr"][1:15])   # 中间 14 周无数据
    assert b300["week_segs"][0] == 2 and b300["week_segs"][8] == 0
    assert b300["distance_km"] == 60.0
    # 330 档只有一个周有 2 次 → 连不成趋势线，不进候选
    assert [b["start_s"] for b in out["bins"]] == [300]


def test_pace_bin_weekly_early_recent_block_drop():
    """首/末各 4 周距离加权对照：同配速心率下降 → 负 drop + 进步结论。"""
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = []
    for wi in range(16):
        hr = 150.0 if wi < 4 else 140.0    # 首 4 周 150，其余 140
        for d in (1, 2):
            acts.append(_act(dates.date_to_ts(mons[wi] + timedelta(days=d)), 300.0, hr))
    out = pace_bin_weekly(acts, END, weeks=16)
    b = out["bins"][0]
    assert b["label"] == "5:00"
    assert b["early_hr"] == 150.0 and b["recent_hr"] == 140.0
    assert b["drop"] == -10.0
    assert "下降 10 bpm" in b["note"] and "有氧能力进步" in b["note"]
    assert out["default_start_s"] == 300


def test_pace_bin_weekly_block_drop_is_distance_weighted():
    """首/末 4 周块用距离加权——短慢跑不把档位均值带偏。"""
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = [
        # 首 4 周内：1 次 30km @148 + 1 次 10km @156 → 加权 150.0
        _act(dates.date_to_ts(mons[0]), 300.0, 148.0, 30000),
        _act(dates.date_to_ts(mons[0] + timedelta(days=1)), 300.0, 156.0, 10000),
        # 末 4 周内：1 次 30km @136 + 1 次 10km @144 → 加权 138.0
        _act(dates.date_to_ts(mons[15]), 300.0, 136.0, 30000),
        _act(dates.date_to_ts(mons[15] + timedelta(days=1)), 300.0, 144.0, 10000),
    ]
    b = pace_bin_weekly(acts, END, weeks=16)["bins"][0]
    assert b["early_hr"] == 150.0 and b["recent_hr"] == 138.0
    assert b["drop"] == -12.0


def test_pace_bin_weekly_20s_gradient_splits_neighbours():
    """20s 梯度：302/314/316 同属 5:00 档，320 起是 5:20 档（30s 档会把 316/329 合并）。"""
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = []
    for wi in (0, 15):
        acts += [
            _act(dates.date_to_ts(mons[wi]), 302.0, 150.0),
            _act(dates.date_to_ts(mons[wi] + timedelta(days=1)), 314.0, 148.0),
            _act(dates.date_to_ts(mons[wi] + timedelta(days=2)), 316.0, 145.0),
            _act(dates.date_to_ts(mons[wi] + timedelta(days=3)), 329.0, 143.0),
            _act(dates.date_to_ts(mons[wi] + timedelta(days=4)), 324.0, 143.0),
        ]
    out = pace_bin_weekly(acts, END, weeks=16)
    assert [b["start_s"] for b in out["bins"]] == [300, 320]
    b300, b320 = out["bins"]
    assert (b300["label"], b320["label"]) == ("5:00", "5:20")
    assert b300["end_s"] == 320 and b320["end_s"] == 340
    assert b300["hr"][0] == 147.7      # (150+148+145)/3
    assert b320["hr"][0] == 143.0      # (143+143)/2


def test_pace_bin_weekly_default_picks_highest_distance_bin():
    """下拉默认档 = 跑量最大的档，**不是**样本段数最多的档。

    真实库里间歇课的快段又短又多：3:20 档 41 段却只有 16.6km，主配速 5:00 档
    35 段有 314km。按段数排序会把默认档停在间歇配速上，必须按里程排。
    """
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = []
    for wi in (0, 15):
        acts += [
            # 5:00 档：每周 2 段 × 20km = 40km（长距离匀速跑：段少但里程大）
            _act(dates.date_to_ts(mons[wi]), 300.0, 150.0, 20000),
            _act(dates.date_to_ts(mons[wi] + timedelta(days=1)), 300.0, 151.0, 20000),
            # 5:20 档：每周 3 段 × 5km = 15km（间歇：段多但里程小）
            _act(dates.date_to_ts(mons[wi] + timedelta(days=2)), 330.0, 140.0, 5000),
            _act(dates.date_to_ts(mons[wi] + timedelta(days=3)), 330.0, 141.0, 5000),
            _act(dates.date_to_ts(mons[wi] + timedelta(days=4)), 330.0, 142.0, 5000),
        ]
    out = pace_bin_weekly(acts, END, weeks=16)
    assert [b["start_s"] for b in out["bins"]] == [300, 320]   # 里程多者在前
    assert out["default_start_s"] == 300
    b300, b320 = out["bins"]
    assert b300["distance_km"] == 80.0 and b320["distance_km"] == 30.0
    assert b300["segs"] == 4 and b320["segs"] == 6   # 段数与里程相反，里程说了算


def test_pace_bin_weekly_hides_thin_bins_below_min_segs():
    """总样本低于 MIN_BIN_SEGS 的档不进下拉框——一次课的几个段会摊出假趋势。

    真实库里 6:40 档只有 2 段 · 1.5km，滚动窗照样能给它算出 3 个点，
    画出来像「趋势」，其实是同一次课的两段。
    """
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = [
        # 5:00 档：4 段（够厚）→ 保留
        _act(dates.date_to_ts(mons[0]), 300.0, 150.0),
        _act(dates.date_to_ts(mons[0] + timedelta(days=1)), 300.0, 151.0),
        _act(dates.date_to_ts(mons[10]), 300.0, 149.0),
        _act(dates.date_to_ts(mons[10] + timedelta(days=1)), 300.0, 148.0),
        # 6:40 档：只有 2 段（薄）→ 挡掉
        _act(dates.date_to_ts(mons[15]), 400.0, 130.0, 1000),
        _act(dates.date_to_ts(mons[15] + timedelta(days=1)), 400.0, 131.0, 1000),
    ]
    out = pace_bin_weekly(acts, END, weeks=16)
    assert [b["start_s"] for b in out["bins"]] == [300]
    assert out["default_start_s"] == 300


def test_pace_bin_weekly_total_includes_pre_window_weeks():
    """薄样本下限看的是**总**样本：展示窗之前 roll-1 周的数据也算数。

    那些周不进画布，但滚动曲线的前几个点靠它们才立得住，若只数窗内样本
    会把「前几周练过、窗内还没练」的档误杀。
    """
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = [
        # wi=-1 与 wi=-2 各 2 段：窗内 0 段，但总样本 4 → 该档必须保留
        _act(dates.date_to_ts(mons[0] - timedelta(days=7)), 300.0, 160.0),
        _act(dates.date_to_ts(mons[0] - timedelta(days=6)), 300.0, 160.0),
        _act(dates.date_to_ts(mons[0] - timedelta(days=14)), 300.0, 162.0),
        _act(dates.date_to_ts(mons[0] - timedelta(days=13)), 300.0, 162.0),
    ]
    out = pace_bin_weekly(acts, END, weeks=16)
    assert [b["start_s"] for b in out["bins"]] == [300]
    assert out["bins"][0]["segs"] == 0          # 窗内确实没有样本
    assert out["bins"][0]["hr_roll"][0] == 161.0  # 但窗内第 1 点靠窗前数据立住


def test_pace_bin_weekly_clamps_bad_paces_and_ignores_out_of_window():
    """走路/坏数据配速与窗口外的活动都不参与，不撑偏坐标轴与均值。"""
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = [
        _act(dates.date_to_ts(mons[0]), 300.0, 150.0),
        _act(dates.date_to_ts(mons[0] + timedelta(days=1)), 300.0, 150.0),
        _act(dates.date_to_ts(mons[15]), 300.0, 140.0),
        _act(dates.date_to_ts(mons[15] + timedelta(days=1)), 300.0, 140.0),
        # 走路/通勤配速（>15:00/km）与坏数据（<3:00/km，心率还异常低）
        _act(dates.date_to_ts(mons[0]), 2000.0, 100.0, 3000),
        _act(dates.date_to_ts(mons[15]), 120.0, 100.0, 500),
        # 比最早一周还早 30 天：在窗口外
        _act(dates.date_to_ts(mons[0] - timedelta(days=30)), 300.0, 190.0),
    ]
    out = pace_bin_weekly(acts, END, weeks=16)
    assert [b["start_s"] for b in out["bins"]] == [300]
    b = out["bins"][0]
    assert b["segs"] == 4                 # 190 bpm 那条没被算进来
    assert b["hr"][0] == 150.0 and b["hr"][15] == 140.0


def test_pace_bin_weekly_empty_and_week_clamp():
    out = pace_bin_weekly([], END)
    assert out["bins"] == [] and out["default_start_s"] is None
    assert len(out["weeks"]) == 16
    # 周数边界钳制：最小 2 周、最多 52 周
    assert len(pace_bin_weekly([], END, weeks=1)["weeks"]) == 2
    assert len(pace_bin_weekly([], END, weeks=999)["weeks"]) == 52


def test_pace_bin_weekly_roll_fills_weeks_with_a_single_run():
    """主曲线走滚动窗口：每周只跑 1 次的周，当周点 hr 为 None，滚动点仍有值。

    这正是消除「断点 / 有跨度没数据」的机制——真实库里多数周每档就 1–2 次。
    """
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = [_act(dates.date_to_ts(mons[w]), 300.0, 150.0) for w in (4, 5, 6, 7)]
    out = pace_bin_weekly(acts, END, weeks=16)
    b = out["bins"][0]
    assert out["roll_weeks"] == 4
    # 当周口径：这 4 周每周都只有 1 次 → 全部不出点
    assert all(b["hr"][w] is None for w in (4, 5, 6, 7))
    assert sum(1 for v in b["hr"] if v is not None) == 0
    # 滚动口径：窗口 {1..4} 只有 1 次仍不足；{2..5} 起攒够 2 次
    assert b["hr_roll"][4] is None
    assert b["hr_roll"][5] == 150.0
    assert b["hr_roll"][7] == 150.0        # 窗口 {4..7} = 4 次
    assert sum(1 for v in b["hr_roll"] if v is not None) >= 3


def test_pace_bin_weekly_roll_is_distance_weighted_across_weeks():
    """滚动点 = 窗口内所有跑步的距离加权均值（跨周合并，不是各周均值再平均）。"""
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = [
        _act(dates.date_to_ts(mons[4]), 300.0, 140.0, 30000),   # 30km @140
        _act(dates.date_to_ts(mons[5]), 300.0, 160.0, 10000),   # 10km @160
        # 第 10 周补 2 次：同一档、离上面两个窗口都远，不影响原有断言；
        # 总样本 ≥ MIN_BIN_SEGS 才进 bins，否则这档会被薄样本下限挡掉
        _act(dates.date_to_ts(mons[10]), 300.0, 150.0, 10000),
        _act(dates.date_to_ts(mons[10] + timedelta(days=1)), 300.0, 150.0, 10000),
    ]
    b = pace_bin_weekly(acts, END, weeks=16)["bins"][0]
    assert b["hr_roll"][4] is None         # 窗口 {1..4} 只有 30km 一次 → 不足 2 次
    assert b["hr_roll"][5] == 145.0        # 窗口 {2..5}：跨周加权 (140×30k+160×10k)/40k
    assert b["hr_roll"][7] == 145.0        # 窗口 {4..7} 仍含两次
    assert b["hr_roll"][8] is None         # 窗口 {5..8} 只剩 10km 一次 → 滑出窗口
    assert b["hr_roll"][10] == 150.0       # 第 10 周自己那两次


def test_pace_bin_weekly_roll_reaches_before_display_window():
    """最早的几个点也要等宽滚动窗：展示窗之前 roll-1 周的数据必须算进来。

    （对应 bridge 多取 roll-1 周的取数契约；漏了这段，前几个点会偏薄。）
    """
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    acts = [
        # 展示窗开始前 3 周（wi=-3）：2 次 @160
        _act(dates.date_to_ts(mons[0] - timedelta(days=21)), 300.0, 160.0),
        _act(dates.date_to_ts(mons[0] - timedelta(days=20)), 300.0, 160.0),
        # 展示窗第 1 周：2 次 @150
        _act(dates.date_to_ts(mons[0]), 300.0, 150.0),
        _act(dates.date_to_ts(mons[0] + timedelta(days=1)), 300.0, 150.0),
    ]
    b = pace_bin_weekly(acts, END, weeks=16)["bins"][0]
    # wi=0 的窗口 {-3..0}：窗前的 2 次 + 当周 2 次 → 等权 155.0
    assert b["hr_roll"][0] == 155.0
    # 但当周实际值与统计量只认展示窗内的数据
    assert b["hr"][0] == 150.0
    assert b["segs"] == 2


# ---- 次内分段口径（pace_hr_segments）----


def test_pace_hr_segments_flattens_structure_and_skips_rest():
    """间歇课摊平成段级行：rest 剔除、recovery（热身冷身）保留、继承父活动时间戳。"""
    acts = [{
        "start_ts": 1000, "distance_m": 12000, "avg_pace_s_km": 300.0,
        "avg_hr": 150.0, "max_hr": 180.0,
        "structure_json": json.dumps([
            {"type": "recovery", "distance_m": 2000, "duration_s": 720,
             "pace_s_km": 360.0, "avg_hr": 130.0},
            {"type": "work", "distance_m": 4000, "duration_s": 800,
             "pace_s_km": 200.0, "avg_hr": 172.0},
            {"type": "rest", "distance_m": 2000, "duration_s": 900,
             "pace_s_km": 450.0, "avg_hr": 140.0},
            {"type": "work", "distance_m": 4000, "duration_s": 800,
             "pace_s_km": 198.0, "avg_hr": 174.0},
        ]),
    }]
    rows = pace_hr_segments(acts)
    assert [r["type"] for r in rows] == ["recovery", "work", "work"]  # rest 剔除
    assert {r["start_ts"] for r in rows} == {1000}   # 段级继承父活动时间戳
    assert {r["max_hr"] for r in rows} == {180.0}    # 段级没有峰值，借整场的
    assert [r["avg_pace_s_km"] for r in rows] == [360.0, 200.0, 198.0]
    assert [r["avg_hr"] for r in rows] == [130.0, 172.0, 174.0]
    assert rows[1]["distance_m"] == 4000


def test_pace_hr_segments_falls_back_to_whole_run_without_structure():
    """无 structure（CSV/FIT 手动导入不生成结构）回退整场一行，不丢整次跑。"""
    rows = pace_hr_segments([{
        "start_ts": 1000, "distance_m": 10000, "avg_pace_s_km": 305.0,
        "avg_hr": 148.0, "max_hr": 170.0, "structure_json": None,
    }])
    assert len(rows) == 1
    assert rows[0]["type"] == "whole"
    assert rows[0]["avg_pace_s_km"] == 305.0 and rows[0]["distance_m"] == 10000
    assert rows[0]["max_hr"] == 170.0 and rows[0]["start_ts"] == 1000


def test_pace_hr_segments_falls_back_when_structure_is_only_rest():
    """结构里只剩休息段（极端课表）同样回退整场，不产出空档。"""
    rows = pace_hr_segments([{
        "start_ts": 1000, "distance_m": 3000, "avg_pace_s_km": 400.0,
        "avg_hr": 120.0, "max_hr": 150.0,
        "structure_json": json.dumps([
            {"type": "rest", "distance_m": 3000, "duration_s": 1200,
             "pace_s_km": 400.0, "avg_hr": 120.0},
        ]),
    }])
    assert [r["type"] for r in rows] == ["whole"]


def test_pace_bin_weekly_interval_segments_reach_fast_bins():
    """整场口径看不到的快档：间歇课的 work 段被分段口径救出来。

    同一场 4:40/km 的间歇课（整场均值落在 4:40 档），其 work 段是 3:20——
    这正是「不单是一次跑步的平均配速，要精确到每次跑步内时段」的效果。
    """
    from runtrainer.utils import dates
    mons = _mondays(END, 16)
    whole_rows, seg_rows = [], []
    for wi in (0, 15):
        for d in (1, 2):
            a = _act(dates.date_to_ts(mons[wi] + timedelta(days=d)),
                     280.0, 150.0, 12000)
            whole_rows.append(a)
            # 每次跑拆出 2 个 3:20/km 的 work 段（各 4km，共 8km）
            seg_rows += [{**a, "avg_pace_s_km": 200.0, "avg_hr": 170.0,
                          "distance_m": 4000, "type": "work"}] * 2
    fast = next(b for b in pace_bin_weekly(seg_rows, END, weeks=16)["bins"]
                if b["start_s"] == 200)
    assert fast["label"] == "3:20"
    assert fast["week_segs"][0] == 4 and fast["week_segs"][15] == 4
    assert fast["hr"][0] == 170.0
    # 整场口径同一批数据里连 3:20 档都不存在
    whole_bins = [b["start_s"]
                  for b in pace_bin_weekly(whole_rows, END, weeks=16)["bins"]]
    assert 200 not in whole_bins and 280 in whole_bins


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
    """匀速跑按 心率/最大心率 六带归类（LT1/LT2 锚点，第十四批整改）；
    缺心率时无法归类。"""
    cases = [  # %HRmax → 带：115=58%恢复 130=65%低有氧 150=75%中有氧
        (115, "recovery"), (130, "easy"), (150, "aerobic"),
        # 165=83% 高有氧（LT1~LT2 之间）、180=90% 阈值（LT2 上）、196=98% 无氧
        (165, "high_aerobic"), (180, "tempo"), (196, "anaerobic"),
    ]
    for avg, kind in cases:
        w = classify_workout([], 1800, 5000, avg_hr=avg, max_hr=200)
        assert w["kind"] == kind
    # LT 锚点与区带元数据随分类输出（前端定位条用）
    w = classify_workout([], 1800, 5000, avg_hr=165, max_hr=200)
    assert w["lt1"] == 0.78 and w["lt2"] == 0.88 and w["hr_metric"] == "hrmax"
    assert [z["key"] for z in w["zones"]] == [
        "recovery", "easy", "aerobic", "high_aerobic", "tempo", "anaerobic"]
    assert "高有氧跑" in w["label"]
    # HRR 口径（静息心率已知）时标注 hrr
    w = classify_workout([], 1800, 5000, avg_hr=150, max_hr=200, rest_hr=50)
    assert w["hr_metric"] == "hrr"
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

    max=200 rest=42：HRR=158。60%=136.8 / 70%=152.6 / 78%=165.2(LT1) /
    88%=181(LT2) / 96%=193.7。
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
    # 高强度：190 → 94%HRR → 阈值（LT2 上沿，<96%）；194+ 才是无氧
    w = classify_workout([], 1800, 5000, avg_hr=190, max_hr=200, rest_hr=42)
    assert w["kind"] == "tempo"
    w = classify_workout([], 1800, 5000, avg_hr=194, max_hr=200, rest_hr=42)
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
