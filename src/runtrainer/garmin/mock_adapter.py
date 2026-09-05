"""Mock 适配器：合成 Garmin 数据（确定性随机），用于开发/测试/演示。

走与真实适配器完全相同的接口，可验证同步全链路而无需账号。
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from ..utils import dates
from .adapter import GarminAdapter, RawActivity, RawDailyHealth

KIND_PACE_HR = {
    "E": (340, (128, 140), (7, 11)), "T": (280, (158, 172), (8, 13)),
    "I": (245, (168, 182), (6, 9)), "LR": (365, (126, 138), (16, 24)),
    "RECOVERY": (400, (112, 125), (4, 6)),
}
WEEK_PATTERN = [(0, "E"), (1, "T"), (2, "E"), (4, "I"), (5, "E"), (6, "LR")]


class MockAdapter(GarminAdapter):
    name = "mock"

    def __init__(self, seed: int = 42):
        self._seed = seed

    def _day_rng(self, d: date) -> random.Random:
        """按日期固定随机种子：同一天的数据不随拉取范围变化，保证增量同步可去重。"""
        return random.Random(f"{self._seed}:{d.isoformat()}")

    def login(self, username: str | None = None, password: str | None = None) -> None:
        return None  # mock 永远成功

    def fetch_profile(self) -> dict:
        return {"nickname": "演示用户", "sex": "male", "birth_year": 1992,
                "max_hr": 186, "rest_hr": 52}

    def fetch_activities(self, since: datetime, limit: int = 100) -> list[RawActivity]:
        # mock 世界是静态合成的：返回 since 到今天的全部活动（limit 仅单页大小）。
        # 不得按 limit 截断——否则首次全量回溯会停在最旧端，近期日子缺失，
        # 第二次增量同步反而"凭空"出现新活动（合成数据无法真实翻页，全量即翻页）。
        result = []
        d = since.date()
        while d <= dates.today():
            rng = self._day_rng(d)
            if rng.random() >= 0.12:
                for wd, kind in WEEK_PATTERN:
                    if wd == d.weekday():
                        result.append(self._mk_activity(d, kind, rng))
            d += timedelta(days=1)
        return result

    def fetch_activity_detail(self, external_id: str) -> RawActivity:
        d = date.fromisoformat(external_id.split("_")[-1])
        kind = external_id.split("_")[-2]
        return self._mk_activity(d, kind, self._day_rng(d), with_samples=True)

    def fetch_daily_health(self, start: date, end: date) -> list[RawDailyHealth]:
        result = []
        d = start
        while d <= end:
            result.append(self._mk_health(d, self._day_rng(d)))
            d += timedelta(days=1)
        return result

    # ---- 内部合成 ----
    def _mk_activity(self, d: date, kind: str, rng: random.Random,
                     with_samples: bool = False) -> RawActivity:
        pace, (hr_lo, hr_hi), (dist_lo, dist_hi) = KIND_PACE_HR[kind]
        dist = rng.uniform(dist_lo, dist_hi)
        p = rng.uniform(pace - 15, pace + 15)
        duration = dist * 1000 / 1000 * p
        hour = rng.choice([6, 6, 7, 18, 19, 20])
        start_ts = int(datetime(d.year, d.month, d.day, hour, rng.randint(0, 55))
                       .astimezone(dates.local_tz()).timestamp())
        samples = []
        if with_samples:
            n = int(duration // 5) + 1
            for i in range(n):
                hr = rng.uniform(hr_lo, hr_hi)
                samples.append({"t_offset_s": i * 5, "hr": round(hr, 1),
                                "speed_mps": round(1000 / (p + rng.uniform(-8, 8)), 3),
                                "cadence": round(rng.uniform(168, 176), 1), "altitude_m": None})
        return RawActivity(
            external_id=f"mock_{kind}_{d.isoformat()}", name=f"mock {kind}",
            sport="running", start_ts=start_ts, tz_offset_min=480,
            duration_s=round(duration), distance_m=round(dist * 1000),
            avg_pace_s_km=round(p), avg_hr=round(rng.uniform(hr_lo, hr_hi), 1),
            max_hr=round(rng.uniform(hr_hi, hr_hi + 8), 1),
            avg_cadence=172, elevation_gain_m=round(rng.uniform(10, 120), 1),
            calories=round(duration / 60 * 10), laps=[],
            samples=samples,
        )

    def _mk_health(self, d: date, rng: random.Random) -> RawDailyHealth:
        sleep_h = rng.uniform(6.2, 8.6)
        hrv = rng.uniform(42, 72)
        hrv_status = "balanced" if hrv > 48 else ("unbalanced" if hrv > 42 else "low")
        start_ts = int(datetime(d.year, d.month, d.day, 23).astimezone(dates.local_tz()).timestamp())
        end_ts = int(datetime(d.year, d.month, d.day, 6, 30).astimezone(dates.local_tz()).timestamp())
        return RawDailyHealth(
            date=d,
            sleep_start_ts=start_ts, sleep_end_ts=end_ts,
            sleep_duration_s=round(sleep_h * 3600),
            deep_s=round(sleep_h * 3600 * rng.uniform(0.16, 0.24)),
            light_s=round(sleep_h * 3600 * rng.uniform(0.45, 0.55)),
            rem_s=round(sleep_h * 3600 * rng.uniform(0.18, 0.26)),
            awake_s=round(rng.uniform(0.1, 0.6) * 3600),
            sleep_score=min(100, int(sleep_h / 8.5 * 80 + rng.uniform(0, 20))),
            resting_hr=round(rng.uniform(48, 58), 1),
            hrv_avg_ms=round(hrv, 1),
            hrv_status=hrv_status,
            stress_avg=round(rng.uniform(18, 45), 1),
            body_battery_min=round(rng.uniform(15, 60), 1),
            body_battery_max=100,
            steps=rng.randint(6000, 18000),
        )
