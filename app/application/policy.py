"""Policy Validator (V2.1): the gate between AgentProposal and HITL.

`Agent proposes. Policy validates. Domain decides.`

The validator checks actor/ticket state/allowed action/reason/risk and
decides whether a proposal may enter the approval pipeline. It NEVER
executes anything — it returns a verdict the workflow acts on.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.application.agent_decision import ActionProposal, PROPOSAL_ACTIONS
from app.application.context_builder import AgentContext
from app.infrastructure.repositories import TicketStore

# Minimum proposal confidence to be admitted into the approval pipeline.
PROPOSAL_MIN_CONFIDENCE = 0.6


@dataclass(frozen=True)
class ProposalVerdict:
    allowed: bool
    reason: str


class PolicyValidator:
    """Validates AgentDecision.action_proposal against policy rules."""

    def __init__(self, store: TicketStore) -> None:
        self._store = store

    def validate_proposal(self, proposal: ActionProposal | None, context: AgentContext) -> ProposalVerdict:
        if proposal is None:
            return ProposalVerdict(False, "no-proposal")
        if proposal.action not in PROPOSAL_ACTIONS:
            return ProposalVerdict(False, f"action-not-whitelisted:{proposal.action}")
        if proposal.confidence < PROPOSAL_MIN_CONFIDENCE:
            return ProposalVerdict(False, f"confidence-too-low:{round(proposal.confidence, 2)}")
        ticket = self._store.get(proposal.ticket_id) if proposal.ticket_id else context.ticket
        if ticket is None:
            return ProposalVerdict(False, "ticket-not-found")
        if ticket.status.value == "CLOSED":
            return ProposalVerdict(False, "ticket-closed")
        if proposal.action == "FORCE_CLOSE" and not proposal.reason.strip():
            return ProposalVerdict(False, "force-close-requires-reason")
        if proposal.action == "FORCE_CLOSE" and ticket.status.value not in ("IN_PROGRESS", "RESOLVED"):
            # The state machine only allows IN_PROGRESS/RESOLVED -> CLOSED;
            # an OPEN ticket cannot be force-closed by any path.
            return ProposalVerdict(False, f"force-close-not-allowed-from-{ticket.status.value}")
        if proposal.ticket_id and proposal.ticket_id != ticket.id:
            return ProposalVerdict(False, "proposal-ticket-mismatch")
        return ProposalVerdict(True, f"allowed:{proposal.action}")
