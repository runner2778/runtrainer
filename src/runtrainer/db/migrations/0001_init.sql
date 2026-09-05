-- 0001 初始 schema
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    nickname TEXT,
    sex TEXT,
    birth_year INTEGER,
    height_cm REAL,
    weight_kg REAL,
    max_hr INTEGER,
    rest_hr INTEGER,
    hr_source TEXT NOT NULL DEFAULT 'manual',
    run_experience TEXT NOT NULL DEFAULT 'intermediate',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    distance_m INTEGER NOT NULL,
    target_seconds INTEGER,
    race_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    vdot REAL,
    vdot_source TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    start_date TEXT NOT NULL,
    race_date TEXT NOT NULL,
    total_weeks INTEGER NOT NULL,
    phase_weeks TEXT NOT NULL,
    vdot REAL NOT NULL,
    base_weekly_km REAL NOT NULL,
    peak_weekly_km REAL NOT NULL,
    run_days INTEGER NOT NULL,
    long_run_weekday INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planned_workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES training_plans(id),
    date TEXT NOT NULL,
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
    UNIQUE (plan_id, date)
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    file_path TEXT,
    name TEXT,
    sport TEXT,
    start_ts INTEGER NOT NULL,
    tz_offset_min INTEGER NOT NULL DEFAULT 0,
    duration_s REAL,
    distance_m REAL,
    avg_pace_s_km REAL,
    avg_hr REAL,
    max_hr REAL,
    avg_cadence REAL,
    max_cadence REAL,
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    calories INTEGER,
    laps_json TEXT,
    has_samples INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS activity_samples (
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    t_offset_s REAL,
    hr REAL,
    speed_mps REAL,
    cadence REAL,
    altitude_m REAL,
    PRIMARY KEY (activity_id, seq)
);

CREATE TABLE IF NOT EXISTS daily_health (
    date TEXT PRIMARY KEY,
    source TEXT,
    sleep_start_ts INTEGER,
    sleep_end_ts INTEGER,
    sleep_duration_s REAL,
    deep_s REAL,
    light_s REAL,
    rem_s REAL,
    awake_s REAL,
    sleep_score REAL,
    resting_hr REAL,
    avg_hr REAL,
    max_hr REAL,
    hrv_avg_ms REAL,
    hrv_status TEXT,
    stress_avg REAL,
    body_battery_min REAL,
    body_battery_max REAL,
    steps INTEGER,
    raw_json TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER REFERENCES training_plans(id),
    workout_id INTEGER REFERENCES planned_workouts(id),
    applies_date TEXT,
    action TEXT NOT NULL,
    changes_json TEXT,
    reason TEXT,
    ai_model TEXT,
    ai_input_json TEXT,
    ai_output_json TEXT,
    guardrail_log_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT PRIMARY KEY,
    last_sync_ts INTEGER,
    last_error TEXT,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_ts);
CREATE INDEX IF NOT EXISTS idx_pw_plan_date ON planned_workouts(plan_id, date);
CREATE INDEX IF NOT EXISTS idx_samples_act_seq ON activity_samples(activity_id, seq);
CREATE INDEX IF NOT EXISTS idx_health_date ON daily_health(date);
