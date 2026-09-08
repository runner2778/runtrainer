-- 0011 详情回填尝试标记：活动详情成功拉取过（无论有无采样曲线）置 1。
-- 此前以 has_samples=0 判缺详情，真实无采样的活动每轮同步都被重复拉取
-- （永久重拉环，每条约浪费 2 个请求）。此列 =1 且 has_samples=0 表示
-- 「详情真没有采样」，不再重拉。
ALTER TABLE activities ADD COLUMN detail_attempted INTEGER NOT NULL DEFAULT 0;
