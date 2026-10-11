"""丹尼尔斯 VDOT 训练表（domain/training_table + plan_service 默认档 + bridge）。"""
from datetime import timedelta

from runtrainer.domain import training_table as tt
from runtrainer.domain import vdot as vd
from runtrainer.utils import dates


# ---------- domain ----------

def test_matrix_covers_full_range_in_steps_of_one():
    rows = tt.vdot_matrix()
    assert [r["vdot"] for r in rows] == list(range(tt.VDOT_MIN, tt.VDOT_MAX + 1))
    assert len(rows) == 56


def test_matrix_paces_are_ordered_within_and_across_rows():
    """行内：同一档 E 慢端 > 快端，且 M>T1>T>I>R（%VDOT 越高配速越快）；
    行间：VDOT 越高，同一区间的配速越快（秒/公里越小）。"""
    rows = tt.vdot_matrix()
    for r in rows:
        assert r["E"][0] > r["E"][1]                       # 慢端 > 快端（s/km）
        assert r["M"] > r["T1"] > r["T"] > r["I"] > r["R"]
    for a, b in zip(rows, rows[1:]):
        for k in ("M", "T1", "T", "I", "R"):
            assert b[k] < a[k]
        assert b["E"][1] < a["E"][1]


def test_matrix_rows_carry_equivalent_races():
    rows = {r["vdot"]: r for r in tt.vdot_matrix()}
    r50 = rows[50]
    # 四个常用距离全在（与页首等效成绩、vdot.DISTANCES 同源）
    assert set(r50["races"]) == {"5K", "10K", "半马", "全马"}
    assert 0 < r50["races"]["5K"] < r50["races"]["10K"] \
        < r50["races"]["半马"] < r50["races"]["全马"]      # 距离越长用时越长
    assert rows[60]["races"]["5K"] < r50["races"]["5K"]    # VDOT 越高 5K 越快
    assert rows[60]["races"]["全马"] < r50["races"]["全马"]


def test_matrix_race_columns_match_domain_distances():
    """矩阵附加列必须就是 vdot.DISTANCES（加距离只改那一处）。"""
    assert [k for k, _ in tt.MATRIX_RACES] == list(vd.DISTANCES)
    r = tt.vdot_matrix()[0]
    for label, dist in tt.MATRIX_RACES:
        assert r["races"][label] == round(vd.predict_time(dist, float(r["vdot"])))


def test_matrix_paces_match_pace_table():
    """矩阵里的配速必须与课表用的 pace_table 完全一致（不另造一套数）。"""
    t = vd.pace_table(54.0)
    row = {r["vdot"]: r for r in tt.vdot_matrix()}[54]
    assert row["M"] == t["M"] and row["T1"] == t["T1"]
    assert row["T"] == t["T"] and row["I"] == t["I"] and row["R"] == t["R"]
    assert row["E"] == [t["E"]["slow_s_km"], t["E"]["fast_s_km"]]


def test_zone_hr_bands_stay_inside_hrr_range_and_are_non_decreasing():
    bands = [tt.zone_hr_band(z["key"]) for z in vd.PACE_ZONES]
    assert all(b is not None for b in bands)
    for b in bands:
        assert 0.0 <= b["lo"] < b["hi"] <= 1.0
        assert b["label"]
    for a, b in zip(bands, bands[1:]):          # 配速越高（越快）心率带越高
        assert b["hi"] >= a["hi"]


def test_zone_hr_bands_use_existing_hr_zone_boundaries():
    """心率带边界只能取自 workout_analysis 的 HR_ZONE_KINDS，不得新造数字。"""
    from runtrainer.domain.workout_analysis import HR_ZONE_KINDS
    edges = {round(lo, 3) for _, lo, _, _ in HR_ZONE_KINDS} | \
            {round(hi, 3) for _, _, hi, _ in HR_ZONE_KINDS}
    for z in vd.PACE_ZONES:
        b = tt.zone_hr_band(z["key"])
        assert round(b["lo"], 3) in edges and round(b["hi"], 3) in edges


