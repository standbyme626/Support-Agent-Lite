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
created_at: datetime
updated_at: datetime
```

Status limited to: `OPEN | IN_PROGRESS | RESOLVED | CLOSED`.

### TicketEvent

```text
id: str
ticket_id: str     # FK → Ticket
event_type: str    # created | started | resolved | closed | ...
payload: json
created_at: datetime
```

Invariant: Ticket current state and TicketEvent are committed transactionally.

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

Invariant: Memory is derived from closed tickets, keyed to the canonical user.

## Ticket state machine

```text
OPEN ──claim──▶ IN_PROGRESS ──resolve──▶ RESOLVED ──close──▶ CLOSED
```

Valid transitions only:

- OPEN → IN_PROGRESS (claim)
- IN_PROGRESS → RESOLVED (resolve)
- RESOLVED → CLOSED (close)

Every transition produces a TicketEvent.

## Decision: not reproduced from legacy

- No composite state (`status` + `lifecycle_stage` + `handoff_state`)
- No `metadata_json` blob on ticket
- No hidden status values beyond the four above
