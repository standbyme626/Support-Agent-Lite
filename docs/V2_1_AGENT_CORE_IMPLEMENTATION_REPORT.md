# Support Agent Lite V2.1 — Agent Core Implementation Report

**Date:** 2026-08-14
**Spec:** `v2.md` V2.1 Agent Core Completion (§0 ~ §49)
**V2 baseline:** commit `925d829`, 179 tests passed
**V2.1 state:** full suite **235 passed, 0 failed** (offline, deterministic)
**Real external network used:** NO (default `pytest` is forced offline)
**Real credentials required:** NO

---

## 1. Repository State

```text
branch:       main
HEAD:         925d829f34f0218b9446f24fe6c769e1a3f25e68
              (docs: expand README with architecture, flowcharts and protocol details)
worktree:     V2.1 Agent Core WIP implemented on top of HEAD (uncommitted)
              + this report + README/docs updates
```

The worktree carried an extensive WIP (PromptRegistry, two-phase ingress,
AgentContext/AgentDecision/AgentToolPort/Policy, eval suite, V2 closure
fixes). The task **kept and completed** that WIP rather than replacing it:
audited every module against the V2.1 spec, fixed the remaining gaps, and
re-verified the full suite.

---

## 2. What Changed

### AgentContext (`app/application/context_builder.py`)
- Contract changed from "summary + recent messages" to the **full
  perception the model actually sees** (AC-A01): current message, canonical
  user, actor role, channel, conversation type/purpose, location, current
  ticket + persisted summary, recent conversation (chronological,
  role-labeled, limit 6), recalled memories, initial RAG evidence
  (`KnowledgeEvidence`).
- Nothing is collected-but-unused anymore: every field is rendered into the
  agent prompt.

### SupportAgent (`app/application/support_agent.py`)
- Bounded loop: **max 3 steps, max 2 read tool calls** (no infinite ReAct).
- Full-perception prompt rendering via `PromptRegistry`; untrusted user
  content is delimited (`<user_message> … </user_message>`) and the system
  policy forbids treating it as instructions.
- Deterministic keyword classify/prioritize/action-map **kept as the
  fallback**, but on the LLM path category/priority/action come from the
  schema-validated decision (no more silently-discarded LLM output).
- Every failure mode (no LLM / timeout / malformed JSON / invalid enums /
  oversized reply / denied tool) degrades to deterministic rules.

### AgentDecision (`app/application/agent_decision.py`, new)
- Full schema: understanding, summary, category, priority_suggestion,
  recommended_action, missing_information[], confidence, needs_human,
  needs_approval, reply_draft (≤300 chars), memory_refs[], knowledge_refs[],
  action_proposal, rationale.
- `validate_decision` enforces enum whitelists, intent→action mapping,
  confidence clamping, hallucinated-ref dropping, proposal whitelist
  (ESCALATE/FORCE_CLOSE), force-close-without-reason rejection, and
  cross-ticket proposal rejection.

### Tools (`app/application/agent_tools.py`, new)
- `AgentToolPort` with exactly 4 READ-ONLY tools (history/knowledge/memory/
  allowed-actions), whitelist enforced in the port AND the loop; write
  tools do not exist.

### Policy (`app/application/policy.py`, new)
- `PolicyValidator`: `Agent proposes. Policy validates. Domain decides.`
  Checks proposal whitelist, minimum confidence (0.6), ticket state,
  FORCE_CLOSE legality (reason required, only IN_PROGRESS/RESOLVED), ticket
  mismatch.

### RAG + Memory integration
- High-confidence retrieval → `KnowledgeEvidence` → grounded Agent
  synthesis with `knowledge_refs` (agent may only answer from evidence).
