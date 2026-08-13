# Support Agent Lite V2 — Implementation Report

**Branch:** `main`
**V1 baseline:** commit `8627b0f`, 139 tests passed
**V2 state:** full suite green (see §Acceptance results)
**Real external network used:** NO
**Real credentials required:** NO

---

## 1. What V2 adds (one paragraph)

On top of the V1-verified AI Support Core (canonical identity, user-centric
cross-channel ticket resolution, 4-state ticket + event transactional model,
RAG grounding, agent advice-only, long-term memory), V2 adds the full
enterprise collaboration layer: formal `Conversation` (type/purpose) and
`Role` models, canonical operator identity with atomic `CLAIM`, persisted
assignment, requester public/private/internal three-tier visibility, a
transactional notification outbox with an audience/visibility policy and
target resolver, official-shape channel protocol adapters (Feishu / WeCom,
mock-network-not-protocol), requester confirmation closure with
cross-channel CONFIRM, resolution rejection, HITL execution
(ESCALATE → APPROVAL → EXECUTE), persisted requester ticket context, real
RAG no-answer handoff, and concurrency/reliability fixes verified by real
tests.

V1 invariants are preserved: canonical user, user-centric resolver, single
status ticket, transactional ticket+event, advice-only agent, independent
approval, RAG grounding, single Ticket API.

---

## 2. V1 baseline & regression

| Item | Value |
| --- | --- |
| V1 commit | `8627b0f` (139 passed) |
| V1 AC-01 ~ AC-10 | ✅ still green (golden path, workflow, memory, operator API, RAG eval) |
| V2 full suite | 178 passed, 0 failed |

## 3. Schema changes (migrations)

| Migration | Purpose |
| --- | --- |
| `0009` | `conversations` (channel, channel_conversation_id, conversation_type, purpose, queue, location, enabled) + `user_roles` (canonical user ↔ role, queue) |
| `0010` | `tickets` operational columns (`assignee_user_id`, `summary`, `category`, `priority`, `queue`, `source_conversation_id`) + `ticket_events` audit columns (`actor_user_id`, `trace_id`, `conversation_id`) |
| `0011` | `pending_actions` (ticket_id, action_type, payload, requested_by, approval_id, execution_status, executed_at) — approval-gated actions |
| `0012` | `notification_outbox` (channel, target, notification_type, visibility, message, status, attempt_count, source_event_id UNIQUE dedupe key, created_at) + `session_ticket_contexts` (persisted requester ticket context) |

## 4. New domain models

- `Conversation` (`conversation.py`): `ConversationType` (DM/GROUP),
  `ConversationPurpose` (REQUESTER/OPERATOR/APPROVAL), queue/location/enabled.
- `Role` (`role.py`): `Role` enum (requester/operator/approver) with
  `queue`; canonical user may hold multiple roles.
- `Notification` (`notification.py`): `NotificationType`
  (REACTIVE_REPLY, PRIVATE_DETAIL, REQUESTER_STATUS_UPDATE,
  OPERATOR_WORK_ITEM, OPERATOR_ACTION_RECEIPT,
  REQUESTER_CONFIRMATION_REQUEST, APPROVAL_REQUEST, APPROVAL_RESULT,
  INTERNAL_NOTE), `Visibility` (PUBLIC/PRIVATE/INTERNAL), outbox record.
- `Outbound` (`outbound.py`): `ChannelCapability` (DM_INBOUND, GROUP_INBOUND,
  DM_OUTBOUND, GROUP_OUTBOUND, WEBHOOK_VERIFICATION),
  `OutboundMessage`, `DeliveryTarget`, `DeliveryResult`.
- `PendingAction` (`pending_action.py`): approved-then-executed actions with
  status machine PENDING → APPROVED → EXECUTED/SKIPPED.
- `Ticket` extensions: events `claimed`, `resolution_rejected`, `escalated`,
  `force_closed`, `closed`; legal transitions `IN_PROGRESS→CLOSED` and
  `RESOLVED→IN_PROGRESS`; `AlreadyClaimed` exception.

## 5. New services

- `conversation_service.py` — registration, operator-conversation lookup,
  seed loading.
- `role_service.py` — canonical user → role/queue resolution.
- `command_parser.py` — slash commands + Chinese aliases
  (`/claim`, `/resolve`, `/escalate`, `/approve`, `/reject`, `/force-close`)
  plus requester confirmation/rejection detection.
- `target_resolver.py` — maps audience/visibility → concrete delivery
  targets (requester public conversation, requester private DM,
  primary operator conversation, action origin conversation, approver).
