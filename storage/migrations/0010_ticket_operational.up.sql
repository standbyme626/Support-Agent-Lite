-- Ticket operational context (assignment + business values + event audit).
ALTER TABLE tickets ADD COLUMN assignee_user_id TEXT;
ALTER TABLE tickets ADD COLUMN summary TEXT;
ALTER TABLE tickets ADD COLUMN category TEXT;
ALTER TABLE tickets ADD COLUMN priority TEXT;
ALTER TABLE tickets ADD COLUMN queue TEXT;
ALTER TABLE tickets ADD COLUMN source_conversation_id TEXT;

ALTER TABLE ticket_events ADD COLUMN actor_user_id TEXT;
ALTER TABLE ticket_events ADD COLUMN trace_id TEXT;
ALTER TABLE ticket_events ADD COLUMN conversation_id TEXT;

CREATE INDEX IF NOT EXISTS idx_tickets_assignee ON tickets(assignee_user_id);
