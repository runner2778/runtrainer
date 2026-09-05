-- 0004 活动详情字段：训练负荷、步幅、训练内容结构
ALTER TABLE activities ADD COLUMN stride_length_m REAL;
ALTER TABLE activities ADD COLUMN aerobic_te REAL;
ALTER TABLE activities ADD COLUMN anaerobic_te REAL;
ALTER TABLE activities ADD COLUMN exercise_load REAL;
ALTER TABLE activities ADD COLUMN structure_json TEXT;
