# IMPLEMENTATION_BACKLOG

Backlog across V1 + V2. All V1 and V2 items are **done**; the remaining
lines are future work that is explicitly NOT in scope unless requested.

## V1 — done (AC-01 .. AC-10)

- [x] P0 skeleton, migrations, `/health`
- [x] P1 domain + strict state machine + transactional Ticket+Event
- [x] P2 IdentityResolver / SessionService / TicketService / TicketResolver
- [x] P3 WeCom/Feishu adapters + webhooks + message-id idempotency
- [x] P4 IntentRouter / RAG Retriever / ContextBuilder / SupportAgent / Workflow
- [x] P5 Operator API (claim/resolve/close/escalate) + Approval state machine
- [x] P6 Memory: extraction on CLOSED + next-session recall
- [x] P7 Trace (trace_id end-to-end) + 10 golden-path tests + demo

## V2 — done (AC-11 .. AC-30)

- [x] Conversation model (type + purpose) + seed conversations
- [x] Role model (canonical operator/approver, queue)
- [x] Ticket operational fields + TicketEvent audit (actor/trace/conversation)
- [x] Atomic claim (1 winner), persisted assignment
- [x] Requester public / private / internal visibility
- [x] Transactional notification outbox (dedupe + retry)
- [x] Official-shape protocol adapters (Feishu token/challenge/AES,
      WeCom sha1/AES/XML), honest capability flags
- [x] Operator slash commands (claim/resolve/escalate/force-close/approve/reject)
- [x] Requester confirmation closure (cross-channel) + resolution rejection
- [x] Memory only after confirmed closure
- [x] NO_ANSWER → real handoff ticket
- [x] HITL execution chain (approve → execute once)
- [x] Concurrency fixes (idempotency same-txn, atomic claim, serialized conn)
- [x] Case trace endpoint + offline 16-step demo
- [x] `docs/CHANNEL_PROTOCOL_MATRIX.md` + `docs/V2_IMPLEMENTATION_REPORT.md`

## Future work (NOT in scope unless explicitly requested)

- Real channel wiring: credentials + `REAL_CHANNEL_NETWORK=true` +
  webhook callback URLs (no redesign required)
- WeCom `GROUP_INBOUND`: blocked on official spec
  (`PENDING_OFFICIAL_SPEC`; official text-message callback has no chat id)
- Card/thread-bound operator actions
- Outbound delivery confirmation via channel webhooks
- Anything listed as out of scope in `docs/PRODUCT_SCOPE.md`
  (Multi-Agent, MCP, GraphRAG, Kafka, Redis, Celery, Kubernetes, ERPNext,
  十系统, Telegram, complex RBAC/console/SLA, duplicate merge)
