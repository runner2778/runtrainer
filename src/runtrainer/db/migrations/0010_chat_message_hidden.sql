-- 0010 聊天消息软隐藏：清空对话只隐藏 UI 显示（hidden=1），
-- AI 上下文仍读取全部消息（保留教练记忆）
ALTER TABLE chat_messages ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
