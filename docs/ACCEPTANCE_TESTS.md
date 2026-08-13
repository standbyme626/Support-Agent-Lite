# ACCEPTANCE_TESTS

Definition of Done: AC-01 .. AC-30 all green. (V1: AC-01..10; V2 adds
AC-11..30. Result: 42 V2 tests across 5 files, see
`docs/V2_IMPLEMENTATION_REPORT.md`.)

## AC-01 WeCom FAQ

Input: `年假怎么申请？`

Result: Identity resolved, Intent = FAQ, RAG hit, grounded answer, NO ticket created.

## AC-02 WeCom auto ticket

Input: `A3 空调坏了`

Result: user resolved → support intent → T1001 created → OPEN → `ticket_created` event.

## AC-03 Webhook idempotency

Same `message_id` sent twice.

Result: 1 message processed, 1 Ticket, 0 duplicate Tickets.

## AC-04 Operator claim

Operator `claim T1001`.

Result: OPEN → IN_PROGRESS, `ticket_started` event.

## AC-05 Feishu cross-channel continuation

Seed: user_001 = wecom/zhangsan + feishu/ou_001. WeCom produced T1001.

Input from feishu: `昨天空调那个事情怎么样了？`

Result: ou_001 → user_001 → T1001. Must NOT create T1002.

## AC-06 Multi-ticket disambiguation

user_001 has T1001 (空调) and T1002 (VPN).

Input: `处理了吗？`

Result: clarification required. LLM must NOT choose arbitrarily.

## AC-07 Agent summary

After multiple messages: ticket summary + recent messages build correct context.

## AC-08 HITL

Execute `escalate`.

Result: Approval PENDING, ticket remains valid. After approve: APPROVED → execute action.

## AC-09 Close → Memory

After T1001 CLOSED: MemoryExtractor produces stable facts.

## AC-10 New Session Recall

New session input `空调又坏了` retrieves prior T1001/A3 resolution as context.

---

## V2 acceptance (AC-11 .. AC-30)

## AC-11 Requester Group Create

Official-shaped simulated channel event, requester group `A3 空调坏了` →

`T1001` + public requester receipt + private requester detail + operator
work item, all derived from the one `ticket_created` business event.

## AC-12 Cross-conversation Continuation

Group created T1001; requester DM `下午三点以后我在办公室` continues
T1001 (no T1002).

## AC-13 Canonical Operator

Same operator via `wecom/lihua` and `feishu/ou_lihua` → same canonical user
(`actor_user_id` identical across channels).

## AC-14 Operator Claim

`CLAIM T1001` → `assignee_user_id` persisted, OPEN → IN_PROGRESS, plus
operator action receipt AND requester lifecycle notification from the same
`ticket_claimed` event.

## AC-15 Concurrent Claim

Two (25) operators claim simultaneously → exactly 1 winner, 1
`ticket_claimed` event, loser gets a conflict.

## AC-16 Shared Operator Conversation

`处理好了` with no explicit ticket id → MUST NOT guess a ticket.

## AC-17 Resolve

`RESOLVE T1001` → IN_PROGRESS → RESOLVED; requester receives a confirmation
request; must NOT auto-CLOSE.

## AC-18 Cross-channel Requester Confirmation

WeCom created T1001; requester confirms from bound Feishu DM → same
canonical user → RESOLVED → CLOSED.

## AC-19 Resolution Rejected

Requester: `还没好` → RESOLVED → IN_PROGRESS (`resolution_rejected` event),
no new ticket.

## AC-20 Memory After Confirmed Close

Long-term memory extraction only after CLOSED; new session recalls.

## AC-21 RAG No-answer Real Handoff

NO_ANSWER claiming "已转人工" must have a real ticket + operator work item.

## AC-22 HITL Execution

`ESCALATE T1001` → Approval PENDING → `APPROVE` → action executed exactly
once (idempotent; second decision gets a truthful already-processed reply).

## AC-23 Notification Visibility

INTERNAL notes reach operators/approvers; requester notifications are never
INTERNAL, requester-facing types stay PUBLIC/PRIVATE.

## AC-24 Notification Dedupe

Same (business event, audience, target) → exactly one notification record.

## AC-25 Concurrent Duplicate Webhook

Same official message id, 25 concurrent sends → 1 business execution, 1
ticket, 0 duplicates, 0 HTTP 500.

## AC-26 Official-shaped Feishu Inbound

Official-shape fixture (schema 2.0, header, im.message.receive_v1) parsed:
`message_id`, `open_id`, `chat_id`, `chat_type`, text; message_id is the
idempotency key.

## AC-27 Feishu Outbound Contract

Recording transport receives requests matching official docs: endpoint,
`receive_id_type` (open_id|chat_id), `receive_id`, Authorization header
shape, content-type, body.

## AC-28 WeCom Official Contract

Only officially documented capabilities: sha1 signature / echostr GET
verification / AES-256-CBC callback decrypt / `message/send` /
`appchat/send` / gettoken caching. GROUP_INBOUND is NOT claimed.

## AC-29 Full Case Trace

Full T1001 lifecycle queryable via `GET /tickets/{id}/case`: events with
actor + trace, notifications, memories, assignment; trace_id links back to
`/traces/{trace_id}`.

## AC-30 Existing V1 Regression

AC-01 .. AC-10 all still green.

## Objective quality gates (produced by tests, not pre-written)

- 100+ replay events → 0 duplicate tickets
- Cross-channel seeded cases → 100% correct user identity resolution
- Cross-channel single-active-ticket cases → 100% same-ticket continuation
- FAQ evaluation → Recall@3 = 100% (14/14; gate ≥ 90%)
- Memory extraction evaluation → Precision = 100% (11/11; gate ≥ 85%)
- Concurrent duplicate webhook (25 threads) → 1 execution / 0 duplicate / 0 500
- Concurrent claim (25 threads) → exactly 1 winner
