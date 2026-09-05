"""M1：FIT/CSV 解析与导入服务测试（fixtures 为真实 Garmin Fenix 5 跑步 FIT）。"""
import pytest

from runtrainer.db.repos import activity_repo
from runtrainer.garmin import import_service
from runtrainer.garmin.csv_importer import parse_csv
from runtrainer.garmin.fit_importer import parse_fit

FIXTURES = "tests/fixtures"


class TestParseFit:
    def test_parse_real_garmin_run(self):
        a = parse_fit(f"{FIXTURES}/sample_run.fit")
        assert a.sport == "running"
        assert a.start_ts == 1497191649  # 2017-06-11 14:34:09 UTC
        assert a.tz_offset_min == 480  # 本机时区 CST
        assert a.duration_s == pytest.approx(56.9, abs=1)
        assert a.distance_m == pytest.approx(157.6, abs=1)
        assert a.avg_pace_s_km == pytest.approx(361, abs=3)
        assert a.avg_hr == pytest.approx(90, abs=1)
        assert a.max_hr == pytest.approx(112, abs=1)
        assert a.elevation_gain_m == pytest.approx(2.0, abs=0.5)
        assert len(a.laps) == 1
        assert len(a.samples) == 21
        # 采样含 HR 且时间偏移从 0 递增
        assert all(s["hr"] is not None for s in a.samples)
        assert a.samples[0]["t_offset_s"] == 0
        assert a.samples[-1]["t_offset_s"] > 0

    def test_invalid_file_raises(self, tmp_path):
        bad = tmp_path / "bad.fit"
        bad.write_bytes(b"not a fit file")
        with pytest.raises(Exception):
            parse_fit(bad)


class TestParseCsv:
    def test_parse(self):
        rows = parse_csv(f"{FIXTURES}/sample_activities.csv")
        assert len(rows) == 2
        r0 = rows[0]
        assert r0.name == "晨跑"
        assert r0.distance_m == 10000
        assert r0.duration_s == 3000
        assert r0.avg_pace_s_km == pytest.approx(300, abs=0.1)
        assert r0.avg_hr == 145
        # 2026-09-01 06:30 本地时间 → epoch 秒
        assert r0.start_ts > 0

    def test_missing_date_skipped(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("date,start_time,duration_s,distance_m\n,06:00:00,100,1000\n2026-09-01,06:00:00,100,1000\n",
                     encoding="utf-8")
        rows = parse_csv(p)
        assert len(rows) == 1


class TestImportService:
    def test_import_fit_dedupes_and_archives(self):
        r1 = import_service.import_fit_file(f"{FIXTURES}/sample_run.fit")
        assert r1["created"] is True
        aid = r1["activity_id"]
        # 重复导入：不新建
        r2 = import_service.import_fit_file(f"{FIXTURES}/sample_run.fit")
        assert r2["created"] is False
        assert r2["activity_id"] == aid
        assert activity_repo.count_activities() == 1
        a = activity_repo.get_activity(aid)
        assert a["file_path"] and a["has_samples"] == 1
        assert len(activity_repo.get_samples(aid)) == 21

    def test_import_csv(self):
        rs = import_service.import_csv_file(f"{FIXTURES}/sample_activities.csv")
        assert len(rs) == 2
        assert all(r["created"] for r in rs)
        # 再次导入全部跳过
        rs2 = import_service.import_csv_file(f"{FIXTURES}/sample_activities.csv")
        assert all(not r["created"] for r in rs2)

    def test_import_files_routing(self, tmp_path):
        ok = import_service.import_files([
            f"{FIXTURES}/sample_run.fit",
            f"{FIXTURES}/sample_activities.csv",
        ])
        assert ok["imported"] == 3  # 1 FIT + 2 CSV
        assert ok["errors"] == []
        # 不支持的类型
        bad = tmp_path / "x.txt"
        bad.write_text("hi")
        r = import_service.import_files([str(bad)])
        assert r["imported"] == 0 and len(r["errors"]) == 1
