"""截图识别的文本解析层（不调用真机 OCR：纯文本 → 字段）。"""
from runtrainer.services import ocr_service as ocr


def _parse(text):
    return ocr.parse_activity_text(text)


def test_parses_a_typical_chinese_running_app_screen():
    r = _parse(
        "跑步  \n"
        "10.52 km\n"
        "用时 52:36\n"
        "配速 5'00\" /km\n"
        "平均心率 152 bpm\n"
        "步频 178 spm\n"
        "步幅 1.12 m\n"
        "热量 620 kcal\n"
    )
    f = r["fields"]
    assert f["distance_m"] == 10520.0
    assert f["duration_s"] == 52 * 60 + 36
    assert f["avg_hr"] == 152
    assert f["avg_cadence"] == 178
    assert f["stride_length_m"] == 1.12
    assert f["calories"] == 620
    assert f["name"] == "跑步"
    # 配速以「距离/用时」为准（52:36 / 10.52 = 300.0 s/km）
    assert f["avg_pace_s_km"] == 300.0
    assert r["warnings"] == []


def test_recovers_distance_when_km_lands_on_its_own_line():
    """真机 OCR 会把单位甩到单独一行、离数字很远（Windows OCR 实测行为）。"""
    r = _parse("10.52\nkm\n用时 52:36\n配速 5.00 /km\n")
    assert r["fields"]["distance_m"] == 10520.0


def test_recovers_pace_written_with_a_full_stop_separator():
    """OCR 常把 5'00" 读成 5.00 / 5。00（撇号丢失）。"""
    r = _parse("距离 5.00 km\n配速 5。02 /km\n")
    assert r["fields"]["avg_pace_s_km"] == 302.0     # 5:02
    r2 = _parse("距离 5.00 km\n配速 4.59 /km\n")
    assert r2["fields"]["avg_pace_s_km"] == 299.0    # 4:59


def test_distance_line_is_not_read_as_pace():
    """「10.00 km」里的 10.00 形如 10:00(=600 s/km)，落在合法域内，
    只靠范围守卫拦不住——必须靠「像距离的行不参与配速候选」挡掉。"""
    r = _parse("跑步\n10.00 km\n用时 50:00\n")
    assert r["fields"]["avg_pace_s_km"] == 300.0     # 由距离/用时算出，而非 600
    assert r["matched"].get("avg_pace_s_km") != "10.00"


def test_heart_rate_does_not_borrow_the_next_line():
    """真机实测：单位词与数字被分行时，\\s* 会跨行把 "00" 与下一行的「心率」
    连起来 → avg_hr 读成 0。同行空白的口径能挡住。"""
    r = _parse("距离 10.00 km\n用时 50:00\n心率 300 bpm\n")
    assert r["fields"].get("avg_hr") is None or 60 <= r["fields"]["avg_hr"] <= 230
    assert r["matched"].get("avg_hr") != "00\n心率"


def test_pace_uses_distance_over_duration_when_they_disagree():
    r = _parse("10.00 km\n用时 40:00\n配速 2'00\" /km\n")
    assert r["fields"]["avg_pace_s_km"] == 240.0     # 40:00/10km，忽略识别到的 2:00
    assert any("配速" in w for w in r["warnings"])


def test_duration_hms_and_minutes_only():
    assert _parse("用时 1:25:30\n距离 21.10 km")["fields"]["duration_s"] == \
        3600 + 25 * 60 + 30
    assert _parse("距离 5 km\n时长 35 分钟")["fields"]["duration_s"] == 35 * 60


def test_subjective_fields_are_read_and_normalized():
    r = _parse("距离 10.00 km\n用时 50:00\n感受 4\nRPE 7\n")
    assert r["fields"]["feel"] == 4
    assert r["fields"]["rpe"] == 7
    r2 = _parse("距离 10.00 km\n用时 50:00\n体感 75\n用力 80\n")
    assert r2["fields"]["feel"] == 4      # 0-100 量纲 → 1-5
    assert r2["fields"]["rpe"] == 8       # 0-100 量纲 → 1-10


def test_absurd_heart_rate_is_dropped_with_a_warning():
    r = _parse("距离 10.00 km\n用时 50:00\n心率 300 bpm\n")
    assert "avg_hr" not in r["fields"]
    assert any("心率" in w for w in r["warnings"])


def test_missing_core_fields_warns_and_yields_no_measurement():
    r = _parse("今天跑得很爽\n")
    assert not ({"distance_m", "duration_s", "avg_pace_s_km", "avg_hr"}
                & set(r["fields"]))
    assert any("距离或用时" in w for w in r["warnings"])


def test_normalize_text_maps_cjk_punctuation_and_fullwidth_digits():
    assert ocr.normalize_text("配速 ５．００") == "配速 5.00"
    assert ocr.normalize_text("10。52 km") == "10.52 km"
    assert ocr.normalize_text("用时：52：36") == "用时:52:36"


def test_mile_distance_is_not_mistaken_for_metres():
    """『1.20 mi』不该被当成 1.2 米。"""
    r = _parse("1.20 mi\n用时 9:30\n")
    assert r["fields"].get("distance_m") is None


def test_recognize_activity_rejects_unreadable_text(monkeypatch):
    monkeypatch.setattr(ocr, "recognize_text", lambda p: "   ")
    try:
        ocr.recognize_activity("x.png")
    except RuntimeError as e:
        assert "没读到文字" in str(e)
    else:
        raise AssertionError("空文本应当报错")


def test_recognize_activity_rejects_text_without_fields(monkeypatch):
    monkeypatch.setattr(ocr, "recognize_text", lambda p: "今天天气不错")
    try:
        ocr.recognize_activity("x.png")
    except RuntimeError as e:
        assert "认不出" in str(e)
    else:
        raise AssertionError("无字段应当报错")


def test_recognize_activity_attaches_image_path(monkeypatch):
    monkeypatch.setattr(ocr, "recognize_text", lambda p: "10.00 km\n用时 50:00")
    out = ocr.recognize_activity("C:/tmp/a.png")
    assert out["image"] == "C:/tmp/a.png"
    assert out["fields"]["distance_m"] == 10000.0


def test_recognize_text_validates_path_and_suffix(tmp_path):
    try:
        ocr.recognize_text(tmp_path / "nope.png")
    except RuntimeError as e:
        assert "不存在" in str(e)
    else:
        raise AssertionError("缺文件应当报错")
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    try:
        ocr.recognize_text(p)
    except RuntimeError as e:
        assert "不支持" in str(e)
    else:
        raise AssertionError("非图片后缀应当报错")


def test_parsed_fields_feed_straight_into_build_row():
    """识别结果必须能直接 build_row（字段名与归一规则一致）。"""
    from runtrainer.domain import activity_edit as ae
    r = _parse("跑步\n10.00 km\n用时 50:00\n平均心率 150 bpm\n步频 180 spm\nRPE 6\n")
    row = ae.build_row({**r["fields"], "name": r["fields"].get("name")})
    assert row["distance_m"] == 10000.0
    assert row["avg_pace_s_km"] == 300.0
    assert row["stride_length_m"] == 1.111
    assert row["rpe"] == 6
