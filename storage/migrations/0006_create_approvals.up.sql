CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL REFERENCES tickets(id),
  action TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
  requested_by TEXT NOT NULL,
  reason TEXT,
  decided_by TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approvals_ticket ON approvals(ticket_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
