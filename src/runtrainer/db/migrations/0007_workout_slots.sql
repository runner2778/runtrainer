-- 一天两练：planned_workouts 增加 slot（1=当日第一练，2=当日第二练）。
-- 唯一约束从 (plan_id, date) 改为 (plan_id, date, slot)。
-- SQLite 不支持修改表约束 → 按官方 12 步流程重建表（保留 id 与全部数据）。
-- 注意：executescript 逐条自动提交，重建过程非原子；单用户桌面应用可接受。
PRAGMA foreign_keys=OFF;

CREATE TABLE planned_workouts_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES training_plans(id),
    date TEXT NOT NULL,
    slot INTEGER NOT NULL DEFAULT 1,
    week_index INTEGER NOT NULL,
    phase TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    distance_km REAL,
    duration_min REAL,
    pace_zone TEXT,
    pace_slow_s_km REAL,
    pace_fast_s_km REAL,
    target_hr_zone TEXT,
    source TEXT NOT NULL DEFAULT 'engine',
    adjustment_id INTEGER,
    status TEXT NOT NULL DEFAULT 'planned',
    completed_activity_id INTEGER,
    segments_json TEXT,
    UNIQUE (plan_id, date, slot)
);

INSERT INTO planned_workouts_new (id, plan_id, date, slot, week_index, phase, kind, title,
    description, distance_km, duration_min, pace_zone, pace_slow_s_km, pace_fast_s_km,
    target_hr_zone, source, adjustment_id, status, completed_activity_id, segments_json)
SELECT id, plan_id, date, 1, week_index, phase, kind, title,
    description, distance_km, duration_min, pace_zone, pace_slow_s_km, pace_fast_s_km,
    target_hr_zone, source, adjustment_id, status, completed_activity_id, segments_json
FROM planned_workouts;

DROP TABLE planned_workouts;
ALTER TABLE planned_workouts_new RENAME TO planned_workouts;
CREATE INDEX IF NOT EXISTS idx_pw_plan_date ON planned_workouts(plan_id, date);

PRAGMA foreign_keys=ON;

-- 计划级选项：一天两练与力量课（重建计划时沿用）
ALTER TABLE training_plans ADD COLUMN double_days INTEGER NOT NULL DEFAULT 0;
ALTER TABLE training_plans ADD COLUMN double_mode TEXT NOT NULL DEFAULT 'auto';
ALTER TABLE training_plans ADD COLUMN strength_days INTEGER NOT NULL DEFAULT 0;
