# DEVELOPMENT_PLAN

Eight phases (0-7 V1, V2 as Phase 8). Never ask for "implement the whole
project" in one step.

## Phase 0 — Architecture Bootstrap

No business functionality. Establishes:

- docs
- AGENTS.md
- repository skeleton
- dependencies
- FastAPI app
- database bootstrap
- pytest
- `GET /health`

Acceptance: `pytest` passes; `uvicorn app.main:app` starts.

## Phase 1 — Domain Foundation

Core domain only: InboundEnvelope, User, ChannelIdentity, Session, Ticket, TicketEvent, repositories, migrations.

Status limited to `OPEN | IN_PROGRESS | RESOLVED | CLOSED`. No LLM.

## Phase 2 — Identity + Ticket Core

IdentityResolver, TicketResolver, TicketStateMachine, TicketService.

Proves: channel identity != session != canonical user; explicit ticket → session ticket → user active tickets → clarification.

## Phase 3 — Real Channel Ingress

`POST /webhooks/wecom`, `POST /webhooks/feishu`. Adapter only Raw → InboundEnvelope. `message_id` idempotency.

## Phase 4 — RAG + Agent + Summary

IntentRouter (faq/support/progress_query/other), Retriever, ContextBuilder, SupportAgent (summary/category/priority/recommended action/reply draft — no direct mutation).

## Phase 5 — Human Collaboration + HITL

Operator API: claim/resolve/close/escalate. Approval API: list/approve/reject. Approval independent of ticket.

## Phase 6 — Memory

messages, ticket_summaries, memories. Short-term: rolling summary + recent 6 messages. Long-term: Closed → MemoryExtractor → stable facts → MemoryRetriever.

## Phase 7 — Trace + Eval + Demo

Unified trace_id across channel/identity/intent/retrieval/ticket/LLM/approval/memory/reply. Run 10 Golden Path tests, produce demo.

## Phase 8 (V2) — Full Collaboration Layer

Read first: `V1_TO_V2_ARCHITECTURE_AUDIT.md` (read-only audit) +
`docs/CHANNEL_PROTOCOL_MATRIX.md` (protocol evidence). Rules: mock network
not protocol; Channel != Role; shared operator group has no implicit active
ticket; Legacy is business evidence, not protocol evidence.

- Conversation (type/purpose) + Role models, migration 0009
- Ticket operational columns + TicketEvent audit (actor/trace), migration 0010
- PendingAction (HITL execution chain), migration 0011
- Notification outbox (dedupe + retry) + persisted session ticket context,
  migration 0012
- Official-shape channel protocol (Feishu token/challenge/AES; WeCom
  sha1/AES/XML; outbound contracts), honest capability flags
- Operator actions (claim atomic / resolve / force-close / escalate) via
  slash commands + REST
- Requester confirmation closure + resolution rejection + memory-after-
  confirmed-close
- Concurrency fixes: atomic idempotency claim in same transaction, atomic
  claim, identity conflict re-read, serialized connection
- AC-11 .. AC-30, offline 16-step demo, case trace endpoint

## Standard task loop

```text
Inspect → Plan → Implement → Test → Report
```

Each task: one clear goal + one file set + one acceptance. End of each phase: `git diff`, `pytest`, manual smoke test, human review.

## Git branch strategy

`main` always runnable. Development branches per phase: `feat/bootstrap`, `feat/domain-core`, `feat/identity`, `feat/ticket-resolution`, `feat/wecom`, `feat/feishu`, `feat/rag`, `feat/context`, `feat/hitl`, `feat/memory`, `feat/trace-evals`.

No long-lived single `feat/support-agent-lite` branch.