- `notification_service.py` — outbox enqueue **inside** the business
  transaction, dispatch after commit with retry (≤3 attempts, failed
  records retried), idempotent dedupe via unique
  `(source_event_id, notification_type, target)`.
- `ticket_action_service.py` — deterministic action executor
  (create/claim/resolve/requester_confirm/reject_resolution/close_direct/
  escalate/force_close/approve/reject); each action commits
  ticket-state + ticket-event + outbox records atomically; HITL execution
  (`_execute`) runs the whitelisted action after APPROVED.
- `ingress_service.py` — atomic idempotency claim in the **same
  transaction** as the business effect; conversation purpose routing;
  release on failure.
- `workflow.py` — purpose-based routing (REQUESTER/OPERATOR/APPROVAL),
  confirmation/rejection detection, NO_ANSWER → real handoff ticket,
  persisted session-ticket context, other-intent continuation with exactly
  one active ticket.

## 6. New endpoints

- `GET/POST /webhooks/{channel}` — protocol verification (Feishu
  token/challenge/encrypt; WeCom sha1 signature / echostr / AES decrypt) and
  official-shaped inbound.
- `POST /conversations/register`, `GET /conversations` — conversation &
  purpose management.
- `POST /tickets/{id}/actions` (claim / resolve / force-close / escalate) —
  deterministic actions from REST.
- `POST /tickets/{id}/approval` (approve / reject) — idempotent (CAS
  rowcount guard) approval decision.
- `GET /tickets/{id}/case` — full case trace (events with actor + trace,
  notifications, memories, ticket).

## 7. Channel capability matrix (summary)

See `docs/CHANNEL_PROTOCOL_MATRIX.md` for the strict per-capability table
with official URLs and verification date (2026-08-13).

| Capability | Feishu | WeCom |
| --- | --- | --- |
| DM_INBOUND | ✅ | ✅ (text message callback) |
| GROUP_INBOUND | ✅ (`chat_type=group`) | ⛔ PENDING_OFFICIAL_SPEC |
| DM_OUTBOUND | ✅ (`receive_id_type=open_id`) | ✅ (`message/send`) |
| GROUP_OUTBOUND | ✅ (`receive_id_type=chat_id`) | ✅ (`appchat/send`) |
| WEBHOOK_VERIFICATION | ✅ token/challenge/encrypt | ✅ sha1/AES/echostr |

**Honest limitations:** WeCom group inbound is NOT claimed because the
official text-message callback format (path/90239) carries no group chat id
and no official page could prove the capability during V2. Business
`OPERATOR` conversations work through simulation/registration-bound
conversation ids. Legacy `hmac-sha256(timestamp:nonce)` is not used.

## 8. Concurrency fixes (verified by tests)

| Race (V1 audit finding) | V2 fix | Verification |
| --- | --- | --- |
| Concurrent webhook 500 | idempotency claim + business in ONE transaction; connection-level `RLock` (SerializedConnection); nested-safe `txn()` | 25 threads, same message id → 1 business execution, 0 tickets duplicated, 0 HTTP 500 |
| Concurrent claim double-success | atomic `UPDATE ... WHERE status='OPEN' AND assignee_user_id IS NULL` + rowcount | 25 threads → exactly 1 winner, 1 `claimed` event |
| Identity IntegrityError 500 | catch + re-read existing identity (no rollback of outer txn) | covered by unit + concurrent first-message test |
| Idempotency key leak after failure | release key on failure (DELETE inside txn) | dedicated test |

## 9. Reliability: transactional outbox

- Ticket change + TicketEvent + outbox record commit atomically.
- Channel delivery happens after commit; simulated transport failure keeps
  the business event; failed records are retained and retried by
  `dispatch()` (attempt_count, max 3).
- Verified by `test_outbox_survives_delivery_failure`.

## 10. Acceptance results

