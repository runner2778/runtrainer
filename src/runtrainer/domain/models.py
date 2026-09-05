"""领域数据类：纯结构，不碰 DB/网络。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Profile:
    nickname: str | None = None
    sex: str | None = None
    birth_year: int | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    max_hr: int | None = None
    rest_hr: int | None = None
    hr_source: str = "manual"
    run_experience: str = "intermediate"  # beginner / intermediate / advanced

    @property
    def age(self) -> int | None:
        if not self.birth_year:
            return None
        return date.today().year - self.birth_year


@dataclass
class Goal:
    distance_m: int
    race_date: date
    target_seconds: int | None = None  # None = 完赛
    vdot: float | None = None
    vdot_source: str | None = None  # result / target / manual
    status: str = "active"
    id: int | None = None


@dataclass
class TrainingPlan:
    goal_id: int
    start_date: date
    race_date: date
    total_weeks: int
    phase_weeks: dict[str, int]
    vdot: float
    base_weekly_km: float
    peak_weekly_km: float
    run_days: int
    long_run_weekday: int
    engine_version: str
    status: str = "active"
    id: int | None = None


@dataclass
class PlannedWorkout:
    date: date
    week_index: int
    phase: str
    kind: str  # E/M/T/I/R/LR/RECOVERY/TUNEUP/RACE
    title: str
    description: str | None = None
    distance_km: float | None = None
    duration_min: float | None = None
    pace_zone: str | None = None
    pace_slow_s_km: float | None = None
    pace_fast_s_km: float | None = None
    target_hr_zone: str | None = None
    source: str = "engine"
    adjustment_id: int | None = None
    status: str = "planned"
    completed_activity_id: int | None = None
    plan_id: int | None = None
    id: int | None = None

    @property
    def is_hard(self) -> bool:
        return self.kind in ("T", "I", "R", "M")


@dataclass
class Activity:
    source: str
    external_id: str
    start_ts: int
    name: str | None = None
    sport: str = "running"
    duration_s: float | None = None
    distance_m: float | None = None
    avg_pace_s_km: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_cadence: float | None = None
    elevation_gain_m: float | None = None
    calories: int | None = None
    laps: list[dict] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)
    id: int | None = None


@dataclass
class DailyHealth:
    date: date
    sleep_duration_s: float | None = None
    sleep_score: float | None = None
    deep_s: float | None = None
    light_s: float | None = None
    rem_s: float | None = None
    awake_s: float | None = None
    resting_hr: float | None = None
    hrv_avg_ms: float | None = None
    hrv_status: str | None = None
    stress_avg: float | None = None
    body_battery_min: float | None = None
    steps: int | None = None


# 训练类型固定色映射（日历/图表统一使用，勿随筛选重排）
KIND_COLORS = {
    "E": "#2a78d6",
    "M": "#1baf7a",
    "T": "#eda100",
    "I": "#e34948",
    "R": "#e87ba4",
    "LR": "#2a78d6",
    "RECOVERY": "#898781",
    "TUNEUP": "#9b6dd6",
    "RACE": "#0ca30c",
    "STRENGTH": "#c2703d",
}

KIND_LABELS = {
    "E": "轻松跑",
    "M": "马拉松配速",
    "T": "阈值跑",
    "I": "间歇跑",
    "R": "重复跑",
    "LR": "长距离",
    "RECOVERY": "恢复",
    "TUNEUP": "测试赛",
    "RACE": "比赛",
    "STRENGTH": "力量训练",
}

PHASE_LABELS = {
    "base": "基础期",
    "early": "早期强度",
    "transition": "过渡期",
    "final": "最终强度",
    "taper": "减量期",
}
