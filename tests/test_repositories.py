"""Phase 1 tests: repositories, uniqueness constraints, transactional writes."""
import pytest

from app.domain.identity import ChannelIdentity, Session, User
from app.domain.ticket import (
    InvalidStateTransition,
    Ticket,
    TicketEventType,
    TicketStatus,
)
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.repositories import (
    ChannelIdentityRepository,
    SessionRepository,
    TicketStore,
    UserRepository,
)


@pytest.fixture()
def conn():
    c = connect(":memory:")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def users(conn):
    repo = UserRepository(conn)
    u = repo.create(User(id="user_001", display_name="zhangsan"))
    return u


def test_channel_identity_unique_constraint(conn, users) -> None:
    repo = ChannelIdentityRepository(conn)
    repo.create(ChannelIdentity(id="ci1", user_id="user_001", channel="wecom", channel_user_id="zhangsan"))
    with pytest.raises(Exception):  # UNIQUE(channel, channel_user_id)
        repo.create(ChannelIdentity(id="ci2", user_id="user_001", channel="wecom", channel_user_id="zhangsan"))


def test_two_channels_for_same_user_ok(conn, users) -> None:
    repo = ChannelIdentityRepository(conn)
    repo.create(ChannelIdentity(id="ci1", user_id="user_001", channel="wecom", channel_user_id="zhangsan"))
    repo.create(ChannelIdentity(id="ci2", user_id="user_001", channel="feishu", channel_user_id="ou_001"))
    assert len(repo.list_by_user("user_001")) == 2


def test_session_belongs_to_user(conn, users) -> None:
    repo = SessionRepository(conn)
    s = repo.create(Session(id="s1", user_id="user_001", channel="wecom", channel_conversation_id="conv_1"))
    assert repo.get("s1") == s
    assert repo.list_by_user("user_001") == [s]


def test_ticket_creation_creates_event(conn, users) -> None:
    store = TicketStore(conn)
    t = Ticket(id="T1001", user_id="user_001", title="A3 空调坏了", description="A3 空调不制冷")
    store.create(t)

    events = store.events("T1001")
    assert len(events) == 1
    assert events[0].event_type == TicketEventType.CREATED
    assert store.get("T1001").status == TicketStatus.OPEN  # type: ignore[union-attr]


def test_transition_writes_event_atomically(conn, users) -> None:
    store = TicketStore(conn)
    store.create(Ticket(id="T1001", user_id="user_001", title="t", description="d"))

    store.transition("T1001", TicketStatus.IN_PROGRESS, payload={"actor": "operator_1"})

    ticket = store.get("T1001")
    assert ticket.status == TicketStatus.IN_PROGRESS  # type: ignore[union-attr]
    events = store.events("T1001")
    assert [e.event_type for e in events] == [TicketEventType.CREATED, TicketEventType.CLAIMED]
    assert events[1].payload == {"actor": "operator_1"}


def test_invalid_transition_rejected_and_no_event(conn, users) -> None:
    store = TicketStore(conn)
    store.create(Ticket(id="T1001", user_id="user_001", title="t", description="d"))

    with pytest.raises(InvalidStateTransition):
        store.transition("T1001", TicketStatus.CLOSED)

    assert store.get("T1001").status == TicketStatus.OPEN  # type: ignore[union-attr]
    assert len(store.events("T1001")) == 1  # only created


def test_full_lifecycle(conn, users) -> None:
    store = TicketStore(conn)
    store.create(Ticket(id="T1001", user_id="user_001", title="A3 空调坏了", description="A3 空调不制冷"))
    store.transition("T1001", TicketStatus.IN_PROGRESS)
    store.transition("T1001", TicketStatus.RESOLVED)
    store.transition("T1001", TicketStatus.CLOSED)

    assert store.get("T1001").status == TicketStatus.CLOSED  # type: ignore[union-attr]
    types = [e.event_type for e in store.events("T1001")]
    assert types == [
        TicketEventType.CREATED,
        TicketEventType.CLAIMED,
        TicketEventType.RESOLVED,
        TicketEventType.CLOSED,
    ]


def test_tickets_scoped_by_user(conn, users) -> None:
    store = TicketStore(conn)
    UserRepository(conn).create(User(id="user_002", display_name="lisi"))
    store.create(Ticket(id="T1001", user_id="user_001", title="a", description="d"))
    store.create(Ticket(id="T1002", user_id="user_002", title="b", description="d"))

    assert [t.id for t in store.list_by_user("user_001")] == ["T1001"]
