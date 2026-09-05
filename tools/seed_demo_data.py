"""生成 8 周演示数据：跑步活动（含采样）+ 每日健康指标（睡眠/HRV/静息心率/压力）。

直接经 repo 层写入，绕过导入与同步路径——用于验证仪表盘/健康页/教练全链路。
用法：.venv\\Scripts\\python tools\\seed_demo_data.py [--weeks 8] [--clear]
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo, health_repo  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

# 各类型课的典型配速(s/km)与心率：合成数据用
KIND_PROFILE = {
    "E": {"pace": (330, 390), "hr": (128, 140), "dist": (7, 11)},
    "T": {"pace": (270, 290), "hr": (158, 172), "dist": (8, 13)},
    "I": {"pace": (235, 255), "hr": (168, 182), "dist": (6, 9)},
    "LR": {"pace": (340, 395), "hr": (126, 138), "dist": (16, 24)},
    "RECOVERY": {"pace": (380, 420), "hr": (112, 125), "dist": (4, 6)},
}

WEEK_PATTERN = [  # (weekday, kind)
    (0, "E"), (1, "T"), (2, "E"), (4, "I"), (5, "E"), (6, "LR"),
]


def _ts(d: date, hour: int, minute: int) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute).astimezone(dates.local_tz()).timestamp())


def _gen_activity(d: date, kind: str, rng: random.Random) -> tuple[dict, list[tuple]]:
    profile = KIND_PROFILE[kind]
    dist_km = rng.uniform(*profile["dist"])
    pace = rng.uniform(*profile["pace"])
    duration = dist_km * 1000 / 1000 * pace  # s
    start_hour = rng.choice([6, 6, 7, 18, 19, 20])
    start_ts = _ts(d, start_hour, rng.randint(0, 55))
    n = int(duration // 5) + 1  # 每 5 秒一个采样
    samples = []
    hr_avg = 0.0
    for i in range(n):
        t = i * 5
        hr = rng.uniform(*profile["hr"]) + 4 * (i / max(n - 1, 1))  # 缓慢爬升
        speed = 1000 / (pace + rng.uniform(-8, 8))
        samples.append((t, round(hr, 1), round(speed, 3), round(rng.uniform(168, 176), 1), None))
        hr_avg += hr
    hr_avg /= n
    activity = {
        "source": "demo", "external_id": f"demo_{d.isoformat()}_{kind}",
        "name": {"E": "轻松跑", "T": "阈值跑", "I": "间歇跑", "LR": "长距离", "RECOVERY": "恢复跑"}[kind],
        "sport": "running", "start_ts": start_ts, "tz_offset_min": 480,
        "duration_s": round(duration), "distance_m": round(dist_km * 1000),
        "avg_pace_s_km": round(pace), "avg_hr": round(hr_avg, 1),
        "max_hr": round(max(s[1] for s in samples), 1),
        "avg_cadence": 172, "elevation_gain_m": round(rng.uniform(10, 120), 1),
        "has_samples": 1,
    }
    return activity, samples


def _gen_health(d: date, rng: random.Random) -> dict:
    sleep_h = rng.uniform(6.2, 8.6)
    score = min(100, int(sleep_h / 8.5 * 80 + rng.uniform(0, 20)))
    hrv = rng.uniform(42, 72)
    hrv_status = "balanced" if hrv > 48 else ("unbalanced" if hrv > 42 else "low")
    return {
        "date": d.isoformat(), "source": "demo",
        "sleep_duration_s": round(sleep_h * 3600),
        "deep_s": round(sleep_h * 3600 * rng.uniform(0.16, 0.24)),
        "light_s": round(sleep_h * 3600 * rng.uniform(0.45, 0.55)),
        "rem_s": round(sleep_h * 3600 * rng.uniform(0.18, 0.26)),
        "awake_s": round(rng.uniform(0.1, 0.6) * 3600),
        "sleep_score": score,
        "resting_hr": round(rng.uniform(48, 58), 1),
        "hrv_avg_ms": round(hrv, 1),
        "hrv_status": hrv_status,
        "stress_avg": round(rng.uniform(18, 45), 1),
        "body_battery_min": round(rng.uniform(15, 60), 1),
        "body_battery_max": 100,
        "steps": rng.randint(6000, 18000),
    }


def seed(weeks: int = 8, clear: bool = False) -> dict:
    database.migrate()
    if clear:
        with database.get_conn() as conn:
            # 先删子表再删父表（FK 顺序）
            conn.execute("DELETE FROM activity_samples")
            conn.execute("DELETE FROM activities")
            conn.execute("DELETE FROM daily_health")
    rng = random.Random(42)
    end = dates.today()
    start = end - timedelta(days=weeks * 7)
    n_act = n_health = 0
    d = start
    while d <= end:
        if rng.random() >= 0.12:  # 约 12% 休息日
            for wd, kind in WEEK_PATTERN:
                if wd == d.weekday():
                    act, samples = _gen_activity(d, kind, rng)
                    aid, created = activity_repo.upsert_activity(act)
                    activity_repo.save_samples(aid, samples)
                    n_act += 1
        h = _gen_health(d, rng)
        health_repo.upsert_daily_health(h["date"], h)
        n_health += 1
        d += timedelta(days=1)
    return {"activities": n_act, "health_days": n_health}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    result = seed(args.weeks, args.clear)
    print(f"完成：{result['activities']} 条活动，{result['health_days']} 天健康数据")
