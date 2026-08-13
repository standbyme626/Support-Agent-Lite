"""Ticket and TicketEvent domain entities with a strict state machine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TicketStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class TicketEventType(str, Enum):
    CREATED = "created"
    STARTED = "started"
    RESOLVED = "resolved"
    CLOSED = "closed"


class InvalidStateTransition(ValueError):
    """Raised when a status transition is not allowed by the state machine."""


# Strict transition table: status -> allowed next statuses.
ALLOWED_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.OPEN: frozenset({TicketStatus.IN_PROGRESS}),
    TicketStatus.IN_PROGRESS: frozenset({TicketStatus.RESOLVED}),
    TicketStatus.RESOLVED: frozenset({TicketStatus.CLOSED}),
    TicketStatus.CLOSED: frozenset(),
}

# Which event type is recorded for a given transition.
EVENT_FOR_TRANSITION: dict[tuple[TicketStatus, TicketStatus], TicketEventType] = {
    (TicketStatus.OPEN, TicketStatus.IN_PROGRESS): TicketEventType.STARTED,
    (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED): TicketEventType.RESOLVED,
    (TicketStatus.RESOLVED, TicketStatus.CLOSED): TicketEventType.CLOSED,
}


def validate_transition(current: TicketStatus, target: TicketStatus) -> None:
    """Raise InvalidStateTransition if `current -> target` is not allowed."""
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransition(f"invalid transition: {current.value} -> {target.value}")


@dataclass(slots=True)
class Ticket:
    """A support ticket. Belongs to a canonical User."""

    id: str
    user_id: str
    title: str
    description: str
    status: TicketStatus = TicketStatus.OPEN
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass(slots=True)
class TicketEvent:
    """Audit event for a ticket. Must be committed with the ticket state change."""

    id: str
    ticket_id: str
    event_type: TicketEventType
    payload: dict | None = None
    created_at: datetime = field(default_factory=_now)
