-- 生活约束备注（上课上班/可训练时间/场地条件等）：教练把课表与现实约束对齐的依据
ALTER TABLE profile ADD COLUMN schedule_note TEXT DEFAULT '';
