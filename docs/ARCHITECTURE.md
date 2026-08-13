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
9. Channel != Role: a channel connector's capabilities (group/dm in/out)
   are channel capability, never business rules.
10. A shared operator conversation has no implicit active ticket: operator
    actions require an explicit ticket id.
11. Mock network, not protocol: channel adapters follow official docs only;
    unproven capabilities are `UNSUPPORTED / PENDING_OFFICIAL_SPEC`.

## Layered view

```text
transport/http (webhooks: /webhooks/{channel}; conversations; ticket actions;
                approval; case trace; memories; traces)
        ↓
application (services: IdentityResolver, ConversationService, RoleService,
             TicketService, TicketActionService, NotificationService,
             CommandParser, TargetResolver, Workflow, ApprovalService)
        ↓
domain (User, ChannelIdentity, Session, Conversation, Role, Ticket,
        TicketEvent, Approval, PendingAction, Notification, Outbound, Memory)
        ↓
infrastructure (repositories, sqlite, llm client, retrieval index,
                idempotency, notification outbox)
```

## Key boundaries

### Channel Adapter

Responsibility: raw payload → `InboundRequest` (verification/challenge)
→ `InboundEnvelope`. Only:

```text
Raw Payload → [verify] → InboundEnvelope
```

Adapters MUST NOT access Ticket / RAG / Workflow / Memory.
Each adapter declares its `ChannelCapability` set honestly.

### Canonical Identity

`ChannelIdentity` (channel + channel_user_id) is bound to a canonical `User`.

```text
wecom/zhangsan → user_001
feishu/ou_001  → user_001
wecom/lihua    → user_ops_001
feishu/ou_lihua→ user_ops_001
```

### Conversation

A `Conversation` is a first-class model: `(channel, channel_conversation_id)`
with `conversation_type` (DM/GROUP) and `purpose` (REQUESTER/OPERATOR/
APPROVAL). Purpose routes workflow; capabilities gate what a connector can
actually do.

### Session

A `Session` belongs to a `User` after identity resolution. Session is NOT the
user identity. Session is NOT memory. Requester ticket context is persisted
(`session_ticket_contexts`), but the user-centric resolver remains the
primary algorithm.

### Ticket

A `Ticket` belongs to a `User`, and may carry `assignee_user_id` (canonical
operator), `summary`, `category`, `priority`, `queue`, `source_conversation_id`.
Status transitions:

```text
OPEN → IN_PROGRESS → RESOLVED → CLOSED
         ↑             │
         └──(rejected)─┘          (RESOLVED → IN_PROGRESS)
IN_PROGRESS → CLOSED              (force close)
```

Every status change writes a `TicketEvent` (with actor + trace) in the same
transaction.

### Approval / HITL

Approval is an independent state machine. A ticket remains valid while an
approval is PENDING. Approved actions execute through the deterministic
action executor (whitelisted `PendingAction`), exactly once (CAS rowcount
guard).

### Notification

Business events enqueue outbox records in the SAME transaction as the
ticket change. Channel delivery happens after commit; failures are retained
and retried. Dedupe key: `(source_event_id, notification_type, target)`.

## What we deliberately do NOT reproduce from legacy

- `metadata_json` blob carrying approval/memory/trace/business state
- Composite status (`status` + `lifecycle_stage` + `handoff_state`)
- Legacy + v2 Ticket API dual track
- Session-centric ticket resolution
- Multi-layer parallel implementations (Legacy API / v2 API / ten-system)