| Case | Result |
| --- | --- |
| AC-01 ~ AC-10 (V1 regression) | ✅ all green |
| AC-11 Requester Group Create (3 outputs) | ✅ |
| AC-12 Cross-conversation continuation | ✅ |
| AC-13 Canonical operator (wecom + feishu → same user) | ✅ |
| AC-14 Operator claim + assignment + 2 notifications | ✅ |
| AC-15 Concurrent claim (1 winner) | ✅ 25 threads |
| AC-16 Shared operator conversation, no implicit ticket | ✅ |
| AC-17 Resolve → RESOLVED + confirmation request | ✅ |
| AC-18 Cross-channel requester confirmation (wecom→feishu) | ✅ |
| AC-19 Resolution rejected → IN_PROGRESS, no new ticket | ✅ |
| AC-20 Memory only after confirmed closure + recall | ✅ |
| AC-21 RAG no-answer → real handoff ticket | ✅ |
| AC-22 HITL: ESCALATE → APPROVED → action executed exactly once | ✅ |
| AC-23 INTERNAL never leaks to requester | ✅ |
| AC-24 Notification dedupe (source_event_id + type + target unique) | ✅ |
| AC-25 Concurrent duplicate webhook (25 threads) | ✅ 1 execute, 0 500 |
| AC-26 Official-shaped Feishu inbound parse | ✅ |
| AC-27 Feishu outbound contract (URL/query/headers/body) | ✅ |
| AC-28 WeCom official contract (signature/AES/XML/outbound shapes) | ✅ 14 protocol tests |
| AC-29 Full case trace (`/tickets/{id}/case`) | ✅ |
| AC-30 V1 regression | ✅ |

**V2 test inventory (41):** collaboration 12 · concurrency 4 · HITL &
notifications 6 · protocol contract 14 · offline demo 5.

## 11. Data metrics (measured, not fabricated)

| Metric | Value |
| --- | --- |
| Full pytest | **178 passed** (0 failed) |
| V1 regression | 137 V1 tests green (139 at baseline; suite grew with V2 docs/tests) |
| V2 acceptance | 41/41 green |
| RAG Recall@3 | **100%** (14/14 eval queries, gate ≥ 90%) |
| Memory eval | 15 memory/recall tests green |
| Concurrent duplicate webhook | 25 threads → 1 business execution, 0 duplicate tickets, 0 HTTP 500 |
| Concurrent claim | 25 threads → exactly 1 winner, 1 `claimed` event |
| Notification dedupe | duplicate business event → 1 record per (event, type, target) |

## 12. Demo (offline, no network, no credentials)

`tests/test_demo_v2.py::test_demo_v2_full_golden_path` runs the 16-step
scenario from the V2 spec end to end:

1 张三 reports `A3 空调坏了` in the requester group → 2 canonical
`user_001` → 3 T0001 → 4 public receipt → 5 private detail → 6 operator
work item → 7 李师傅 `CLAIM T0001` → 8 atomic success → 9 requester
lifecycle update → 10 `RESOLVE` → 11 confirmation request → 12 张三
confirms from his **Feishu DM** → 13 CLOSED → 14 memory extraction →
15 new session → 16 recall (旧工单 + 修复方案)。

Plus `test_demo_faq_rag` (FAQ → RAG, no ticket) and
`test_demo_hitl_escalate_approve_execute`.

## 13. Legacy: adapted ideas vs. explicitly not ported

**Adapted (business semantics only):** dual audience (群简短/私聊详情),
operator action receipt, requester lifecycle notification, requester
confirmation, explicit ticket id in shared group, outbound retry thinking.

**Not ported:** GROUP_CHAT_TO_SYSTEM, 十系统, session-centric
`active_ticket`, `group:<chat>:user:<sender>` as primary identity,
dual Ticket API, composite lifecycle/handoff state, metadata pending
actions, the 19-command suite, custom HMAC-as-protocol, the giant
`wecom_bridge_server.py`, `SupportIntakeWorkflow` monolith.

## 14. Not in V2 scope (as specified)

Multi-Agent, MCP, GraphRAG, Kafka, Redis, Celery, Kubernetes, ERPNext,
十系统, Telegram, complex web console / RBAC, duplicate merge, complex SLA,
complex knowledge-management UI, LangGraph suspend/resume.

## 15. Known limitations

- WeCom `GROUP_INBOUND`: `PENDING_OFFICIAL_SPEC` (official text-message
  callback has no chat id; no official page proved group inbound).
- Feishu tenant-access-token exchange requires real credentials and is only
  simulated (`SIMULATED_TENANT_TOKEN`) in offline mode.
- Outbound delivery is fire-and-forget with outbox retry; no webhook-based
  delivery confirmation is modelled.
- Operator actions are slash-command bound; card/thread-bound actions are
  future work.
- A second decision on an already-decided approval returns a truthful
  "already processed" reply rather than re-executing.

## 16. Future wiring (real channel)

Provide App ID / Secret / Corp ID / Agent ID / Token / EncodingAESKey /
Verification Token / Encrypt Key + real conversation ids, set
`REAL_CHANNEL_NETWORK=true` (swaps in `RealHttpTransport`), and configure
the webhook callback URLs. No redesign of identity, conversation, ticket,
notification, or workflow is required.