- Low confidence → NO_ANSWER → **real ticket + operator work item** — never
  an LLM free answer (invariant #7, AC-A05).
- Recalled memories enter the prompt AND the decision carries `memory_refs`
  validated against the context (AC-A04).
- `Memory.confidence` is now a real ranking input:
  `effective = relevance × (0.5 + 0.5·confidence)` (§17 Option A) + test.

### Transaction model (`app/application/ingress_service.py` +
`app/infrastructure/processing.py`, new)
- Two-phase ingress: Transaction A (claim + identity + session +
  conversation + deterministic effects + `AGENT_PENDING`) COMMIT → Agent
  run (no DB write lock) → Transaction B (CAS advance, decision
  persistence, notifications, proposals, `COMPLETED`) COMMIT → post-commit
  dispatch.
- Durable lifecycle: `RECEIVED → AGENT_PENDING → AGENT_COMPLETED →
  COMPLETED` + `FAILED_RETRYABLE`; duplicate deliveries resume from
  AGENT_PENDING/FAILED_RETRYABLE (no duplicate tickets/events); COMPLETED
  duplicates are no-ops.

### Observability
- Per-run trace: run_id, prompt_key/version, model, latency_ms, steps,
  tool_calls, summary, confidence, knowledge/memory refs, fallback status,
  error_type. Raw prompts are never persisted.

### PromptRegistry (`app/application/prompt_registry.py`, new)
- Versioned (`<key>.v1.md` + front-matter meta), deterministic loading,
  missing-variable errors, literal-brace escaping (`{{`/`}}`), unbalanced
  brace rejection, JSON extraction. Own test file.

### V2 closure fixes
- §27 closure bypass: REST `/close` deprecated → requires reason →
  FORCE_CLOSE approval pipeline; normal close only via requester
  confirmation. OPEN tickets can never be force-closed.
- §26 PRIVATE_DETAIL first-contact: first-ever group message resolves the
  channel USER identity target without a prior DM; the public receipt never
  claims "已私发给你" when no private target resolves.
- §28 REST trust boundary: every control-plane action resolves the
  canonical actor and verifies the required role (401 unknown actor, 403
  wrong role).
- §30 case trace: `/tickets/{id}/case` now aggregates ticket + events +
  notifications + delivery attempts + approvals + pending actions +
  memories (AC-29).
- §32/§33 zombie paths: `IntentRouter.llm_classify_fn` and
  `MemoryExtractor(llm=...)` were removed — one semantic owner (the Agent),
  deterministic closed-case extraction stays.

### Concurrency correctness fix (`app/infrastructure/db.py`)
- `SerializedConnection.execute` previously returned a raw sqlite3 cursor
  whose `fetchall()`/`fetchone()`/iteration steps ran OUTSIDE the
  serialization lock. Under concurrent webhooks this let one thread's
  transaction interleave with another thread's in-flight read statement on
  the shared connection — observed as torn outbox rows (`None` enum
  values) and `cannot start a transaction within a transaction` failures
  in `test_concurrent_first_contact_same_identity_no_500` (~1/20 runs).
- Fixed with `SerializedCursor`: all row fetching stays inside the lock.
  The race test now passes 40+ consecutive runs (was ~1/20 flaky).

### Eval (`tests/test_agent_eval.py`, new)
- AC-A20 golden set: **14 deterministic cases** (triage ×3, continuation,
  semantic urgency, memory repeat, RAG grounded, clarification, injection,
  unavailable/timeout/malformed fallbacks, invalid enum, tool limit) with
  an explicit pass-rate metric (100%).

---

## 3. Architecture Before / After

```text
BEFORE (V2, "deterministic workflow + LLM polish")

Inbound → Idempotency → Identity → Intent(keyword) → Workflow
      → ContextBuilder(summary) → SupportAgent(loose fields)
      → [LLM inside the serialized write transaction]
      → TicketActionService → Notification
      ✗ context partially unused · ✗ crash window after commit
      ✗ /close backdoor · ✗ arbitrary REST actors

AFTER (V2.1, bounded stateful agent)

Inbound → Claim + Identity/Session/Conversation ─┐  Transaction A
      → Deterministic pre-routing (intent/ticket/commands/confirm) │
      → deterministic effects + processing = AGENT_PENDING ───────┘
      → AgentContext (full perception)                        COMMIT
      → Bounded SupportAgent (≤3 steps · ≤2 read-only tools)
            ─── LLM + tools, NO DB write lock ───
      → Structured AgentDecision ─┐
      → PolicyValidator ──────────┤  Transaction B (CAS, exactly once)
      → HITL (proposal) / apply (persist decision + notifications) │
      → processing = COMPLETED ────────────────────────────────────┘
      → TicketActionService (deterministic) → TicketEvent → Outbox
      → post-commit dispatch
```

