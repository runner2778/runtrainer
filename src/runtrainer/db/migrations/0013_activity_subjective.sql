-- 0013 主观感受与手动录入：佳明「主观感受 / 用力程度」+ 手动补录活动
--
-- feel        主观感受，1（很轻松）…5（非常吃力）——佳明 Connect 的 5 档
--             （API 里 directWorkoutFeel 为 0/25/50/75/100，入库前归一成 1-5）。
-- rpe         用力程度 RPE，1…10——佳明 Connect 的 10 档
--             （API 里 directWorkoutRpe 为 0…100，入库前 //10 归一）。
-- note        自由备注：只有手动录入会写。
-- manual      1 = 手动补录/截图识别入库（Garmin 同步不会覆盖它）。
--             手动活动的 external_id 形如 manual-<uuid>，与手表活动并存。
ALTER TABLE activities ADD COLUMN feel INTEGER;
ALTER TABLE activities ADD COLUMN rpe INTEGER;
ALTER TABLE activities ADD COLUMN note TEXT;
ALTER TABLE activities ADD COLUMN manual INTEGER NOT NULL DEFAULT 0;
