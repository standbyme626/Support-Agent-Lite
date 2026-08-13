"""Phase 2 tests: IdentityResolver + TicketResolver + SessionService.

Covers acceptance seeds:
- wecom/zhangsan + feishu/ou_001 -> same canonical user
- explicit ticket -> session ticket -> active tickets -> clarification
"""
import pytest

from app.application.identity_service import IdentityResolver
from app.application.session_service import SessionService
from app.application.ticket_service import ResolutionKind, TicketResolver, TicketService
from app.domain.identity import ChannelIdentity, User
from app.domain.ticket import TicketStatus
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
def ctx(conn):
    users = UserRepository(conn)
    identities = ChannelIdentityRepository(conn)
    sessions = SessionRepository(conn)
    store = TicketStore(conn)
    return {
        "conn": conn,
        "users": users,
        "identities": identities,
        "sessions": sessions,
        "store": store,
        "identity": IdentityResolver(users, identities),
        "session_service": SessionService(sessions),
        "ticket_service": TicketService(store),
        "resolver": TicketResolver(TicketService(store), store),
    }


def seed_user(ctx, channel: str, channel_user_id: str, display_name: str) -> User:
    return ctx["identity"].resolve(channel, channel_user_id, display_name)


# --- IdentityResolver ---


def test_new_channel_identity_creates_user(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    assert user.id.startswith("user_")
    identity = ctx["identities"].find("wecom", "zhangsan")
    assert identity is not None
    assert identity.user_id == user.id


def test_same_channel_identity_resolves_same_user(ctx) -> None:
    u1 = seed_user(ctx, "wecom", "zhangsan", "张三")
    u2 = ctx["identity"].resolve("wecom", "zhangsan")
    assert u1.id == u2.id


def test_cross_channel_same_person_same_user(ctx) -> None:
    """AC-05 seed: wecom/zhangsan + feishu/ou_001 -> same canonical user."""
    wecom_user = seed_user(ctx, "wecom", "zhangsan", "张三")
    feishu_user = ctx["identity"].bind("feishu", "ou_001", wecom_user.id)
    assert wecom_user.id == feishu_user.id


def test_channel_identity_is_not_user_id(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    assert user.id != "zhangsan"


# --- SessionService ---


def test_session_find_or_create_belongs_to_user(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    s1 = ctx["session_service"].find_or_create(user.id, "wecom", "conv_1")
    s2 = ctx["session_service"].find_or_create(user.id, "wecom", "conv_1")
    assert s1.id == s2.id
    assert s1.user_id == user.id


# --- TicketService ---


def test_create_ticket_via_service(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    ticket = ctx["ticket_service"].create(user.id, "A3 空调坏了", "A3 空调不制冷")
    assert ticket.id == "T0001"
    assert ticket.status == TicketStatus.OPEN
    assert len(ctx["store"].events(ticket.id)) == 1


def test_create_second_ticket_increments_id(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    t1 = ctx["ticket_service"].create(user.id, "a", "d")
    t2 = ctx["ticket_service"].create(user.id, "b", "d")
    assert (t1.id, t2.id) == ("T0001", "T0002")


def test_claim_resolve_close(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    ticket = ctx["ticket_service"].create(user.id, "a", "d")
    assert ctx["ticket_service"].claim(ticket.id).status == TicketStatus.IN_PROGRESS
    assert ctx["ticket_service"].resolve(ticket.id).status == TicketStatus.RESOLVED
    assert ctx["ticket_service"].close(ticket.id).status == TicketStatus.CLOSED


# --- TicketResolver ---


def test_resolution_create_new_when_no_ticket(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    resolution = ctx["resolver"].resolve("A3 空调坏了", user.id)
    assert resolution.kind == ResolutionKind.CREATE_NEW


def test_resolution_only_active_ticket(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    ctx["ticket_service"].create(user.id, "A3 空调坏了", "不制冷")
    resolution = ctx["resolver"].resolve("处理了吗?", user.id)
    assert resolution.kind == ResolutionKind.ONLY_ACTIVE
    assert resolution.ticket is not None


def test_resolution_clarify_when_multiple_active(ctx) -> None:
    """AC-06: two active tickets -> clarification, never random pick."""
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    ctx["ticket_service"].create(user.id, "A3 空调坏了", "不制冷")
    ctx["ticket_service"].create(user.id, "VPN 连不上", "报错 619")
    resolution = ctx["resolver"].resolve("处理了吗?", user.id)
    assert resolution.kind == ResolutionKind.CLARIFY
    assert {t.id for t in resolution.candidates} == {"T0001", "T0002"}
    assert resolution.ticket is None


def test_resolution_explicit_ticket_wins(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    t1 = ctx["ticket_service"].create(user.id, "A3 空调坏了", "不制冷")
    ctx["ticket_service"].create(user.id, "VPN 连不上", "报错 619")
    resolution = ctx["resolver"].resolve("T0001 那个处理了吗", user.id)
    assert resolution.kind == ResolutionKind.EXPLICIT
    assert resolution.ticket is not None
    assert resolution.ticket.id == t1.id


def test_resolution_session_ticket_when_multiple_active(ctx) -> None:
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    t1 = ctx["ticket_service"].create(user.id, "A3 空调坏了", "不制冷")
    ctx["ticket_service"].create(user.id, "VPN 连不上", "报错 619")
    resolution = ctx["resolver"].resolve("处理了吗?", user.id, session_ticket_id=t1.id)
    assert resolution.kind == ResolutionKind.SESSION
    assert resolution.ticket is not None
    assert resolution.ticket.id == t1.id


def test_resolution_foreign_ticket_ignored(ctx) -> None:
    user_a = seed_user(ctx, "wecom", "zhangsan", "张三")
    user_b = seed_user(ctx, "feishu", "ou_002", "李四")
    foreign = ctx["ticket_service"].create(user_b.id, "别人的单", "d")
    resolution = ctx["resolver"].resolve(f"{foreign.id} 怎么样了?", user_a.id)
    assert resolution.kind in (ResolutionKind.CREATE_NEW, ResolutionKind.ONLY_ACTIVE, ResolutionKind.CLARIFY)


def test_cross_channel_continuation_no_duplicate(ctx) -> None:
    """AC-05 end-to-end: feishu message continues T0001, never creates T0002."""
    user = seed_user(ctx, "wecom", "zhangsan", "张三")
    ticket = ctx["ticket_service"].create(user.id, "A3 空调坏了", "A3 空调不制冷")

    feishu_user = ctx["identity"].bind("feishu", "ou_001", user.id)
    assert feishu_user.id == user.id

    session = ctx["session_service"].find_or_create(user.id, "feishu", "conv_feishu_1")
    resolution = ctx["resolver"].resolve("昨天空调那个事情怎么样了?", user.id, session_ticket_id=session.id)

    # No session ticket yet and single active -> same ticket continued
    assert resolution.kind == ResolutionKind.ONLY_ACTIVE
    assert resolution.ticket is not None
    assert resolution.ticket.id == ticket.id
    assert [t.id for t in ctx["store"].list_by_user(user.id)] == ["T0001"]
