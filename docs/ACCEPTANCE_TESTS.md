# ACCEPTANCE_TESTS

Definition of Done: AC-01 .. AC-10 all green.

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

## Objective quality gates (produced by tests, not pre-written)

- 100+ replay events → 0 duplicate tickets
- Cross-channel seeded cases → 100% correct user identity resolution
- Cross-channel single-active-ticket cases → 100% same-ticket continuation
- FAQ evaluation → Recall@3 ≥ 90% (target; fill with real results)
- Memory extraction evaluation → Precision ≥ 85% (target; fill with real results)