---

## 4. Agent Decision Contract (final schema)

`validate_decision(parsed, allowed_memory_ids, allowed_knowledge_ids,
context_ticket_id, intent) -> (AgentDecision | None, reason)`

| Field | Type / Enums | Validation |
| --- | --- | --- |
| `understanding` | string | non-empty optional |
| `summary` | string | required |
| `category` | `account|network|device|software|billing|hr|general` | must match |
| `priority_suggestion` | `high|normal|low` | must match |
| `recommended_action` | `dispatch_repair|network_triage|software_support|credential_reset|finance_review|hr_review|assign_operator|ask_clarification|faq_answer` | must match intent→action map |
| `missing_information` | [string] | list of strings |
| `confidence` | number | clamped to [0,1] |
| `needs_human` / `needs_approval` | boolean | parsed safely |
| `reply_draft` | string | required, ≤300 chars (else fallback) |
| `memory_refs` / `knowledge_refs` | [string] | only ids present in context (anti-hallucination) |
| `action_proposal` | `{action: ESCALATE\|FORCE_CLOSE, reason, confidence, ticket_id}` or null | whitelist; FORCE_CLOSE requires reason; ticket must match context |
| `rationale` | string | short explainable reason (never chain-of-thought) |

Any unusable core field → `(None, reason)` → caller falls back to
deterministic rules. Safe normalization (clamping, ref-dropping, proposal
dropping) never triggers fallback.

---

## 5. Tool Contract (implemented tools)

| Tool | Purpose | Read/Write | Limit |
| --- | --- | --- | --- |
| `get_ticket_history(ticket_id)` | full event history + recent session | READ ONLY | ≤2/run |
| `search_knowledge(query)` | KB retrieval (returns doc ids + scores) | READ ONLY | ≤2/run |
| `recall_memory(query)` | canonical-user prior-ticket memory | READ ONLY | ≤2/run |
| `get_allowed_actions(ticket_id, actor_role)` | state/role-legal actions | READ ONLY | ≤2/run |

Whitelist enforced in `AgentToolPort.call` and in the agent loop; a denied
tool is recorded as `ok=False` and never executed. There is no
claim/resolve/close/approve/reject/assign/update/execute tool.

---

## 6. Transaction Model

```text
Transaction A (ingress txn)
  idempotency claim → identity → session → conversation
  → deterministic workflow effects (ticket + created event + operator work
    item / confirmation / operator / approval actions)
  → processing state = AGENT_PENDING (agent paths) or COMPLETED
  COMMIT

Agent Run (BETWEEN transactions)
  LLM + read-only tools (autocommit reads). NO DB write lock held
  (AC-A11, verified by SlowLLM lock-freedom test).

Transaction B (CAS-guarded, exactly once)
  advance(key, AGENT_PENDING → AGENT_COMPLETED) — only the winner applies
  → policy-validated decision → operational fields → requester
    notifications → HITL proposals → state = COMPLETED
  COMMIT

Post-commit
  outbox dispatch → delivery attempts
```

Crash recovery:
- Crash after A: record stays `AGENT_PENDING`; duplicate delivery calls
  `workflow.resume()` which rebuilds the agent phase from the record
  (ticket already exists) — deterministic effects are never re-run
  (AC-A12).
- Phase-B failure: state → `FAILED_RETRYABLE` (with error), next duplicate
  delivery resumes.
- `COMPLETED`/`AGENT_COMPLETED`: duplicate deliveries are no-ops.
- Concurrent duplicates: idempotency claim in A + CAS advance in B → one
  business execution, no 500 (10-thread agent-path test).