def test_typical_workouts_all_resolve_in_catalog():
    """每个区的典型课键都必须在课表模板池里真实存在，且带名字与说明。"""
    pool = tt._catalog_templates()
    for zone_key, keys in tt.ZONE_SESSION_KEYS.items():
        got = tt.typical_workouts(zone_key)
        assert len(got) == len(keys), (zone_key, [s["key"] for s in got])
        for s in got:
            assert s["name"] and s["detail"]
            t = pool[s["key"]]
            assert s["name"] == t.name and s["detail"] == t.description


def test_every_pace_zone_has_at_least_one_typical_workout():
    for z in vd.PACE_ZONES:
        assert tt.typical_workouts(z["key"])


def test_each_anchor_belongs_to_exactly_one_zone():
    """M/T1/T/I/R 各锚点在表里只出现一次，且归属与「较慢的那一区」一致。"""
    table = tt.daniels_table(54.0)
    marks = [a["mark"] for z in table["zones"] for a in z["anchors"]]
    assert sorted(marks) == ["I", "M", "R", "T", "T1"]
    by_key = {z["key"]: [a["mark"] for a in z["anchors"]] for z in table["zones"]}
    assert by_key["aerobic"] == ["M"]
    assert by_key["threshold"] == ["T1", "T"]
    assert by_key["vo2max"] == ["I"]
    assert by_key["anaerobic"] == ["R"]


def test_anchor_pace_matches_pace_table_anchor():
    table = tt.daniels_table(54.0)
    t = vd.pace_table(54.0)
    got = {a["mark"]: a["pace_s_km"] for z in table["zones"] for a in z["anchors"]}
    assert got == {"M": t["M"], "T1": t["T1"], "T": t["T"], "I": t["I"], "R": t["R"]}


def test_zone_bpm_span_from_rest_and_max_hr():
    table = tt.daniels_table(54.0, rest_hr=52.0, max_hr=190.0)
    span = 190.0 - 52.0
    for z in table["zones"]:
        hr = z["hr"]
        assert hr["bpm_lo"] == round(52 + hr["lo"] * span)
        assert hr["bpm_hi"] == round(52 + hr["hi"] * span)
        assert 52 <= hr["bpm_lo"] <= hr["bpm_hi"] <= 190


def test_zone_bpm_omitted_without_hr_profile():
    table = tt.daniels_table(54.0)
    assert table["rest_hr"] is None
    assert all("bpm_lo" not in z["hr"] for z in table["zones"])


def test_daniels_table_has_zones_equivalents_and_matrix():
    table = tt.daniels_table(54.0)
    assert len(table["zones"]) == len(vd.PACE_ZONES)
    assert set(table["equivalent"]) == {"5K", "10K", "半马", "全马"}
    assert len(table["matrix"]) == 56
    assert table["matrix_min"] == tt.VDOT_MIN and table["matrix_max"] == tt.VDOT_MAX


# ---------- service：默认档解析 ----------

def _patch(monkeypatch, *, cached=None, ability=None, plan=None):
    from runtrainer.db.repos import plan_repo
    from runtrainer.services import plan_service
    calls = {"ability": 0}

    def _ability(*a, **kw):
        calls["ability"] += 1
        return ability or {}

    monkeypatch.setattr(plan_service, "ability_assessment", _ability)
    monkeypatch.setattr(plan_service, "quick_assessment_cached", lambda *a, **kw: cached)
    monkeypatch.setattr(plan_repo, "get_active_plan", lambda: plan)
    return calls


def test_context_falls_back_to_default_without_any_evidence(monkeypatch):
    from runtrainer.services import plan_service
    calls = _patch(monkeypatch)
    ctx = plan_service.vdot_table_context()
    assert (ctx["vdot"], ctx["vdot_source"]) == (tt.VDOT_DEFAULT, "default")
    assert ctx["estimate_vdot"] is None and ctx["plan_vdot"] is None
    assert calls["ability"] == 1        # 无缓存时探一次重口径


