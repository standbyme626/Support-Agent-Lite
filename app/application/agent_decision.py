"""Structured AgentDecision contract (V2.1 Agent Core).

The agent's output is a schema-validated decision, never free text and
never a business action. `validate_decision` normalizes/falls back on
every failure mode (missing fields, wrong enums, out-of-range confidence,
oversized reply, unknown refs, malformed proposals) so the pipeline can
never act on a dangerous or undefined value.

Invariant #4: a decision carries advice + an optional ActionProposal.
Proposals have NO business effect until Policy validates them and HITL
approves.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Allowed enums (mirrored in the prompt schema section; validated here).
CATEGORIES = frozenset({"account", "network", "device", "software", "billing", "hr", "general"})
PRIORITIES = frozenset({"high", "normal", "low"})
ACTIONS = frozenset({
    "dispatch_repair",
    "network_triage",
    "software_support",
    "credential_reset",
    "finance_review",
    "hr_review",
    "assign_operator",
    "ask_clarification",
    "faq_answer",
})
PROPOSAL_ACTIONS = frozenset({"ESCALATE", "FORCE_CLOSE"})

MAX_REPLY_LEN = 300
MIN_CONFIDENCE = 0.0
MAX_CONFIDENCE = 1.0

# Intent -> allowed recommended_action values (scenario contracts).
INTENT_ACTIONS: dict[str, frozenset[str]] = {
    "support": frozenset(ACTIONS - {"faq_answer"}),
    "no_answer": frozenset({"assign_operator"}),
    "faq_answer": frozenset({"faq_answer"}),
}

MISSING = object()


@dataclass(frozen=True)
class ActionProposal:
    action: str  # ESCALATE | FORCE_CLOSE (whitelist enforced by Policy)
    reason: str
    confidence: float
    ticket_id: str | None = None


@dataclass(frozen=True)
class AgentDecision:
    understanding: str
    summary: str
    category: str
    priority_suggestion: str  # high | normal | low
    recommended_action: str
    missing_information: list[str] = field(default_factory=list)
    confidence: float = 0.5
    needs_human: bool = False
    needs_approval: bool = False
    reply_draft: str = ""
    memory_refs: list[str] = field(default_factory=list)
    knowledge_refs: list[str] = field(default_factory=list)
    action_proposal: ActionProposal | None = None
    rationale: str = ""


def _as_str(value: object, default: str) -> str:
    return str(value).strip() if isinstance(value, str) and value.strip() else default


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return default


def _as_float(value: object, default: float = 0.5) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, number))


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _validate_refs(refs: list[str], allowed_ids: set[str]) -> list[str]:
    """Drop refs that do not exist in the provided context (anti-hallucination)."""
    return [ref for ref in refs if ref in allowed_ids]


def validate_decision(
    parsed: dict,
    *,
    allowed_memory_ids: set[str],
    allowed_knowledge_ids: set[str],
    context_ticket_id: str | None,
    intent: str,
) -> tuple[AgentDecision | None, str]:
    """Validate raw LLM output into an AgentDecision.

    Returns (None, reason) when core fields are unusable (caller falls
    back to deterministic rules); otherwise a sanitized decision where
    only safe normalization happened (unknown refs dropped, out-of-range
    confidence clamped, invalid proposal dropped).
    """
    if not isinstance(parsed, dict):
        return None, "not-an-object"

    understanding = _as_str(parsed.get("understanding"), "")
    summary = _as_str(parsed.get("summary"), "")
    reply = _as_str(parsed.get("reply_draft"), "")
    category = _as_str(parsed.get("category"), "")
    priority = _as_str(parsed.get("priority_suggestion"), "")
    action = _as_str(parsed.get("recommended_action"), "")
    rationale = _as_str(parsed.get("rationale"), "")

    if not summary or not reply:
        return None, "missing-summary-or-reply"
    if len(reply) > MAX_REPLY_LEN:
        return None, f"reply-too-long:{len(reply)}"
    if category not in CATEGORIES:
        return None, f"unknown-category:{category}"
    if priority not in PRIORITIES:
        return None, f"unknown-priority:{priority}"
    if action not in ACTIONS:
        return None, f"unknown-action:{action}"
    allowed = INTENT_ACTIONS.get(intent, ACTIONS)
    if action not in allowed:
        return None, f"action-not-allowed-for-intent:{intent}:{action}"

    confidence = _as_float(parsed.get("confidence"), 0.5)
    missing = _as_str_list(parsed.get("missing_information"))
    needs_human = _as_bool(parsed.get("needs_human"))
    needs_approval = _as_bool(parsed.get("needs_approval"))
    memory_refs = _validate_refs(_as_str_list(parsed.get("memory_refs")), allowed_memory_ids)
    knowledge_refs = _validate_refs(_as_str_list(parsed.get("knowledge_refs")), allowed_knowledge_ids)

    proposal: ActionProposal | None = None
    raw_proposal = parsed.get("action_proposal")
    if isinstance(raw_proposal, dict):
        prop_action = _as_str(raw_proposal.get("action"), "")
        if prop_action not in PROPOSAL_ACTIONS:
            # Not in the whitelist: drop silently (no business effect).
            proposal = None
        else:
            reason = _as_str(raw_proposal.get("reason"), "")
            if prop_action == "FORCE_CLOSE" and not reason:
                proposal = None  # force close without reason is invalid
            else:
                prop_ticket = raw_proposal.get("ticket_id")
                if prop_ticket is not None and str(prop_ticket) != context_ticket_id:
                    proposal = None  # proposal for a ticket outside context
                else:
                    proposal = ActionProposal(
                        action=prop_action,
                        reason=reason,
                        confidence=_as_float(raw_proposal.get("confidence"), confidence),
                        ticket_id=context_ticket_id,
                    )

    decision = AgentDecision(
        understanding=understanding,
        summary=summary,
        category=category,
        priority_suggestion=priority,
        recommended_action=action,
        missing_information=missing,
        confidence=confidence,
        needs_human=needs_human,
        needs_approval=needs_approval,
        reply_draft=reply,
        memory_refs=memory_refs,
        knowledge_refs=knowledge_refs,
        action_proposal=proposal,
        rationale=rationale,
    )
    return decision, ""