Idempotency stays exact: the claim and all deterministic business effects
commit in the same transaction.

---

## 7. Safety Invariants

| Invariant | How it holds | Proof |
| --- | --- | --- |
| Agent cannot mutate Ticket directly | no write tools; decision only flows through `set_operational`/actions in phase B; `action_proposal` has zero business effect until Policy + Approval | `test_ac09_malicious_model_cannot_mutate_state`, `test_ac09_close_ticket_text_is_just_text`, `test_ac10_*` |
| Low-confidence RAG guarded | low retrieval confidence → NO_ANSWER → real ticket + operator work item; the agent never free-answers | `test_ac05_low_confidence_handoff_not_free_answer`, `test_retriever.py` invariant #7 tests |
| Approval stays deterministic | proposal → PolicyValidator → PendingAction → Approval; executor is `TicketActionService._execute`, actor = approver | `test_ac10_agent_proposal_requires_approval`, `test_demo_agent_proposes_hitl_executes` |
| Role boundary enforced | REST resolves canonical actor + verifies role; workflow commands check role per purpose | `test_rest_requires_actor/unknown_actor/operator_role`, `test_approval_requires_approver_role` |
| Closure has no backdoor | direct close removed; `/close` = FORCE_CLOSE approval pipeline; OPEN never force-closeable | `test_full_lifecycle_claim_resolve_close`, `test_invalid_transitions_return_409` |
| PRIVATE_DETAIL honesty | private target resolved from channel USER identity without prior DM; receipt never claims private delivery unless target resolved | `test_ac15_private_detail_first_contact` |
| LLM outside write txn | two-phase ingress + processing state machine | `test_ac11_llm_latency_outside_write_lock` |
| Raw prompts never persisted | trace only records meta (key/version/model/latency/refs/…) | `test_agent_observability_trace_fields` |

---

## 8. Acceptance Criteria (AC-A01 ~ AC-A20)

| AC | Requirement | Result | Test(s) |
| --- | --- | --- | --- |
| A01 | Complete context consumption | **PASS** | `test_ac01_agent_input_contains_full_perception`, `test_agent_llm_polish_with_fallback` |
| A02 | Multi-turn continuation | **PASS** | `test_ac02_multiturn_continuation_e2e` |
| A03 | Semantic urgency (not keywords) | **PASS** | `test_ac03_business_urgency_is_semantic` |
| A04 | Memory influence (`memory_refs != []`) | **PASS** | `test_ac04_memory_refs_flow_through`, `test_ac04_memory_reaches_agent_e2e` |
| A05 | Grounded RAG / low-confidence handoff | **PASS** | `test_ac05_high_confidence_grounded_answer`, `test_ac05_low_confidence_handoff_not_free_answer`, `test_ac05_faq_grounded_e2e` |
| A06 | Clarification (`missing_information`, ask_clarification) | **PASS** | `test_ac06_clarification_contract` |
| A07 | Bounded tools (≤2 calls, no write tools) | **PASS** | `test_ac07_write_tools_do_not_exist`, `test_ac07_tool_loop_executes_one_read_tool`, `test_ac07_tool_limit_capped_at_two`, `test_ac07_illegal_tool_rejected` |
| A08 | Structured decision validation (fallback, no leaks) | **PASS** | `test_ac08_confidence_out_of_range_clamped`, `test_ac08_missing_fields_fall_back`, `test_agent_oversized_reply_falls_back`, `test_agent_invalid_enum_falls_back` |
| A09 | State safety (malicious model cannot mutate) | **PASS** | `test_ac09_malicious_model_cannot_mutate_state`, `test_ac09_close_ticket_text_is_just_text` |
| A10 | Approval boundary (proposal → HITL) | **PASS** | `test_ac10_agent_proposal_requires_approval`, `test_ac10_low_confidence_proposal_rejected` |
| A11 | LLM outside long write txn | **PASS** | `test_ac11_llm_latency_outside_write_lock` |
| A12 | Crash-safe processing | **PASS** | `test_ac12_crash_after_phase_a_resumes_without_duplicates`, `test_ac12_phase_b_failure_marks_retryable_then_resumes` |
| A13 | LLM failure fallback (unavailable/timeout/malformed) | **PASS** | `test_ac13_unavailable_timeout_malformed_all_complete`, `test_agent_llm_failure_falls_back_to_rules`, `test_agent_llm_timeout_falls_back` |
| A14 | Agent trace (prompt version/model/latency/decision/tools/fallback/refs) | **PASS** | `test_agent_observability_trace_fields` |
| A15 | PRIVATE_DETAIL first-contact | **PASS** | `test_ac15_private_detail_first_contact` |
| A16 | Closure safety (no unapproved direct close) | **PASS** | `test_full_lifecycle_claim_resolve_close`, `test_invalid_transitions_return_409`, `test_ac09_close_to_memory` |
| A17 | REST role boundary | **PASS** | `test_rest_requires_actor`, `test_rest_unknown_actor_rejected`, `test_rest_requires_operator_role`, approver-role tests |
| A18 | Offline test isolation (even with REAL_CHANNEL_NETWORK=true) | **PASS** | `test_ac18_default_tests_are_offline`; verified by running the suite with the env var exported |
| A19 | V1/V2 regression (AC-01~AC-30) | **PASS** | full suite (V1 golden path, workflow, memory, RAG eval, V2 collaboration/concurrency/HITL/protocol/demo) |
| A20 | Agent quality eval (golden set ≥10, repeatable) | **PASS** | `test_ac20_golden_set_100_percent` — 14/14 (100%) |