def test_context_prefers_estimate_over_plan(monkeypatch):
    from runtrainer.services import plan_service
    _patch(monkeypatch, cached={"vdot": 48.0, "window_days": 21}, plan={"vdot": 45.0})
    ctx = plan_service.vdot_table_context()
    assert (ctx["vdot"], ctx["vdot_source"]) == (48.0, "quick")
    assert "21" in ctx["vdot_label"]
    assert ctx["plan_vdot"] == 45.0     # 仍带出课表档，供「用课表」按钮


def test_context_uses_long_baseline_when_no_quick_cache(monkeypatch):
    from runtrainer.services import plan_service
    calls = _patch(monkeypatch, ability={"vdot": 52.5}, plan={"vdot": 45.0})
    ctx = plan_service.vdot_table_context()
    assert (ctx["vdot"], ctx["vdot_source"]) == (52.5, "ability")
    assert ctx["vdot_label"] == "当前水平预估"
    assert calls["ability"] == 1


def test_context_falls_back_to_plan_when_no_estimate(monkeypatch):
    from runtrainer.services import plan_service
    _patch(monkeypatch, plan={"vdot": 45.0})
    ctx = plan_service.vdot_table_context()
    assert (ctx["vdot"], ctx["vdot_source"]) == (45.0, "plan")
    assert ctx["vdot_label"] == "当前课表"


def test_context_explicit_vdot_wins_and_skips_heavy_path(monkeypatch):
    from runtrainer.services import plan_service
    calls = _patch(monkeypatch, cached={"vdot": 48.0}, plan={"vdot": 45.0})
    ctx = plan_service.vdot_table_context(61.0)
    assert (ctx["vdot"], ctx["vdot_source"]) == (61.0, "manual")
    assert calls["ability"] == 0        # 显式指定时不需重口径


def test_context_passes_profile_hr(monkeypatch):
    from runtrainer.db.repos import profile_repo
    from runtrainer.services import plan_service
    _patch(monkeypatch)
    profile_repo.upsert_profile({"rest_hr": 52, "max_hr": 190})
    ctx = plan_service.vdot_table_context(54.0)
    assert ctx["rest_hr"] == 52 and ctx["max_hr"] == 190


# ---------- bridge ----------

def test_bridge_get_vdot_table_envelope():
    from runtrainer.api.bridge import Api
    res = Api().get_vdot_table()
    assert res["ok"] is True
    d = res["data"]
    assert d["vdot"] == tt.VDOT_DEFAULT      # 空库：无课表无预估 → 默认档
    assert d["vdot_source"] == "default"
    assert len(d["zones"]) == 6 and len(d["matrix"]) == 56
    assert set(d["equivalent"]) == {"5K", "10K", "半马", "全马"}


def test_bridge_get_vdot_table_with_explicit_vdot():
    from runtrainer.api.bridge import Api
    res = Api().get_vdot_table(60.0)
    assert res["ok"] is True
    assert res["data"]["vdot"] == 60.0
    assert res["data"]["vdot_source"] == "manual"


def test_bridge_get_vdot_table_uses_active_plan_vdot():
    from runtrainer.api.bridge import Api
    from runtrainer.services import plan_service
    race = dates.today() + timedelta(days=84)
    plan_service.create_goal_and_plan({
        "goal": {"distance_m": 21097, "race_date": race.isoformat(),
                 "target_seconds": None, "vdot": 45.0, "name": "半马"},
        "plan": {"base_weekly_km": 40.0},
    })
    res = Api().get_vdot_table()
    assert res["ok"] is True
    assert res["data"]["plan_vdot"] == 45.0
    assert res["data"]["vdot"] == 45.0          # 无预估 → 用课表档
    assert res["data"]["vdot_source"] == "plan"
