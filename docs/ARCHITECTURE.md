# ARCHITECTURE

## Core invariants

1. Channel identity != canonical user.
2. Session != user.
3. Session != memory.
4. Agent must not directly mutate sensitive Ticket state.
5. Ticket current state and TicketEvent must be committed transactionally.
6. Approval is an independent state machine.
7. Low-confidence RAG must not become free-form model answers.
8. Cross-channel continuation must resolve through canonical user identity.

## Layered view

```text
transport/http (webhooks: /webhooks/wecom, /webhooks/feishu; operator API; approvals API)
        ↓
application (services: IdentityResolver, TicketService, ReplyService, ApprovalService)
        ↓
domain (User, ChannelIdentity, Session, Ticket, TicketEvent, Approval, Memory)
        ↓
infrastructure (repositories, sqlite, llm client, retrieval index)
```

## Key boundaries

### Channel Adapter

Responsibility: raw payload → `InboundEnvelope`. Only:

```text
Raw Payload → InboundEnvelope
```

Adapters MUST NOT access Ticket / RAG / Workflow / Memory.

### Canonical Identity

`ChannelIdentity` (channel + channel_user_id) is bound to a canonical `User`.

```text
wecom/zhangsan → user_001
feishu/ou_001  → user_001
```

### Session

A `Session` belongs to a `User` after identity resolution. Session is NOT the user identity. Session is NOT memory.

### Ticket

A `Ticket` belongs to a `User`. Status transitions are limited to:

```text
OPEN → IN_PROGRESS → RESOLVED → CLOSED
```

Every status change writes a `TicketEvent` in the same transaction.

### Approval

Approval is an independent state machine. A ticket remains valid while an approval is PENDING. Approval state is NOT stored on the ticket.

## What we deliberately do NOT reproduce from legacy

- `metadata_json` blob carrying approval/memory/trace/business state
- Composite status (`status` + `lifecycle_stage` + `handoff_state`)
- Legacy + v2 Ticket API dual track
- Session-centric ticket resolution
- Multi-layer parallel implementations (Legacy API / v2 API / ten-system)
