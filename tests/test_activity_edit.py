"""手动录入/编辑的纯函数层：主观量表归一、派生量、时间解析。"""
from datetime import datetime

from runtrainer.domain import activity_edit as ae


def test_norm_feel_accepts_garmin_and_direct_scales():
    # 佳明 Connect 的 0/25/50/75/100
    assert [ae.norm_feel(v) for v in (25, 50, 75, 100)] == [1, 3, 4, 5]
    # 用户直接填 1-5
    assert [ae.norm_feel(v) for v in (1, 3, 5)] == [1, 3, 5]
    # 0 与 None 都是「没打标签」
    assert ae.norm_feel(0) is None
    assert ae.norm_feel(None) is None
    assert ae.norm_feel("") is None
    # 越界夹紧、字符串数字可用
    assert ae.norm_feel(999) == 5
    assert ae.norm_feel("4") == 4


def test_norm_rpe_accepts_garmin_and_direct_scales():
    assert [ae.norm_rpe(v) for v in (20, 30, 50, 80, 100)] == [2, 3, 5, 8, 10]
    assert [ae.norm_rpe(v) for v in (1, 5, 10)] == [1, 5, 10]
    assert ae.norm_rpe(0) is None
    assert ae.norm_rpe(None) is None
    assert ae.norm_rpe(1000) == 10


def test_rpe_ten_is_read_as_direct_scale():
    """两种量纲在 10 这一档重合：1-10 量纲的 10 = 极限，0-100 量纲的 10 = RPE 1。
    表单里用户直接填 1-10 是主用法，所以 10 一律按「极限」解释。"""
    assert ae.norm_rpe(10) == 10
    assert ae.norm_rpe(11) == 1        # 已超出 1-10 → 必是 0-100 量纲


def test_norm_feel_never_confuses_the_two_scales():
    """5 在两种量纲下含义不同：1-5 量纲的 5 = 非常吃力；0-100 量纲没有 5。
    5 必须解释成「非常吃力」，而不是 0-100 里的 5 分（→1）。"""
    assert ae.norm_feel(5) == 5
    assert ae.norm_rpe(5) == 5
    # 6 已超出 1-5 量纲，必然来自 0-100（6/20≈0 → 夹紧到 1）
    assert ae.norm_feel(6) == 1


def test_derive_pace_and_stride():
    # 10km / 50min → 300 s/km
    assert ae.derive_pace(10000, 3000) == 300.0
    assert ae.derive_pace(0, 3000) is None
    assert ae.derive_pace(10000, None) is None
    # 300 s/km = 3.333 m/s；步频 180 spm → 3.333/(180/60) = 1.111 m
    assert ae.derive_stride(300.0, 180) == 1.111
    assert ae.derive_stride(None, 180) is None
    assert ae.derive_stride(300.0, 0) is None


def test_norm_local_datetime_parses_local_wall_clock():
    got = ae.norm_local_datetime("2026-10-06 07:12")
    assert got is not None
    ts, tz = got
    local = datetime.fromtimestamp(ts)
    assert (local.year, local.month, local.day, local.hour, local.minute) == \
        (2026, 10, 6, 7, 12)
    assert ae.norm_local_datetime("2026/10/06") is not None
    assert ae.norm_local_datetime("2026-10-06T07:12:30") is not None
    assert ae.norm_local_datetime("明天早上") is None
    assert ae.norm_local_datetime("2026-13-45 99:99") is None
    assert ae.norm_local_datetime(None) is None


def test_norm_sport_aliases_and_default():
    assert ae.norm_sport("跑步机") == "treadmill_running"
    assert ae.norm_sport("Treadmill") == "treadmill_running"
    assert ae.norm_sport("越野跑") == "trail_running"
    assert ae.norm_sport("骑行") == "cycling"
    assert ae.norm_sport("") == "running"
    assert ae.norm_sport("蹦极") == "running"      # 未知运动退回跑步


def test_build_row_derives_and_respects_manual_inputs():
    row = ae.build_row({"distance_m": 10000, "duration_s": 3000,
                        "avg_cadence": 180, "rpe": 7, "feel": 4,
                        "name": "  晚跑  "})
    assert row["name"] == "晚跑"
    assert row["avg_pace_s_km"] == 300.0
    assert row["stride_length_m"] == 1.111        # 步幅由配速+步频推导
    assert row["rpe"] == 7 and row["feel"] == 4
    assert row["manual"] == 1 and row["has_samples"] == 0
    assert row["source"] == "manual"
    # 手填的派生量不被覆盖
    hand = ae.build_row({"distance_m": 10000, "duration_s": 3000,
                         "avg_pace_s_km": 250, "stride_length_m": 1.5})
    assert hand["avg_pace_s_km"] == 250
    assert hand["stride_length_m"] == 1.5
    # 缺距离或缺用时也能入库（力量课只填时长）
    partial = ae.build_row({"duration_s": 1800, "name": "核心训练"})
    assert partial["duration_s"] == 1800
    assert partial["distance_m"] is None
    assert partial["avg_pace_s_km"] is None
    # 名称缺省
    assert ae.build_row({})["name"] == "手动录入"


def test_build_row_uses_passed_wall_clock_when_no_start_ts():
    row = ae.build_row({"duration_s": 600, "start_local": "2026-10-06 07:12"})
    assert datetime.fromtimestamp(row["start_ts"]).hour == 7


def test_merge_edit_recomputes_pace_and_stride():
    cur = {"distance_m": 10000, "duration_s": 3000, "avg_pace_s_km": 300.0,
           "avg_cadence": 180, "stride_length_m": 1.111, "feel": None, "rpe": None}
    out = ae.merge_edit(cur, {"duration_s": 2700})
    assert out["avg_pace_s_km"] == 270.0          # 用时改了 → 配速重算
    assert out["stride_length_m"] == round(1000 / 270 / (180 / 60), 3)
    # 只改主观感受不动派生量
    out2 = ae.merge_edit(cur, {"rpe": 9, "feel": 5})
    assert out2["rpe"] == 9 and out2["feel"] == 5
    assert out2["avg_pace_s_km"] == 300.0
    assert out2["stride_length_m"] == 1.111
    # 爬升可改：预估系统的坡度折算依赖它，录错了要能修
    assert ae.merge_edit(cur, {"elevation_gain_m": 210})["elevation_gain_m"] == 210.0
    # 只输出白名单字段（不会把 structure_json 之类的脏字段带进 UPDATE）
    assert set(out2) == {"name", "sport", "distance_m", "duration_s",
                         "avg_pace_s_km", "avg_hr", "max_hr", "calories",
                         "avg_cadence", "stride_length_m", "feel", "rpe",
                         "note", "manual", "elevation_gain_m"}


def test_edit_fields_normalizes_and_drops_unknown():
    out = ae.edit_fields({"rpe": 80, "feel": 50, "note": "  腿沉  ",
                          "sport": "跑步机", "id": 1, "source": "garmin"})
    assert out == {"rpe": 8, "feel": 3, "note": "腿沉", "sport": "treadmill_running"}
    assert ae.edit_fields({"note": "   "})["note"] is None
