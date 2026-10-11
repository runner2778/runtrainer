"""AI 教练升级（批 32）单元测试：训练刺激-恢复 / 今日决策引擎 / 运动员画像 /
提示词新块 / 教练接线（decision_snapshot、单调性个人校准）。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from runtrainer.ai import prompt_builder
from runtrainer.domain import athlete_model, stimulus
from runtrainer.domain import today_decision as td
from runtrainer.services import coach_service, plan_service
from runtrainer.utils import dates

TODAY = date(2026, 10, 3)


def _w(day_offset: int, kind: str, km: float, dur_min: float | None = None,
       **kw) -> dict:
    d = (TODAY + timedelta(days=day_offset)).isoformat()
    return {"id": abs(day_offset), "date": d, "kind": kind,
            "distance_km": km,
            "duration_min": dur_min if dur_min is not None else km * 6, **kw}


def _ts(day_offset: int) -> float:
    return datetime.combine(TODAY + timedelta(days=day_offset),
                            time(8, 0)).timestamp()


def _act(day_offset: int, pace_s: float = 300, hr: float = 141, **kw) -> dict:
    return {"id": abs(day_offset), "start_ts": _ts(day_offset),
            "avg_pace_s_km": pace_s, "avg_hr": hr, "max_hr": hr + 15,
            "duration_s": 40 * 60, "distance_m": 8000, "sport": "running",
            "has_samples": False, "structure_json": None, **kw}


def _h(day_offset: int, rhr: float | None = 50, hrv: float | None = 60,
        sleep_h: float | None = 7) -> dict:
    return {"date": (TODAY + timedelta(days=day_offset)).isoformat(),
            "resting_hr": rhr, "hrv_avg_ms": hrv,
            "sleep_duration_s": int(sleep_h * 3600) if sleep_h else None}


BASE_ROWS = [_h(-i) for i in range(14, 0, -1)]
QW = {"kind": "T", "distance_km": 10, "duration_min": 40,
      "date": TODAY.isoformat()}


# ---------------------------------------------------------------------------
# 训练刺激-恢复模型
# ---------------------------------------------------------------------------

class TestStimulus:
    def test_kind_costs(self):
        assert stimulus.stimulus_of(_w(0, "E", 10))["cost_au"] == 10.0
        assert stimulus.stimulus_of(_w(0, "T", 10))["cost_au"] == 25.0
        assert stimulus.stimulus_of(_w(0, "I", 5))["cost_au"] == 15.0
        assert stimulus.stimulus_of(_w(0, "R", 5))["cost_au"] == 17.5
        assert stimulus.stimulus_of(_w(0, "RACE", 10))["cost_au"] == 40.0
        # 力量课按次计费
        assert stimulus.stimulus_of(_w(0, "STRENGTH", 0, 40))["cost_au"] == 8.0
        # 未知课型按轻松跑处理（不臆造刺激）
        assert stimulus.stimulus_of(_w(0, "WTF", 5))["cost_au"] == 5.0

    def test_quality_and_stamina(self):
        assert stimulus.stimulus_of(_w(0, "I", 10))["quality"]
        assert not stimulus.stimulus_of(_w(0, "E", 10))["quality"]
        assert not stimulus.stimulus_of(_w(0, "RECOVERY", 6))["quality"]
        # 长距离耐力刺激：≥15km 或 ≥90 分钟
        assert stimulus.stimulus_of(_w(0, "E", 16, 96))["stamina_min"] > 0
        assert stimulus.stimulus_of(_w(0, "E", 10, 95))["stamina_min"] > 0
        assert stimulus.stimulus_of(_w(0, "E", 10, 60))["stamina_min"] == 0

    def test_acute_fatigue_decay(self):
        # 20km 阈值 = 50 AU，3 天前 → 半衰期 3 天 → 残留 25
        fat = stimulus.acute_fatigue([_w(-3, "T", 20)], TODAY)
        assert fat["residual_au"] == pytest.approx(25.0, abs=0.2)
        assert fat["n"] == 1
        # 未来的课不计入「已发生」的疲劳
        fat2 = stimulus.acute_fatigue([_w(0, "T", 20), _w(1, "T", 20)], TODAY)
        assert fat2["n"] == 1

    def test_fatigue_forecast(self):
        fc = stimulus.fatigue_forecast([_w(0, "T", 10)], TODAY, days=3)
        assert fc[0]["date"] == (TODAY + timedelta(days=1)).isoformat()
        assert fc[0]["residual_au"] == pytest.approx(25 * 0.5 ** (1 / 3), abs=0.2)
        assert fc[2]["residual_au"] < fc[0]["residual_au"]  # 随时间衰减

    def test_quality_baseline_needs_samples(self):
        # 质量课 <3 节 → None（调用方回退群体上限）
        assert stimulus.quality_baseline([_w(-1, "E", 8), _w(-2, "T", 8)],
                                         TODAY) is None
        q = [_w(-5, "T", 10), _w(-10, "I", 8), _w(-15, "T", 12)]
        base = stimulus.quality_baseline(q + [_w(-1, "E", 10)], TODAY)
        assert base and base["n"] == 3
        assert base["median_au"] >= 0

    def test_quality_flags_personal_calibration(self):
        # 个人基线中位 20 → 上限 max(25, 27) = 27；
        # 昨天 30km 阈值（75 AU）在 +2 天残留 37.5 > 27 → 提示偏紧
        ws = [_w(-1, "T", 30), _w(2, "T", 10)]
        flags = stimulus.quality_day_flags(ws, TODAY, baseline={"n": 10, "median_au": 20.0})
        by_date = {f["date"]: f for f in flags}
        assert by_date[(TODAY + timedelta(days=2)).isoformat()]["flag"] == "fatigued"
        # 个人基线上限抬到 99 时同一残留不算偏紧（真实库校准后的形态）
        flags2 = stimulus.quality_day_flags(ws, TODAY, baseline={"n": 10, "median_au": 74.0})
        assert all(f["flag"] == "ok" for f in flags2)

    def test_quality_flags_taper(self):
        # 赛前 14 天内：质量课按课表执行，不提示课前疲劳
        ws = [_w(-1, "T", 30), _w(2, "T", 10)]
        flags = stimulus.quality_day_flags(ws, TODAY,
                                           race_date=TODAY + timedelta(days=10))
        assert all(f["flag"] == "taper" for f in flags)

    def test_week_balance(self):
        ws = [_w(-2, "T", 8), _w(-1, "E", 10)]
        wb = stimulus.week_balance(ws, goal_label="半马",
                                   week_start=TODAY - timedelta(days=2), today=TODAY)
        assert not wb["ok"]
        assert any("长距离耐力" in m for m in wb["missing"])
        ws2 = [_w(-2, "T", 8), _w(-1, "E", 16, 96)]
        wb2 = stimulus.week_balance(ws2, goal_label="半马",
                                    week_start=TODAY - timedelta(days=2), today=TODAY)
        assert wb2["ok"]

    def test_describe_stimulus(self):
        txt = stimulus.describe_stimulus(_w(0, "T", 10, 40))
        assert "阈值刺激" in txt and "AU" in txt


# ---------------------------------------------------------------------------
# 今日决策引擎
# ---------------------------------------------------------------------------

class TestTodayDecision:
    def test_execute_when_clean(self):
        d = td.decide(QW, None, BASE_ROWS, {}, None, TODAY)
        assert d["verdict"] == "execute" and d["score"] == 0

    def test_hrv_low_caution(self):
        d = td.decide(QW, _h(0, 50, 40, 7), BASE_ROWS, {}, None, TODAY)
        assert d["verdict"] == "execute_caution" and d["score"] == 1
        assert any("HRV" in s["name"] for s in d["signals"])

    def test_rhr_high_reduce(self):
        d = td.decide(QW, _h(0, 56, 60, 7), BASE_ROWS, {}, None, TODAY)
        assert d["verdict"] == "reduce" and d["score"] == 2

    def test_sleep_short_caution(self):
        d = td.decide(QW, _h(0, 50, 60, 4), BASE_ROWS, {}, None, TODAY)
        assert d["verdict"] == "execute_caution"
        assert any("睡眠" in s["name"] for s in d["signals"])

    def test_stacked_signals_easy_or_rest(self):
        d = td.decide(QW, _h(0, 56, 60, 4), BASE_ROWS, {}, None, TODAY)
        assert d["verdict"] == "easy_or_rest" and d["score"] >= 3

    def test_rhr_trend_hard_rest(self):
        rows = [_h(-4, 48, 60), _h(-3, 52, 60), _h(-2, 55, 60), _h(-1, 58, 60)]
        d = td.decide(QW, None, rows, {}, None, TODAY)
        assert d["verdict"] == "rest"
        assert d["score"] >= td.INJURY_PENALTY
        assert any("伤病指征" in s["name"] for s in d["signals"])

    def test_hrv_trend_yellow_not_rest(self):
        # HRV 3 天连降 ≥15ms：黄色信号（+2），不是硬性休息（手表噪声大）
        rows = [_h(-4, 50, 80), _h(-3, 50, 75), _h(-2, 50, 70), _h(-1, 50, 60)]
        d = td.decide(QW, None, rows, {}, None, TODAY)
        assert d["verdict"] == "reduce"
        assert any("HRV 连续下降" in s["name"] for s in d["signals"])

    def test_hrv_trend_small_drop_ignored(self):
        rows = [_h(-4, 50, 80), _h(-3, 50, 78), _h(-2, 50, 76), _h(-1, 50, 75)]
        d = td.decide(QW, None, rows, {}, None, TODAY)
        assert d["verdict"] == "execute"

    def test_monotony_population_threshold(self):
        d = td.decide(QW, None, BASE_ROWS, {"monotony": 2.5}, None, TODAY)
        assert d["verdict"] == "execute_caution"

    def test_monotony_personal_calibration(self):
        # 群体阈值对高密度课表常年超限：个人 P90 4.8 时，3.0 是本人正常水平
        d = td.decide(QW, None, BASE_ROWS,
                      {"monotony": 3.0, "monotony_p90": 4.8}, None, TODAY)
        assert d["verdict"] == "execute"
        assert not any("单调性" in s["name"] for s in d["signals"])
        d2 = td.decide(QW, None, BASE_ROWS,
                       {"monotony": 5.2, "monotony_p90": 4.8}, None, TODAY)
        assert any("单调性" in s["name"] for s in d2["signals"])

    def test_taper_suppresses_load_signals(self):
        recent = {"acwr": {"ratio": 2.0}, "monotony": 3.0,
                  "consecutive_days": 20}
        d = td.decide(QW, None, BASE_ROWS, recent, 10, TODAY)
        assert d["verdict"] == "execute"
        assert any("减量期" in s["name"] for s in d["signals"])

    def test_yesterday_hard_and_consecutive(self):
        d = td.decide(QW, None, BASE_ROWS, {"yesterday_hard": True}, None, TODAY)
        assert d["verdict"] == "reduce"
        d2 = td.decide(QW, None, BASE_ROWS, {"consecutive_days": 12}, None, TODAY)
        assert d2["verdict"] == "execute_caution"
        # 轻松课日不计连续天数罚分（轻松跑本身就是恢复）
        easy = {"kind": "E", "distance_km": 8, "duration_min": 45,
                "date": TODAY.isoformat()}
        d3 = td.decide(easy, None, BASE_ROWS, {"consecutive_days": 12}, None, TODAY)
        assert d3["verdict"] == "execute"

    def test_acwr_quality_vs_easy_day(self):
        recent = {"acwr": {"ratio": 1.6, "acute_km": 80, "chronic_km": 50}}
        d = td.decide(QW, None, BASE_ROWS, recent, None, TODAY)
        assert d["verdict"] == "reduce"
        easy = {"kind": "E", "distance_km": 8, "duration_min": 45,
                "date": TODAY.isoformat()}
        d2 = td.decide(easy, None, BASE_ROWS, recent, None, TODAY)
        assert d2["verdict"] == "execute"
        assert any("轻松课" in s["name"] for s in d2["signals"])

    def test_rest_day_no_mechanical_makeup(self):
        d = td.decide(None, None, BASE_ROWS, {}, None, TODAY)
        assert d["verdict"] == "rest_day"
        assert "机械" in d["note"]


# ---------------------------------------------------------------------------
# 运动员画像
# ---------------------------------------------------------------------------

class TestAthleteModel:
    def test_pace_bin(self):
        assert athlete_model._pace_bin(299) == 280
        assert athlete_model._pace_bin(300) == 300
        assert athlete_model._pace_bin(321) == 320

    def test_expected_hr(self):
        model = {"pace_hr": {"bins": {"300": {"mean": 141.0, "n": 60,
                                               "std": 9.0}}}}
        exp = athlete_model.expected_hr(model, 305)
        assert exp == {"hr": 141.0, "n": 60, "std": 9.0}
        assert athlete_model.expected_hr(model, None) is None
        assert athlete_model.expected_hr(model, 200) is None

    def test_build_and_describe_minimal(self):
        acts = [_act(-i) for i in range(1, 4)]  # 3 次同配速 → 1 个档位
        model = athlete_model.build(acts, [], [], lambda aid: [], {}, TODAY)
        lines = athlete_model.describe(model)
        assert any("5:00/km" in l and "n=3" in l for l in lines)
        # 空模型 → 数据不足，不装精确
        assert "数据不足" in athlete_model.describe({})[0]

    def test_split_negative_split(self):
        def samples(aid):
            rows = []
            for i in range(90):
                speed = 3.0 if i < 30 else (3.2 if i >= 60 else 3.1)
                rows.append({"t_offset_s": i * 10, "speed_mps": speed,
                             "hr": 140 + i % 3})
            return rows
        act = _act(-1, has_samples=True, duration_s=30 * 60 + 10)
        sp = athlete_model._split([act], samples, TODAY)
        assert sp["n"] == 1
        assert sp["median_ratio"] < 0.95
        assert "后程快" in sp["style"]

    def test_drift_positive(self):
        def samples(aid):
            return [{"t_offset_s": i * 10,
                     "hr": 140.0 if i < 60 else 150.0} for i in range(120)]
        acts = [_act(-i, has_samples=True, duration_s=40 * 60)
                for i in range(1, 4)]
        dr = athlete_model._drift(acts, samples, TODAY)
        assert dr["n"] == 3
        assert dr["median_bpm"] == pytest.approx(10.0, abs=0.5)
        assert dr["by_duration"]["40-60"]["median"] == pytest.approx(10.0, abs=0.5)


# ---------------------------------------------------------------------------
# 提示词新块
# ---------------------------------------------------------------------------

class TestPromptBlocks:
    def _ctx(self) -> dict:
        return {
            "today": TODAY.isoformat(),
            "athlete": {"schedule_note": "周三晚有课，不安排强度课"},
            "athlete_model_lines": ["配速 5:00/km 心率基线 141±9 bpm（n=60）"],
            "today_decision": {
                "verdict": "reduce", "verdict_label": "降低强度或减量",
                "score": 2, "note": "疲劳信号明显",
                "signals": [{"name": "静息心率偏高", "penalty": 2,
                             "evidence": "今日 RHR 45 ≥ 基线 39+5"}]},
            "today_workout": _w(0, "T", 10, 40),
            "stimulus": {"fatigue": {"n": 1, "residual_au": 30.0,
                                     "top": [{"date": TODAY.isoformat(),
                                              "kind": "T", "residual": 30.0}]},
                         "quality_flags": [], "week_balance": {"ok": True,
                                                              "week_au": 30.0}},
            "miss_events": [{"date": "2026-10-01", "kind": "E",
                             "event": "missed", "planned_km": 8.0}],
        }

    def test_new_blocks_present(self):
        text = "\n".join(prompt_builder._context_blocks(self._ctx()))
        assert "【运动员长期画像" in text
        assert "生活约束备注" in text and "周三晚有课" in text
        assert "【今日决策支持（本地计算）】" in text
        assert "降低强度或减量" in text and "罚分 2" in text
        assert "【训练刺激与恢复剖面】" in text and "阈值刺激" in text
        assert "【漏训与偏差事件（近 7 天，本地判定）】" in text
        assert "不机械补课" in text

    def test_missing_data_blocks_graceful(self):
        text = "\n".join(prompt_builder._context_blocks({"today": TODAY.isoformat()}))
        assert "（画像数据不足）" in text
        assert "（今日决策数据不足）" in text
        assert "（近 7 天无漏训/少跑/多跑事件）" in text

    def test_sync_analysis_personal_expectation(self):
        ctx = {"today": TODAY.isoformat(),
               "athlete_model": {"pace_hr": {"bins": {"300": {
                   "mean": 141.0, "n": 60, "std": 9.0}}}}}
        acts = [{"start_ts": _ts(-1), "name": "轻松跑", "avg_pace_s_km": 305,
                 "avg_hr": 130.0}]
        p = prompt_builder.build_sync_analysis(ctx, acts)
        assert "个人基线 141±9 bpm" in p["user"]
        assert "偏低（同配速进步信号）" in p["user"]


# ---------------------------------------------------------------------------
# 教练接线
# ---------------------------------------------------------------------------

@pytest.fixture()
def plan_fixture():
    race = TODAY + timedelta(days=84)
    payload = plan_service.create_goal_and_plan({
        "goal": {"distance_m": 21097, "race_date": race.isoformat(),
                 "target_seconds": None, "vdot": 45.0, "name": "半马"},
        "plan": {"base_weekly_km": 40.0},
    })
    from runtrainer.db.repos import plan_repo
    return plan_repo.get_plan(payload["plan_id"])


def _quality_day(plan_fixture, monkeypatch):
    from runtrainer.db.repos import plan_repo
    ws = plan_repo.get_workouts(plan_fixture["id"])
    for w in ws:
        if w["kind"] in ("T", "I") and 2 <= int(w["week_index"]) <= 8:
            d = date.fromisoformat(w["date"])
            monkeypatch.setattr("runtrainer.utils.dates.today", lambda: d)
            return d, w
    raise RuntimeError("计划中未找到强度课")


class TestCoachWiring:
    def test_monotony_p90(self):
        sessions = []
        for i in range(30):
            sessions.append({"date": (TODAY - timedelta(days=i)).isoformat(),
                             "distance_km": 8 + (i % 5) * 2,
                             "duration_min": 60, "sport": "running"})
        p90 = coach_service._monotony_p90(sessions, TODAY)
        assert p90 is not None and p90 >= 0
        # 样本不足 → None（回退群体阈值）
        assert coach_service._monotony_p90(sessions[:5], TODAY) is None

    def test_decision_snapshot_on_quality_day(self, plan_fixture, monkeypatch):
        d, _ = _quality_day(plan_fixture, monkeypatch)
        ds = coach_service.decision_snapshot(d)
        assert ds is not None
        dec = ds["decision"]
        assert dec["date"] == d.isoformat()
        assert dec["quality_today"] is True
        # 无任何健康/负荷信号 → 按课表执行
        assert dec["verdict"] == "execute"
        # 画像模型在空库上也能建（全特征带 n，不炸）
        assert ds["model"]["pace_hr"]["bins"] == {}

    def test_get_snapshot_payload(self, plan_fixture, monkeypatch):
        d, _ = _quality_day(plan_fixture, monkeypatch)
        snap = coach_service.get_snapshot()
        assert snap["has_active_plan"] is True
        assert snap["today"] == d.isoformat()
        assert snap["today_decision"]["verdict"] == "execute"
        assert isinstance(snap["athlete_model_lines"], list)
        assert snap["schedule_note"] == ""

    def test_get_plan_progress_carries_decision(self, plan_fixture, monkeypatch):
        d, _ = _quality_day(plan_fixture, monkeypatch)
        from runtrainer.api.bridge import Api
        r = Api().get_plan_progress()
        assert r["ok"] is True
        dec = r["data"]["today_decision"]
        assert dec["date"] == d.isoformat()
        assert dec["verdict"] in td.VERDICT_LABELS
