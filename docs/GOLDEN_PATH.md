# GOLDEN_PATH

The end-to-end path every new feature must serve. If a capability does not serve this path: not now.

```text
企业微信 / 飞书
        ↓
Channel Adapter (verify → Raw → InboundEnvelope)
        ↓
Canonical Identity (channel_user_id → user)
        ↓
Conversation (type + purpose: REQUESTER / OPERATOR / APPROVAL)
        ↓
Session (belongs to user)
        ↓
Intent Router (FAQ | Support | ProgressQuery | Other)
      /        \
   FAQ         Support
    ↓             ↓
   RAG          Ticket Resolver (create / continue)
    ↓             ↓
 grounded      Context Builder
 answer        ↓
 (no ticket)   Agent (summary / recommendation only)
                ↓
             Workflow (purpose routing)
                ↓
             Ticket Created → 3 outputs:
               · requester public receipt
               · requester private detail (DM)
               · operator work item
                ↓
             Operator (explicit ticket id; claim / resolve)
                ↓
             High-risk Action → Approval (independent) → execute once
                ↓
             RESOLVED → Requester Confirmation (cross-channel OK)
                ↓
             CLOSED → Memory Extraction (stable facts)
                ↓
             Next Session Recall
```

## V2 milestones

4. **Milestone 4 (collaboration layer)**: conversation purpose routing,
   canonical operator claim (atomic, 1 winner), assignment persisted,
   requester public/private/internal split, transactional outbox with
   dedupe, official-shape channel contracts (mock network, not protocol).
5. **Milestone 5 (closure + HITL + reliability)**: requester confirmation
   closes the ticket (cross-channel), rejection returns to processing,
   ESCALATE → APPROVE → action executed once, case trace covers the full
   lifecycle, outbox survives simulated delivery failure.
