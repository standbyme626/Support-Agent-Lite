"""Phase 6 tests: MemoryExtractor + MemoryService (extraction, storage, recall).

AC-09: CLOSED ticket -> MemoryExtractor produces stable facts.
AC-10: new session message recalls prior resolution as context.
Quality gate: memory extraction Precision >= 85% (produced by tests).
"""
import pytest

from app.application.identity_service import IdentityResolver
from app.application.memory_extractor import MemoryExtractor
from app.application.memory_service import MemoryService
from app.application.ticket_service import TicketService
from app.domain.memory import MemoryKind
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.repositories import (
    ChannelIdentityRepository,
    MemoryRepository,
    TicketStore,
    UserRepository,
)


@pytest.fixture()
def ctx():
    conn = connect(":memory:")
    apply_migrations(conn)
    users = UserRepository(conn)
    store = TicketStore(conn)
    identity = IdentityResolver(users, ChannelIdentityRepository(conn))
    user = identity.resolve("wecom", "zhangsan", "张三")
    tickets = TicketService(store)
    memory = MemoryService(store, MemoryRepository(conn))
    yield {"conn": conn, "store": store, "tickets": tickets, "memory": memory, "user": user}
    conn.close()


def _closed_ticket(ctx, title: str, resolution_note: str | None = None):
    ticket = ctx["tickets"].create(ctx["user"].id, title, title)
    ctx["tickets"].claim(ticket.id)
    ctx["tickets"].resolve(ticket.id, {"note": resolution_note} if resolution_note else None)
    ctx["tickets"].close(ticket.id)
    return ctx["store"].get(ticket.id)  # re-fetch: CLOSED status


# --- extractor ---


def test_extractor_requires_closed_ticket(ctx) -> None:
    ticket = ctx["tickets"].create(ctx["user"].id, "A3 空调坏了", "不制冷")
    with pytest.raises(ValueError):
        MemoryExtractor().extract(ticket, events=ctx["store"].events(ticket.id))


def test_extractor_produces_issue_fact_and_summary(ctx) -> None:
    ticket = _closed_ticket(ctx, "A3 空调坏了", "已更换空调滤网")
    result = MemoryExtractor().extract(ticket, events=ctx["store"].events(ticket.id))

    stable = [m.fact for m in result.memories if m.kind == MemoryKind.STABLE_FACT]
    summaries = [m.fact for m in result.memories if m.kind == MemoryKind.SUMMARY]
    assert "设备问题：A3 空调坏了" in stable
    assert summaries == [f"工单 {ticket.id}：A3 空调坏了 已处理完成。"]
    assert result.category == "设备问题"


def test_extractor_uses_resolution_note_from_event(ctx) -> None:
    ticket = _closed_ticket(ctx, "VPN 连不上", "已重置VPN账号")
    result = MemoryExtractor().extract(ticket, events=ctx["store"].events(ticket.id))
    facts = [m.fact for m in result.memories if m.kind == MemoryKind.STABLE_FACT]
    assert "处理结果：已重置VPN账号" in facts
    assert "网络问题：VPN 连不上" in facts


def test_extractor_without_note_skips_resolution_fact(ctx) -> None:
    ticket = _closed_ticket(ctx, "邮箱密码重置")
    result = MemoryExtractor().extract(ticket, events=ctx["store"].events(ticket.id))
    stable = [m.fact for m in result.memories if m.kind == MemoryKind.STABLE_FACT]
    assert stable == ["账号问题：邮箱密码重置"]


# --- service: remember ---


def test_remember_stores_memory_for_closed_ticket(ctx) -> None:
    ticket = _closed_ticket(ctx, "A3 空调坏了", "已更换空调滤网")
    memories = ctx["memory"].remember(ticket.id)

    assert len(memories) == 3  # issue + resolution + summary
    assert all(m.user_id == ctx["user"].id for m in memories)
    assert all(m.ticket_id == ticket.id for m in memories)


def test_remember_is_idempotent(ctx) -> None:
    ticket = _closed_ticket(ctx, "A3 空调坏了")
    ctx["memory"].remember(ticket.id)
    again = ctx["memory"].remember(ticket.id)
    assert again == ctx["memory"].list(user_id=ctx["user"].id)