Plus §36 prompt-injection tests: `test_injection_message_is_delimited_untrusted`,
`test_injection_cannot_fabricate_business_state`; §37 failure-mode coverage
(empty / plain text / missing fields / unknown enums / confidence>1 /
denied tool / oversized reply) all fall back safely.

**Old-test changes (per §44):** V1/V2 tests that used the unapproved
direct-close or unauthenticated REST were updated because they pinned the
removed behavior; each diff documents the reason (e.g. `test_operator_api.py`
now proves RESOLVED→CLOSED requires confirmation or an approved
FORCE_CLOSE). No assertion was weakened to make the implementation pass.

---

## 9. Test Results (executed for this report)

```bash
$ REAL_CHANNEL_NETWORK=true .venv/bin/python -m pytest -p no:warnings
235 passed in 3.43s
```

| Metric | Value |
| --- | --- |
| Command | `.venv/bin/python -m pytest -p no:warnings` (with `REAL_CHANNEL_NETWORK=true` exported to prove hermeticity) |
| passed | **235** |
| failed | **0** |
| skipped | **0** |
| duration | ~3.4 s (3 runs: 3.42s / 3.45s / 3.43s) |

Plus: the previously-flaky `test_concurrent_first_contact_same_identity_no_500`
(~1/20 failures) was fixed (`SerializedCursor`) and then ran green 40+
consecutive times; the full concurrency + processing-state suites ran green
15 consecutive times.

Targeted runs during development: `tests/test_memory.py` (memory confidence
ranking), `tests/test_agent_core.py`, `tests/test_agent_eval.py`,
`tests/test_processing_state.py`, `tests/test_prompt_registry.py` — all
green.

---

## 10. Files Changed

### New
```text
app/application/agent_decision.py       AgentDecision schema + validation
app/application/agent_tools.py          read-only AgentToolPort (4 tools)
app/application/policy.py               PolicyValidator (proposal gate)
app/application/prompt_registry.py      versioned prompt registry + safe render
app/application/prompts/agent_decision.v1.md   agent prompt (schema/injection-safe)
app/infrastructure/processing.py        inbound processing lifecycle store
storage/migrations/0013_notification_card.up.sql
storage/migrations/0014_inbound_processing.up.sql
tests/fake_llm.py                       Recording/Scripted/Broken/Timeout/Malformed/Slow LLM
tests/test_agent_core.py                AC-A01..A18 + injection + state-safety
tests/test_agent_eval.py                AC-A20 golden set (14 cases)
tests/test_processing_state.py          AC-A11..A14 + concurrency + observability
tests/test_prompt_registry.py           prompt rendering safety
docs/V2_1_AGENT_CORE_IMPLEMENTATION_REPORT.md   this report
```

