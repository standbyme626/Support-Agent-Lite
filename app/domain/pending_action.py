"""PendingAction: an action awaiting approval (V2 HITL execution chain).

Approval is a decision; PendingAction is the executable unit bound to
that decision. execution_status guards exactly-once execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PendingActionStatus(str, Enum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class ApprovableAction(str, Enum):
    """Whitelist of actions that can go through approval (never free strings)."""

    ESCALATE = "ESCALATE"
    FORCE_CLOSE = "FORCE_CLOSE"


@dataclass(slots=True)
class PendingAction:
    id: str
    ticket_id: str
    action_type: ApprovableAction
    payload: dict | None
    requested_by: str
    approval_id: str | None = None
    execution_status: PendingActionStatus = PendingActionStatus.PENDING
    executed_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)
