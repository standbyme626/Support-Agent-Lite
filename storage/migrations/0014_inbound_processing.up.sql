-- Inbound message durable processing lifecycle (V2.1).
--
-- The idempotency claim (processed_messages) only proves "claimed once".
-- This table adds the PROCESSING state so a crash between "domain committed"
-- and "agent decision applied" can be resumed by a later duplicate delivery
-- without re-running deterministic business effects:
--
--   RECEIVED          claim + identity/session/conversation resolved
--   AGENT_PENDING     deterministic domain effects committed, agent run pending
--   AGENT_COMPLETED   transient: agent decision applied, finishing phase B
--   COMPLETED         fully processed (duplicate deliveries are no-ops)
--   FAILED_RETRYABLE  phase B failed after phase A committed; retry resumes
--
-- State transitions are CAS-guarded (UPDATE ... WHERE state = ?) so
-- concurrent duplicate deliveries execute the agent/phase-B exactly once.
CREATE TABLE IF NOT EXISTS inbound_processing (
  idempotency_key TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'RECEIVED', 'AGENT_PENDING', 'AGENT_COMPLETED', 'COMPLETED', 'FAILED_RETRYABLE'
  )),
  kind TEXT,
  ticket_id TEXT,
  user_id TEXT,
  session_id TEXT,
  conversation_channel TEXT,
  conversation_id TEXT,
  intent TEXT,
  reply TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inbound_processing_state ON inbound_processing(state);
