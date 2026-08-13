# PRODUCT_SCOPE

## What this is

`support-agent-lite` is a cross-channel enterprise support agent that resolves support requests from enterprise IM channels (WeCom / Feishu) through a user-centric, workflow-first architecture.

## Golden Path (what must work end-to-end)

```text
Channel (WeCom / Feishu)
  → InboundEnvelope
  → Canonical Identity
  → Session
  → Intent (FAQ | Support | ProgressQuery)
  → RAG answer OR Ticket Resolver
  → Ticket lifecycle
  → Agent context/summary
  → Operator collaboration
  → Approval (for high-risk actions)
  → Resolve / Close
  → Memory extraction
  → Recall in next session
```

## Out of scope (v1)

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

## Design principles

1. **User-centric**: all continuation and memory resolves through a canonical user identity, never through a channel session id.
2. **Workflow-first**: business logic lives in explicit workflows, not in an agent's free-form choices.
3. **Small coherent phases**: each phase is independently testable against the acceptance contract.
4. **Grounding**: low-confidence RAG must not become free-form model answers.
