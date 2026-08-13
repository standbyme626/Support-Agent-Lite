# LEGACY_PORT_MAP

Strategy: `PORT | ADAPT | REWRITE | IGNORE`. Always check dependencies and hidden coupling before porting.

## PORT (copy, minor changes)

- seed FAQ / SOP data
- some test fixtures
- Trace logger simple idea
- Ticket event type naming
- Retriever evaluation data

## ADAPT (reference, re-implement to new interface)

- WeCom adapter: payload parsing, message id parsing (do not copy whole implementation)
- Feishu adapter: open_id / message_id / event_id (add real webhook endpoint)
- Retrieval: document normalization, source attribution, evaluation (re-implement core)
- V2 — business semantics only: dual audience (群简短 / 私聊详情), operator
  action receipt, requester lifecycle notification, requester confirmation,
  explicit ticket id in shared operator group, outbound retry thinking
  (from `reference/workflows/case_collab_workflow.py`,
  `reference/docs/upgrade5-wecom-dispatch.md`)

## REWRITE (clean-slate)

- IdentityResolver / canonical user model (did not exist in legacy)
- Ticket state machine (single status, no composite state)
- Memory (did not exist in legacy)
- Session model (user-bound, not channel-bound)
- Approval (independent state machine, not stored on ticket)
- V2 — canonical operator identity (legacy had no operator actor), atomic
  claim, conversation purpose routing, notification outbox, HITL execution
  chain, official-doc-only protocol adapters

## IGNORE / DO NOT PORT

- `workflows/support_intake_workflow.py`
- `workflows/case_collab_workflow.py`
- `scripts/ops_api_server.py`
- `scripts/wecom_bridge_server.py`
- `app/domain/systems/` (ten-system)
- `storage/systems*`
- `runtime/`
- `legacy_adapter.py`
- `TicketLifecycleAPI`
- Legacy + v2 Ticket API dual track
- Composite status (`status` + `lifecycle_stage` + `handoff_state`)
- `tickets.metadata_json` carrying approval/memory/trace/business state
- V2 — legacy HMAC as protocol (custom `hmac-sha256(timestamp:nonce)` is
  NOT an official WeCom algorithm; protocol comes from official docs only)
- V2 — session-centric `active_ticket`, `group:<chat>:user:<sender>` as
  identity, the 19-command suite, `GROUP_CHAT_TO_SYSTEM`
