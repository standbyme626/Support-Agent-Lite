"""ApprovalService: independent approval state machine (invariant #6).

`escalate` creates a PENDING approval for a high-risk action but NEVER
touches ticket status — a ticket remains valid while the approval is
pending (AC-08). Decisions move PENDING -> APPROVED | REJECTED.
"""
from __future__ import annotations

from uuid import uuid4

from app.domain.approval import Approval, ApprovalStatus, InvalidApprovalDecision
from app.infrastructure.repositories import ApprovalRepository, TicketStore


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:10]}"


class ApprovalService:
    def __init__(self, store: TicketStore, approvals: ApprovalRepository) -> None:
        self._store = store
        self._approvals = approvals

    def escalate(
        self,
        ticket_id: str,
        *,
        action: str = "escalate",
        requested_by: str = "operator",
        reason: str | None = None,
    ) -> Approval:
        """Request approval for a high-risk action. Ticket status unchanged."""
        if self._store.get(ticket_id) is None:
            raise KeyError(f"ticket not found: {ticket_id}")
        approval = Approval(
            id=new_id("apr_"),
            ticket_id=ticket_id,
            action=action,
            requested_by=requested_by,
            reason=reason,
        )
        return self._approvals.create(approval)

    def approve(self, approval_id: str, *, decided_by: str = "approver") -> Approval:
        return self._approvals.decide(
            approval_id, ApprovalStatus.APPROVED, decided_by=decided_by
        )

    def reject(
        self,
        approval_id: str,
        *,
        decided_by: str = "approver",
        reason: str | None = None,
    ) -> Approval:
        return self._approvals.decide(
            approval_id, ApprovalStatus.REJECTED, decided_by=decided_by, reason=reason
        )

    def list(self, status: ApprovalStatus | None = None) -> list[Approval]:
        return self._approvals.list_by_status(status)

    def get(self, approval_id: str) -> Approval | None:
        return self._approvals.get(approval_id)


__all__ = ["ApprovalService", "Approval", "ApprovalStatus", "InvalidApprovalDecision"]
