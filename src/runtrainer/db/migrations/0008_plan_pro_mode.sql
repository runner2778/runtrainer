-- 职业双练模式（效仿职业运动员）：休息日轻松跑单练，其余所有训练日两练；
-- 减量/比赛周恢复常规。计划级开关，重建计划时沿用。
ALTER TABLE training_plans ADD COLUMN pro_mode INTEGER NOT NULL DEFAULT 0;
