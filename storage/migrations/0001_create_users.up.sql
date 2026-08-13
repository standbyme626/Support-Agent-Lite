CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_identities (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  channel TEXT NOT NULL,
  channel_user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (channel, channel_user_id)
);

CREATE INDEX IF NOT EXISTS idx_channel_identities_user ON channel_identities(user_id);
