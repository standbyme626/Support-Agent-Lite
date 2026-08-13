# DEVELOPMENT_PLAN

Seven phases. Never ask for "implement the whole project" in one step.

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

## Standard task loop

```text
Inspect → Plan → Implement → Test → Report
```

Each task: one clear goal + one file set + one acceptance. End of each phase: `git diff`, `pytest`, manual smoke test, human review.

## Git branch strategy

`main` always runnable. Development branches per phase: `feat/bootstrap`, `feat/domain-core`, `feat/identity`, `feat/ticket-resolution`, `feat/wecom`, `feat/feishu`, `feat/rag`, `feat/context`, `feat/hitl`, `feat/memory`, `feat/trace-evals`.

No long-lived single `feat/support-agent-lite` branch.
