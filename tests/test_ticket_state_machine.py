"""Phase 1 tests: ticket state machine (pure domain, no DB)."""
import pytest

from app.domain.ticket import (
    InvalidStateTransition,
    Ticket,
    TicketStatus,
    validate_transition,
)


def test_valid_transitions() -> None:
    for current, target in [
        (TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
        (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
        (TicketStatus.RESOLVED, TicketStatus.CLOSED),
    ]:
        validate_transition(current, target)  # should not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TicketStatus.OPEN, TicketStatus.RESOLVED),
        (TicketStatus.OPEN, TicketStatus.CLOSED),
        (TicketStatus.IN_PROGRESS, TicketStatus.CLOSED),
        (TicketStatus.RESOLVED, TicketStatus.IN_PROGRESS),
        (TicketStatus.CLOSED, TicketStatus.OPEN),
        (TicketStatus.CLOSED, TicketStatus.CLOSED),
        (TicketStatus.IN_PROGRESS, TicketStatus.IN_PROGRESS),
    ],
)
def test_invalid_transitions_raise(current: TicketStatus, target: TicketStatus) -> None:
    with pytest.raises(InvalidStateTransition):
        validate_transition(current, target)


def test_new_ticket_starts_open() -> None:
    ticket = Ticket(id="t1", user_id="user_001", title="A3 空调坏了", description="A3 空调不制冷")
    assert ticket.status == TicketStatus.OPEN
