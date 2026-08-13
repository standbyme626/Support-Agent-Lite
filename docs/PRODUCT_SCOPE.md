# PRODUCT_SCOPE

## What this is

`support-agent-lite` is a cross-channel enterprise support agent that resolves support requests from enterprise IM channels (WeCom / Feishu) through a user-centric, workflow-first architecture.

## Golden Path (what must work end-to-end)

```text
Channel (WeCom / Feishu)
  → InboundEnvelope
  → Canonical Identity
  → Conversation (type + purpose)
  → Intent (FAQ | Support | ProgressQuery)
  → RAG answer OR Ticket Resolver
  → Ticket lifecycle
  → Agent context/summary
  → Operator collaboration (claim / resolve, atomic)
  → Approval (for high-risk actions) → HITL execution
  → Requester confirmation (cross-channel allowed)
  → Close → Memory extraction
  → Recall in next session
  → Notifications (public / private / internal) via transactional outbox
```

## Out of scope

- Multi-Agent
- MCP
- GraphRAG
- Kafka
- Kubernetes
- ERPNext / ten-system architecture
- Telegram
- Complex RBAC
- Complex frontend
- Duplicate merge
- Complex SLA
- Legacy compatibility APIs
- Multiple Ticket APIs (exactly one)
- LangGraph suspend/resume (HITL is a deterministic execution chain)

## In scope since V2

- Conversation as a first-class model (`ConversationType`, `ConversationPurpose`)
- Canonical Operator identity + persisted assignment
- Requester public / private / internal three-tier visibility
- Transactional notification outbox + audience policy + target resolver
- Channel protocol contracts strictly from official docs
  (`docs/CHANNEL_PROTOCOL_MATRIX.md`), honest capability flags
- Requester confirmation closure, resolution rejection, force close
- HITL execution chain (action request → approval → execute once)

## Design principles

1. **User-centric**: all continuation and memory resolves through a canonical user identity, never through a channel session id.
2. **Workflow-first**: business logic lives in explicit workflows, not in an agent's free-form choices.
3. **Small coherent phases**: each phase is independently testable against the acceptance contract.
4. **Grounding**: low-confidence RAG must not become free-form model answers.
