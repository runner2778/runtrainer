-- 0005 AI 教练聊天记录（教练消息可携带待批调整 id 与已应用档案更新）
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,                 -- user / coach
  content TEXT NOT NULL,
  adjustment_ids_json TEXT,           -- coach 提出的调整（adjustments 表 id 列表）
  profile_updates_json TEXT,          -- coach 应用的档案更新（键值）
  model TEXT,
  created_at TEXT NOT NULL
);
