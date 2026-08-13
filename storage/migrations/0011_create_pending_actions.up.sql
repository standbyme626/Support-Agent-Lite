CREATE TABLE IF NOT EXISTS pending_actions (
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL REFERENCES tickets(id),
  action_type TEXT NOT NULL,
  payload TEXT,
  requested_by TEXT NOT NULL,
  approval_id TEXT,
  execution_status TEXT NOT NULL CHECK (execution_status IN ('PENDING', 'EXECUTED', 'SKIPPED', 'FAILED')),
  executed_at TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_actions_ticket ON pending_actions(ticket_id);
CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(execution_status);
