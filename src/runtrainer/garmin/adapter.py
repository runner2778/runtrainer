"""Garmin 数据源适配器接口。

garminconnect 库生态不稳定（2026-03 曾被 Cloudflare 防护打挂），
所有数据接入必须经此接口隔离：自动同步失败时应用核心功能零降级。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime


class AdapterError(Exception):
    """适配器统一异常：认证失败/限流/库故障均抛此。"""


@dataclass
class RawActivity:
    """统一的原始活动结构（各适配器输出此格式）。"""
    external_id: str
    name: str
    sport: str
    start_ts: int
    tz_offset_min: int
    duration_s: float | None = None
    distance_m: float | None = None
    avg_pace_s_km: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_cadence: float | None = None
    max_cadence: float | None = None
    stride_length_m: float | None = None
    aerobic_te: float | None = None
    anaerobic_te: float | None = None
    exercise_load: float | None = None
    elevation_gain_m: float | None = None
    elevation_loss_m: float | None = None
    calories: int | None = None
    laps: list[dict] = field(default_factory=list)  # 规范形 [{distance_m, elapsed_s, avg_hr, pace_s_km}]
    samples: list[dict] = field(default_factory=list)  # [{t_offset_s, hr, speed_mps, cadence, altitude_m}]


@dataclass
class RawDailyHealth:
    """统一的每日健康结构（睡眠/HRV/心率/压力等，缺项 None）。"""
    date: date
    sleep_start_ts: int | None = None
    sleep_end_ts: int | None = None
    sleep_duration_s: float | None = None
    deep_s: float | None = None
    light_s: float | None = None
    rem_s: float | None = None
    awake_s: float | None = None
    sleep_score: float | None = None
    resting_hr: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    hrv_avg_ms: float | None = None
    hrv_status: str | None = None
    stress_avg: float | None = None
    body_battery_min: float | None = None
    body_battery_max: float | None = None
    steps: int | None = None
    raw: dict | None = None


class GarminAdapter(ABC):
    """Garmin 数据源抽象。实现：garminconnect_adapter / mock_adapter。"""

    name = "garmin"

    @abstractmethod
    def login(self, username: str | None = None, password: str | None = None) -> None:
        """登录，失败抛 AdapterError。凭据通常已在构造适配器时传入。"""

    @abstractmethod
    def fetch_profile(self) -> dict:
        """返回 {nickname, sex, birth_year, max_hr, rest_hr, ...}，缺项 None。"""

    @abstractmethod
    def fetch_activities(self, since: datetime, limit: int = 100) -> list[RawActivity]:
        """拉取 since 以来的全部活动概要（不含 samples）。

        limit 为单页大小；实现必须自行翻页拉全量，不得只返回第一页。
        """

    @abstractmethod
    def fetch_activity_detail(self, external_id: str) -> RawActivity:
        """拉取单次活动详情（含 samples 与单圈）。"""

    @abstractmethod
    def fetch_daily_health(self, start: date, end: date) -> list[RawDailyHealth]:
        """按日期范围拉取每日健康数据。"""
