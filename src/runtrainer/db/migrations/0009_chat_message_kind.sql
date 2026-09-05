-- 0009 聊天消息类型：区分普通对话与同步后自动分析（前端展示标记不同）
ALTER TABLE chat_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat';