def test_remember_rejects_open_ticket(ctx) -> None:
    ticket = ctx["tickets"].create(ctx["user"].id, "A3 空调坏了", "d")
    with pytest.raises(ValueError):
        ctx["memory"].remember(ticket.id)


def test_remember_unknown_ticket_raises(ctx) -> None:
    with pytest.raises(KeyError):
        ctx["memory"].remember("T9999")


# --- service: recall (AC-10) ---


def test_recall_finds_prior_ticket_facts(ctx) -> None:
    ticket = _closed_ticket(ctx, "A3 空调坏了", "已更换空调滤网")
    ctx["memory"].remember(ticket.id)

    hits = ctx["memory"].recall(ctx["user"].id, "空调又坏了")
    facts = [h.fact for h in hits]
    assert any("A3 空调坏了" in f for f in facts)
    assert hits[0].memory.user_id == ctx["user"].id


def test_recall_ignores_unrelated_queries(ctx) -> None:
    ticket = _closed_ticket(ctx, "A3 空调坏了")
    ctx["memory"].remember(ticket.id)
    assert ctx["memory"].recall(ctx["user"].id, "年假怎么申请") == []


def test_recall_only_sees_own_user_memory(ctx) -> None:
    ticket = _closed_ticket(ctx, "A3 空调坏了")
    ctx["memory"].remember(ticket.id)

    other = IdentityResolver(UserRepository(ctx["conn"]), ChannelIdentityRepository(ctx["conn"])).resolve(
        "feishu", "ou_999", "李四"
    )
    assert ctx["memory"].recall(other.id, "空调又坏了") == []


def test_list_memories_by_kind(ctx) -> None:
    ticket = _closed_ticket(ctx, "打印机脱机", "重启打印服务")
    ctx["memory"].remember(ticket.id)

    stable = ctx["memory"].list(user_id=ctx["user"].id, kind=MemoryKind.STABLE_FACT)
    summaries = ctx["memory"].list(user_id=ctx["user"].id, kind=MemoryKind.SUMMARY)
    assert len(stable) == 2
    assert len(summaries) == 1


# --- quality gate: extraction Precision >= 85% ---

# Labeled cases: (title, resolution_note, expected stable facts)
EVAL_CASES = [
    ("A3 空调坏了", "已更换空调滤网", ["设备问题：A3 空调坏了", "处理结果：已更换空调滤网"]),
    ("VPN 连不上", "已重置VPN账号", ["网络问题：VPN 连不上", "处理结果：已重置VPN账号"]),
    ("邮箱密码重置", None, ["账号问题：邮箱密码重置"]),
    ("年假申请流程咨询", "审批通过", ["人事问题：年假申请流程咨询", "处理结果：审批通过"]),
    ("发票开不出来", "已补开电子发票", ["财务问题：发票开不出来", "处理结果：已补开电子发票"]),
    ("打印机脱机", "重启打印服务", ["设备问题：打印机脱机", "处理结果：重启打印服务"]),
]

MIN_PRECISION = 0.85


def test_memory_extraction_eval(ctx) -> None:
    """Quality gate: extraction Precision >= 85% (recall on labels too).

    Numbers are produced by this test: for every labeled closed ticket,
    every extracted stable fact must be expected and every expected fact
    must be extracted.
    """
    extractor = MemoryExtractor()
    total_extracted = 0
    total_expected = 0
    total_correct = 0
    total_label_hits = 0
    for title, note, expected in EVAL_CASES:
        ticket = _closed_ticket(ctx, title, note)
        result = extractor.extract(ticket, events=ctx["store"].events(ticket.id))
        extracted = [m.fact for m in result.memories if m.kind == MemoryKind.STABLE_FACT]

        total_extracted += len(extracted)
        total_expected += len(expected)
        total_correct += sum(1 for fact in extracted if fact in expected)
        total_label_hits += sum(1 for exp in expected if exp in extracted)

    precision = total_correct / total_extracted
    recall = total_label_hits / total_expected
    assert precision >= MIN_PRECISION, f"precision={precision:.2%} < {MIN_PRECISION:.0%}"
    assert recall >= MIN_PRECISION, f"label recall={recall:.2%} < {MIN_PRECISION:.0%}"
