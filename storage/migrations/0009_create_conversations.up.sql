CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  channel_conversation_id TEXT NOT NULL,
  conversation_type TEXT NOT NULL CHECK (conversation_type IN ('DM', 'GROUP')),
  purpose TEXT NOT NULL CHECK (purpose IN ('REQUESTER', 'OPERATOR', 'APPROVAL')),
  queue TEXT,
  location TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE (channel, channel_conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_conversations_purpose ON conversations(purpose);
CREATE INDEX IF NOT EXISTS idx_conversations_queue ON conversations(queue);

CREATE TABLE IF NOT EXISTS user_roles (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ('requester', 'operator', 'approver')),
  queue TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (user_id, role)
);

CREATE INDEX IF NOT EXISTS idx_user_roles_user ON user_roles(user_id);
