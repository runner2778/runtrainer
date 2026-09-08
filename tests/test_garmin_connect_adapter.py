"""中国区 Garmin API 结构解析（字段名与国际版不同：activityType / userData）。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from runtrainer.garmin.adapter import AdapterError
from runtrainer.garmin.garminconnect_adapter import GarminConnectAdapter

# 真实中国区 API 形状（tools/probe_garmin.py 实测）
CN_PROFILE = {
    "id": 12482964,
    "userData": {
        "gender": "MALE", "birthDate": "2007-09-18",
        "height": 174.0, "weight": 61000.0,
        "vo2MaxRunning": 63.0, "lactateThresholdHeartRate": 182,
        "maxHeartRate": None, "restingHeartRate": None,
    },
    "userSleep": None, "connectDate": None, "sourceType": None,
}
CN_ACTIVITY = {
    "activityId": 12345,
    "activityName": "六盘水轻松跑",
    # 中国区字段名是 activityType；国际版的 "type" 为 null
    "activityType": {"typeId": 1, "typeKey": "running"},
    "startTimeLocal": "2026-09-01 06:30:00",
    "duration": 1800.0, "distance": 5000.0,
    "averageHR": 128.0, "maxHR": 144.0, "calories": 220,
    "ownerFullName": "沉稳果断坚韧",
}


class StubClient:
    def get_user_profile(self):
        return CN_PROFILE

    def get_activities(self, start, limit):
        return [CN_ACTIVITY]


def _adapter() -> GarminConnectAdapter:
    a = GarminConnectAdapter.__new__(GarminConnectAdapter)
    a._client = StubClient()
    return a


def test_fetch_profile_cn_shape():
    p = _adapter().fetch_profile()
    assert p["nickname"] == "沉稳果断坚韧"      # 档案无昵称 → 从活动 ownerFullName 取
    assert p["sex"] == "male"
    assert p["birth_year"] == 2007
    assert p["height_cm"] == 174.0               # height 已是 cm
    assert p["weight_kg"] == 61.0                # weight 是克 → kg
    assert p["vo2max"] == 63.0


def test_fetch_activities_uses_activity_type_field():
    # 活动 startTimeLocal 为本地时间（中国 06:30 = UTC 前一日 22:30），since 取更早
    since = datetime(2026, 8, 30, tzinfo=timezone.utc)
    acts = _adapter().fetch_activities(since)
    assert len(acts) == 1
    a = acts[0]
    assert a.external_id == "12345"
    assert a.distance_m == 5000.0
    assert a.avg_pace_s_km == 360.0
    assert a.avg_hr == 128.0
    assert a.calories == 220


class FlakyClient:
    """健康端点桩：bad_days 里的日期 4 端点全抛（模拟限流/服务故障）。"""

    def __init__(self, bad_days: set[str]):
        self._bad = bad_days

    def get_sleep_data(self, iso):
        if iso in self._bad:
            raise Exception("429 too many requests")
        return {"dailySleepDTO": {"sleepTimeSeconds": 28800.0}}

    def get_hrv_data(self, iso):
        if iso in self._bad:
            raise Exception("429 too many requests")
        return {}

    def get_stress_data(self, iso):
        if iso in self._bad:
            raise Exception("429 too many requests")
        return {}

    def get_user_summary(self, iso):
        if iso in self._bad:
            raise Exception("429 too many requests")
        return {}


def test_fetch_daily_health_skips_failed_day():
    """健康逐日拉取（批19）：单日 4 端点全挂只跳过该日，不拖垮整批 90 天。"""
    a = GarminConnectAdapter.__new__(GarminConnectAdapter)
    a._client = FlakyClient({"2026-09-02"})
    days = a.fetch_daily_health(date(2026, 9, 1), date(2026, 9, 3))
    assert [d.date.isoformat() for d in days] == ["2026-09-01", "2026-09-03"]
    assert days[0].sleep_duration_s == 28800.0


def test_fetch_daily_health_all_failed_raises():
    """整批全挂仍抛（外部干预信号），不被静默吞掉。"""
    a = GarminConnectAdapter.__new__(GarminConnectAdapter)
    a._client = FlakyClient({"2026-09-01", "2026-09-02"})
    with pytest.raises(AdapterError):
        a.fetch_daily_health(date(2026, 9, 1), date(2026, 9, 2))
