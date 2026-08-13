# DOMAIN_MODEL

## Entities

### InboundEnvelope

```text
channel: str
message_id: str
channel_user_id: str
conversation_id: str
text: str
timestamp: datetime
trace_id: str
metadata: dict
```

### User (canonical)

```text
id: str            # user_001
display_name: str
created_at: datetime
```

### ChannelIdentity

```text
id: str
user_id: str       # FK → User
channel: str       # wecom | feishu
channel_user_id: str
UNIQUE(channel, channel_user_id)
```

Invariant: ChannelIdentity belongs to a User. Binding is what enables cross-channel continuation.

### Session

```text
id: str
user_id: str       # FK → User (after identity resolution)
channel: str
channel_conversation_id: str
created_at: datetime
```

Invariant: Session belongs to a User after identity resolution. Session is NOT the user identity.

### Ticket

```text
id: str            # T1001
user_id: str       # FK → User
title: str
description: str
status: TicketStatus
assignee_user_id: str | null   # canonical operator (V2)
summary: str | null            # persisted agent summary (V2)
category: str | null
priority: str | null
queue: str | null              # e.g. facility
source_conversation_id: str | null
created_at: datetime
updated_at: datetime
```

Status limited to: `OPEN | IN_PROGRESS | RESOLVED | CLOSED`.

### TicketEvent

```text
id: str
ticket_id: str     # FK → Ticket
event_type: str    # created | claimed | resolved | closed
                   #        | resolution_rejected | escalated | force_closed
actor_user_id: str | null   # WHO (canonical) — V2 audit
trace_id: str | null        # WHICH trace triggered it — V2 audit
conversation_id: str | null # WHERE (conversation) it happened — V2 audit
payload: json
created_at: datetime
```

Invariant: Ticket current state and TicketEvent are committed transactionally.

### Conversation (V2)

```text
id: str
channel: str                 # wecom | feishu
channel_conversation_id: str # external group/dm id
conversation_type: DM | GROUP
purpose: REQUESTER | OPERATOR | APPROVAL
queue: str | null            # operator queue (e.g. facility)
location: str | null         # e.g. A3
enabled: bool
UNIQUE(channel, channel_conversation_id)
```

Invariant: Channel != Role — a channel connector's capability is separate
from the conversation purpose assigned to a specific conversation id.

### Role (V2)

```text
user_id: str     # FK → User (canonical, may hold multiple roles)
role: requester | operator | approver
queue: str | null
```

Invariant: Operator identity is canonical — `wecom/lihua` and
`feishu/ou_lihua` resolve to the same operator user, so `actor_user_id`
is identical across channels.

### PendingAction (V2, HITL)

```text
id: str
ticket_id: str
action_type: str   # whitelist: escalate | force_close
payload: json
requested_by: str
approval_id: str
execution_status: PENDING | APPROVED | REJECTED | EXECUTED | SKIPPED
executed_at: datetime | null
```

Invariant: Approval-required actions are NOT stored in ticket metadata; they
have their own model and execute through the deterministic executor.

### Approval (independent state machine)

```text
id: str
ticket_id: str
action: str
status: PENDING | APPROVED | REJECTED
requested_by: str
decided_at: datetime | null
```

Invariant: Approval is independent of Ticket. A PENDING approval does not mutate ticket status.

### Memory

```text
id: str
user_id: str
ticket_id: str     # source ticket
kind: str          # stable_fact | summary
fact: str
confidence: float
created_at: datetime
```

Invariant: Memory is derived from closed tickets (preferring requester-
confirmed closure), keyed to the canonical user.

### Notification (V2)

```text
id: str
channel: str
target: str              # resolved conversation or user target
notification_type: str   # REACTIVE_REPLY | PRIVATE_DETAIL |
                         # REQUESTER_STATUS_UPDATE | OPERATOR_WORK_ITEM |
                         # OPERATOR_ACTION_RECEIPT |
                         # REQUESTER_CONFIRMATION_REQUEST |
                         # APPROVAL_REQUEST | APPROVAL_RESULT | INTERNAL_NOTE
visibility: PUBLIC | PRIVATE | INTERNAL
message: str
source_event_id: str     # business event that produced it
status: pending | sent | failed
attempt_count: int
created_at: datetime
UNIQUE(source_event_id, notification_type, target)   # dedupe
```

Invariant: Ticket change + TicketEvent + outbox record commit in ONE
transaction; channel delivery happens after commit.

### Outbound (V2)

```text
ChannelCapability: DM_INBOUND | GROUP_INBOUND | DM_OUTBOUND |
                   GROUP_OUTBOUND | WEBHOOK_VERIFICATION
OutboundMessage   (channel, DeliveryTarget, text, notification_type)
DeliveryTarget    (channel, kind: USER|CONVERSATION, target_id)
DeliveryResult    (ok, code, error, attempt)
```

## Ticket state machine

```text
OPEN ──claim──▶ IN_PROGRESS ──resolve──▶ RESOLVED ──confirm──▶ CLOSED
                  ▲                            │
                  └───────── reject ────────────┘
IN_PROGRESS ──force_close──▶ CLOSED
```

Valid transitions only:

- OPEN → IN_PROGRESS (claim, requires `assignee_user_id IS NULL`)
- IN_PROGRESS → RESOLVED (resolve)
- RESOLVED → CLOSED (requester confirmation)
- RESOLVED → IN_PROGRESS (resolution rejected by requester)
- IN_PROGRESS → CLOSED (operator force close, reason required)

Every transition produces a TicketEvent (with actor + trace).

## Decision: not reproduced from legacy

- No composite state (`status` + `lifecycle_stage` + `handoff_state`)
- No `metadata_json` blob on ticket (no pending actions / approval state in it)
- No hidden status values beyond the four above
