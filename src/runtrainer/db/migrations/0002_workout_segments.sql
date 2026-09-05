-- M3：课表结构化详情段（warmup/main/cooldown），供日历弹窗与 AI 提示词使用
ALTER TABLE planned_workouts ADD COLUMN segments_json TEXT;
