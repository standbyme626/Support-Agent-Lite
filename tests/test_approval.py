"""Phase 5 tests: Approval domain state machine + ApprovalRepository."""
import pytest

from app.application.approval_service import ApprovalService
from app.application.identity_service import IdentityResolver
from app.application.ticket_service import TicketService
from app.domain.approval import (
    Approval,
    ApprovalStatus,
    InvalidApprovalDecision,
    validate_decision,
)
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.repositories import (
    ApprovalRepository,
    ChannelIdentityRepository,
    TicketStore,
    UserRepository,
)


# --- pure domain ---


def test_new_approval_is_pending() -> None:
    approval = Approval(id="apr_1", ticket_id="T0001", action="escalate", requested_by="operator")
    assert approval.status == ApprovalStatus.PENDING
    assert approval.decided_at is None


@pytest.mark.parametrize("target", [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED])
def test_pending_can_be_decided(target: ApprovalStatus) -> None:
    validate_decision(ApprovalStatus.PENDING)  # should not raise


@pytest.mark.parametrize("current", [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED])
def test_decided_cannot_be_decided_again(current: ApprovalStatus) -> None:
    with pytest.raises(InvalidApprovalDecision):
        validate_decision(current)


# --- repository + service ---


@pytest.fixture()
def ctx():
    conn = connect(":memory:")
    apply_migrations(conn)
    users = UserRepository(conn)
    store = TicketStore(conn)
    identity = IdentityResolver(users, ChannelIdentityRepository(conn))
    user = identity.resolve("wecom", "zhangsan", "张三")
    tickets = TicketService(store)
    ticket = tickets.create(user.id, "A3 空调坏了", "不制冷")
    approvals = ApprovalService(store, ApprovalRepository(conn))
    yield {"conn": conn, "store": store, "tickets": tickets, "ticket": ticket, "approvals": approvals}
    conn.close()


def test_escalate_creates_pending_and_ticket_unchanged(ctx) -> None:
    """AC-08: escalate -> Approval PENDING, ticket remains valid."""
    ticket_before = ctx["store"].get(ctx["ticket"].id)
    approval = ctx["approvals"].escalate(ctx["ticket"].id, reason="需要上级介入")

    assert approval.ticket_id == ctx["ticket"].id
    assert approval.status == ApprovalStatus.PENDING
    assert approval.action == "escalate"
    assert approval.reason == "需要上级介入"

    ticket_after = ctx["store"].get(ctx["ticket"].id)
    assert ticket_after is not None
    assert ticket_after.status == ticket_before.status  # status NOT mutated
    assert [e.event_type.value for e in ctx["store"].events(ctx["ticket"].id)] == ["created"]  # no new events


def test_escalate_unknown_ticket_raises(ctx) -> None:
    with pytest.raises(KeyError):
        ctx["approvals"].escalate("T9999")


def test_approve_moves_to_approved(ctx) -> None:
    approval = ctx["approvals"].escalate(ctx["ticket"].id)
    decided = ctx["approvals"].approve(approval.id, decided_by="manager")

    assert decided.status == ApprovalStatus.APPROVED
    assert decided.decided_by == "manager"
    assert decided.decided_at is not None
    assert ctx["store"].get(ctx["ticket"].id).status.value == "OPEN"  # still valid


def test_reject_moves_to_rejected(ctx) -> None:
    approval = ctx["approvals"].escalate(ctx["ticket"].id)
    decided = ctx["approvals"].reject(approval.id, decided_by="manager", reason="理由不充分")

    assert decided.status == ApprovalStatus.REJECTED
    assert decided.reason == "理由不充分"


def test_double_decision_fails(ctx) -> None:
    approval = ctx["approvals"].escalate(ctx["ticket"].id)
    ctx["approvals"].approve(approval.id)
    with pytest.raises(InvalidApprovalDecision):
        ctx["approvals"].approve(approval.id)


def test_decide_unknown_approval_raises(ctx) -> None:
    with pytest.raises(KeyError):
        ctx["approvals"].approve("apr_missing")


def test_list_approvals_with_status_filter(ctx) -> None:
    a1 = ctx["approvals"].escalate(ctx["ticket"].id, action="escalate")
    a2 = ctx["approvals"].escalate(ctx["ticket"].id, action="emergency")
    ctx["approvals"].approve(a1.id)

    all_approvals = ctx["approvals"].list()
    pending = ctx["approvals"].list(status=ApprovalStatus.PENDING)
    approved = ctx["approvals"].list(status=ApprovalStatus.APPROVED)

    assert {a.id for a in all_approvals} == {a1.id, a2.id}
    assert [a.id for a in pending] == [a2.id]
    assert [a.id for a in approved] == [a1.id]


def test_approval_list_is_independent_of_ticket_list(ctx) -> None:
    """Invariant #6: approvals are not tickets and vice versa."""
    ctx["approvals"].escalate(ctx["ticket"].id)
    assert len(ctx["tickets"].active_tickets(ctx["store"].get(ctx["ticket"].id).user_id)) == 1
    assert len(ctx["approvals"].list()) == 1
