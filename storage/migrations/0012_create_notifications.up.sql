CREATE TABLE IF NOT EXISTS notification_outbox (
  id TEXT PRIMARY KEY,
  source_event_id TEXT NOT NULL,
  notification_type TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ('PUBLIC', 'PRIVATE', 'INTERNAL')),
  target_type TEXT NOT NULL,
  target_key TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  result_code TEXT,
  ticket_id TEXT,
  trace_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (source_event_id, notification_type, target_key)
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON notification_outbox(status);
CREATE INDEX IF NOT EXISTS idx_outbox_ticket ON notification_outbox(ticket_id);

CREATE TABLE IF NOT EXISTS delivery_attempts (
  id TEXT PRIMARY KEY,
  outbox_id TEXT NOT NULL REFERENCES notification_outbox(id),
  attempt_number INTEGER NOT NULL,
  success INTEGER NOT NULL,
  result_code TEXT,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delivery_attempts_outbox ON delivery_attempts(outbox_id);

CREATE TABLE IF NOT EXISTS session_ticket_contexts (
  session_id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
