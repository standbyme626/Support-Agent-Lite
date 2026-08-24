# AGENTS.md

## Project

`support-agent-lite` is a cross-channel enterprise support agent.

The architecture is **user-centric and workflow-first**.

Legacy project is available at `reference/` (READ-ONLY). See `docs/reference/LEGACY_ARCHITECTURE_AUDIT.md`.

## Core invariants

1. Channel identity != canonical user.
2. Session != user.
3. Session != memory.
4. Agent must not directly mutate sensitive Ticket state.
5. Ticket current state and TicketEvent must be committed transactionally.
6. Approval is an independent state machine.
7. Low-confidence RAG must not become free-form model answers.
8. Cross-channel continuation must resolve through canonical user identity.

## Architecture

Read before coding:

- `docs/PRODUCT_SCOPE.md`
- `docs/ARCHITECTURE.md`
- `docs/DOMAIN_MODEL.md`
- `docs/GOLDEN_PATH.md`
- `docs/ACCEPTANCE_TESTS.md`

Legacy reference:

- `docs/reference/LEGACY_ARCHITECTURE_AUDIT.md`

The legacy project is reference-only.

Do not port large legacy modules without explicit approval.

## Development rules

Before implementing a task:

1. Read relevant docs.
2. Inspect existing interfaces.
3. Write/update tests first when practical.
4. Implement the smallest coherent change.
5. Run targeted tests.
6. Run full tests when the phase is complete.

## Agent topology

A single bounded agent loop is the default. Multi-agent designs (e.g.
diagnosis/execution sub-agents) are permitted since 2026-08-24 by owner
decision, under two hard conditions:

- Invariant 4 still applies: no agent may directly mutate sensitive Ticket
  state; execution goes through deterministic services + Approval.
- Each added agent must have a single clear responsibility and its own
  tests; no shared mutable memory between agents.

## Forbidden

Do not introduce without explicit request:

- MCP
- GraphRAG
- Kafka
- Kubernetes
- ERPNext
- ten-system architecture
- legacy compatibility APIs
- multiple Ticket APIs
