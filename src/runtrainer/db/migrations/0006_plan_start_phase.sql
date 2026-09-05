-- 计划起点时期：NULL = 完整周期；否则课表从该时期向后生成（截断前面时期）
ALTER TABLE training_plans ADD COLUMN start_phase TEXT;
