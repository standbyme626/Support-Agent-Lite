CREATE TABLE IF NOT EXISTS processed_messages (
  idempotency_key TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  processed_at TEXT NOT NULL
);
