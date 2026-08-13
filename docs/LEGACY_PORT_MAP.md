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

## REWRITE (clean-slate)

- IdentityResolver / canonical user model (did not exist in legacy)
- Ticket state machine (single status, no composite state)
- Memory (did not exist in legacy)
- Session model (user-bound, not channel-bound)
- Approval (independent state machine, not stored on ticket)

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