### Modified
```text
app/application/context_builder.py      full-perception AgentContext
app/application/support_agent.py        bounded agent loop + fallback + rendering
app/application/workflow.py             prepare/run_agent/apply/resume two-phase
app/application/ingress_service.py      two-phase processing + crash resume
app/application/ticket_action_service.py honest receipt, PRIVATE_DETAIL gate,
                                          FORCE_CLOSE state guard
app/application/target_resolver.py      first-contact DM fallback to channel user
app/application/intent_router.py        LLM fallback removed (zombie path)
app/application/memory_extractor.py     unused llm param removed (zombie path)
app/application/memory_service.py       confidence-aware recall ranking
app/application/conversation_service.py / identity_service.py / repositories.py
app/infrastructure/db.py                SerializedCursor: lock covers row fetching
                                          (concurrency race fix)
app/adapters/transports.py              (json import fix)
app/main.py                             REST trust boundary, /close deprecated,
                                          case trace aggregation
seed/conversations.json · tests/conftest.py · tests/v2_fixtures.py
tests/test_context_agent.py · test_demo_v2.py · test_golden_path.py
tests/test_intent_router.py · test_memory.py · test_operator_api.py
tests/test_v2_collaboration.py · test_workflow_memory.py
README.md · docs/ARCHITECTURE.md · docs/V2_IMPLEMENTATION_REPORT.md
```

### Removed
```text
app/prompts/  (stale WIP prompt dir, superseded by app/application/prompts/)
```

---

## 11. Remaining Limitations

1. **WeCom `GROUP_INBOUND` is `PENDING_OFFICIAL_SPEC`** (unchanged): the
   official text-message callback carries no group chat id, so real WeCom
   group messages can't be routed by conversation id yet.
2. **Real LLM not exercised in CI**: automated tests use deterministic fake
   LLMs; real OpenRouter is optional integration/manual only. Prompt
   quality against a real model is unverified.
3. **Tool set is minimal by design**: no thread/card-bound actions, no
   deeper KB or memory browsing.
4. **Outbox delivery is fire-and-forget with retry**: no webhook-based
   delivery confirmation is modelled.
5. **`FAILED_RETRYABLE` has no dead-letter/backoff policy**: a permanently
   failing phase B would be retried on every duplicate delivery.
6. **No real-credentials e2e test**: outbound token exchange is simulated.
7. **Memory is per-canonical-user, single-fact text**: no expiry, no
   dedupe across repeated identical tickets.
8. **REST trust boundary is minimal by design** (no OAuth/SSO/IAM): actor
   existence + role verification only; explicitly documented as a trusted
   control-plane API.

---

## 12. Final Verdict

```text
Current Agent Level:            Level 2 (bounded stateful enterprise support agent)
V2 Collaboration:               Complete
V2.1 Agent Core:                Complete
Ready to describe as
"Stateful Enterprise Support Agent": YES
```

**Reason:** the agent now truly consumes its context (AC-A01), remembers
(AC-A04), grounds on knowledge (AC-A05), asks for clarification (AC-A06),
uses bounded read-only tools (AC-A07) and emits a schema-validated decision
that the policy layer gates (AC-A08..A10). The LLM no longer occupies the
DB write transaction (AC-A11), crash-safe processing exists (AC-A12), the
V2 closure/trust-boundary holes are closed (AC-A15..A17), tests are
hermetic (AC-A18), and the golden eval set is deterministic and green
(AC-A20). It remains bounded — no autonomous loop, no direct state
mutation, no multi-agent machinery — exactly the "Agent proposes. Policy
validates. Domain decides." target.
```
