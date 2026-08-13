"""Approval: independent state machine for high-risk ticket actions.

Invariant #6: Approval is independent of Ticket. A PENDING approval does
NOT mutate ticket status — a ticket stays valid while its escalation is
awaiting a decision (AC-08).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class InvalidApprovalDecision(ValueError):
    """Raised when a decision is attempted on a non-PENDING approval."""


@dataclass(slots=True)
class Approval:
    """High-risk action request, decided independently of the ticket."""

    id: str
    ticket_id: str
    action: str
    requested_by: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)


def validate_decision(current: ApprovalStatus) -> None:
    """Only PENDING approvals can be approved or rejected."""
    if current != ApprovalStatus.PENDING:
        raise InvalidApprovalDecision(f"approval already decided: {current.value}")
