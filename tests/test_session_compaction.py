"""#6 记忆系统增强:会话级滚动摘要(pi compaction 同款)+ 记忆来源标记。

验收(升级计划 §#6):长对话压缩后关键事实仍在上下文。
pi 语义对齐:切点落在 user 消息(不拆轮次);first_kept_message_id 之后
原文保留;摘要迭代更新;无 LLM 时确定性抽取(离线可跑)。
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.application.context_builder import ContextBuilder
from app.application.identity_service import IdentityResolver
from app.application.memory_service import MemoryService
from app.application.session_compactor import KEEP_RECENT, SessionCompactor
from app.application.ticket_service import TicketService
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session
from app.domain.memory import Memory, MemoryKind
from app.domain.message import Message
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.repositories import (
    ChannelIdentityRepository,
    MemoryRepository,
    MessageRepository,
    SessionCompactionRepository,
    SessionRepository,
    TicketStore,
    UserRepository,
)

_EPOCH = datetime(2026, 8, 26, tzinfo=timezone.utc)


@pytest.fixture()
def ctx():
    conn = connect(":memory:")
    apply_migrations(conn)
    users = UserRepository(conn)
    user = IdentityResolver(users, ChannelIdentityRepository(conn)).resolve("wecom", "zhangsan", "张三")
    sessions = SessionRepository(conn)
    session = Session(
        id="s_test", user_id=user.id, channel="wecom", channel_conversation_id="c1"
    )
    sessions.create(session)
    messages = MessageRepository(conn)
    compactions = SessionCompactionRepository(conn)
    yield {
        "conn": conn,
        "messages": messages,
        "compactions": compactions,
        "compactor": SessionCompactor(messages, compactions),
        "session_id": session.id,
        "user": user,
        "session": session,
    }
    conn.close()


def _msg(ctx, role: str, text: str, clock=[0]) -> None:
    """Persist one message with a strictly increasing timestamp."""
    clock[0] += 1
    ctx["messages"].add(
        Message(
            id=uuid4().hex[:12],
            session_id=ctx["session_id"],
            user_id=ctx["user"].id,
            role=role,
            text=text,
            created_at=_EPOCH + timedelta(seconds=clock[0]),
        )
    )


def _converse(ctx, rounds: int) -> None:
    for i in range(rounds):
        _msg(ctx, "user", f"我的工牌读不了门禁，第{i}次反馈")
        _msg(ctx, "assistant", f"已记录您的问题#{i}，正在处理")


# --- 触发与切点(pi 语义) ---


def test_no_compaction_below_threshold(ctx) -> None:
    _converse(ctx, rounds=5)  # 10 条 < 阈值 12
    assert ctx["compactor"].maybe_compact(ctx["session_id"]) is None
    assert ctx["compactions"].latest_for(ctx["session_id"]) is None


def test_compaction_cuts_on_user_boundary_and_keeps_recent_tail(ctx) -> None:
    _converse(ctx, rounds=8)  # 16 条 > 12
    entry = ctx["compactor"].maybe_compact(ctx["session_id"])

    assert entry is not None
    all_msgs = ctx["messages"].list_after(ctx["session_id"])
    first_kept_idx = next(i for i, m in enumerate(all_msgs) if m.id == entry.first_kept_message_id)
    assert all_msgs[first_kept_idx].role == "user"  # 切点不拆轮次(必要时多留一条)
    assert len(all_msgs) - first_kept_idx >= KEEP_RECENT
    assert entry.messages_compacted == first_kept_idx
    assert entry.chars_before > 0
    assert entry.summarizer == "deterministic"


def test_regression_key_fact_survives_compaction(ctx) -> None:
    """核心验收:压缩后,被压缩区域的关键事实仍能进入 Agent 上下文。"""
    _msg(ctx, "user", "我的门禁卡在 B2 层刷不开，昨天还好好的")
    _msg(ctx, "assistant", "收到，请先到一楼前台重新激活卡片")
    _converse(ctx, rounds=7)  # 总计 16 条
    entry = ctx["compactor"].maybe_compact(ctx["session_id"])
    assert entry is not None

    builder = ContextBuilder(ctx["messages"], compactions=ctx["compactions"])
    envelope = InboundEnvelope(
        channel="wecom",
        message_id="m1",
        channel_user_id="zhangsan",
        conversation_id="c1",
        text="还是不行",
    )
    users = UserRepository(ctx["conn"])
    identities = ChannelIdentityRepository(ctx["conn"])
    user = IdentityResolver(users, identities).resolve("wecom", "zhangsan", "张三")

    context = builder.build(envelope, user, ctx["session"], ticket=None)

    assert context.history_summary != ""
    assert "门禁卡" in context.history_summary  # 关键事实由摘要承载,未丢失
    recent_ids = {m.id for m in context.recent_messages}
    all_ids = {m.id for m in ctx["messages"].list_after(ctx["session_id"])}
    compacted_ids = all_ids - recent_ids - set()
    assert len(compacted_ids) >= 10  # 大部分历史已被摘要替换
    assert len(context.recent_messages) <= KEEP_RECENT


def test_iterative_compaction_counts_only_uncompacted_tail(ctx) -> None:
    _converse(ctx, rounds=8)  # 16 条 → 第一次压缩
    first = ctx["compactor"].maybe_compact(ctx["session_id"])
    assert first is not None

    _converse(ctx, rounds=2)  # 尾部 7+4=11 条 ≤ 阈值 → 不触发
    assert ctx["compactor"].maybe_compact(ctx["session_id"]) is None

    _converse(ctx, rounds=4)  # 尾部 19 条 > 阈值 → 增量压缩
    second = ctx["compactor"].maybe_compact(ctx["session_id"])
    assert second is not None
    tail = ctx["messages"].list_after(ctx["session_id"], after_id=first.first_kept_message_id)
    second_cut = next(i for i, m in enumerate(tail) if m.id == second.first_kept_message_id)
    assert second.messages_compacted == second_cut  # 增量:只压未压缩尾部
    # 迭代更新:旧摘要首行(第1轮诉求)保留在新摘要中,不丢历史
    assert first.summary.splitlines()[0] in second.summary


# --- 摘要生成器 ---


def test_llm_summarizer_used_when_available(ctx) -> None:
    calls = []

    def fake_llm(conversation: str) -> str:
        calls.append(conversation)
        return "LLM 摘要:用户反复报告工牌故障"

    compactor = SessionCompactor(ctx["messages"], ctx["compactions"], summarizer=fake_llm)
    _converse(ctx, rounds=8)
    entry = compactor.maybe_compact(ctx["session_id"])

    assert entry is not None and entry.summarizer == "llm"
    assert "工牌" in entry.summary
    assert len(calls) == 1


def test_llm_failure_falls_back_to_deterministic(ctx) -> None:
    def broken_llm(conversation: str) -> str:
        raise RuntimeError("provider down")

    compactor = SessionCompactor(ctx["messages"], ctx["compactions"], summarizer=broken_llm)
    _converse(ctx, rounds=8)
    entry = compactor.maybe_compact(ctx["session_id"])

    assert entry is not None and entry.summarizer == "deterministic"
    assert "诉求" in entry.summary  # 抽取式兜底仍含用户原话


def test_no_safe_cut_point_returns_none(ctx) -> None:
    """全是 assistant 消息时找不到 user 切点,宁可不压(不拆轮次)。"""
    for i in range(15):
        _msg(ctx, "assistant", f"只有助手消息 {i}")
    assert ctx["compactor"].maybe_compact(ctx["session_id"]) is None


# --- 记忆来源标记(v2.md §49:confirmed 优先) ---


@pytest.fixture()
def memory_ctx():
    conn = connect(":memory:")
    apply_migrations(conn)
    store = TicketStore(conn)
    yield {
        "conn": conn,
        "memories": MemoryRepository(conn),
        "tickets": TicketService(store),
        "store": store,
        "user": IdentityResolver(UserRepository(conn), ChannelIdentityRepository(conn)).resolve(
            "wecom", "zhangsan", "张三"
        ),
    }
    conn.close()


def _memory(memory_ctx, memory_id: str, source: str) -> Memory:
    ticket = memory_ctx["tickets"].create(memory_ctx["user"].id, f"空调故障 {memory_id}", "d")
    return Memory(
        id=memory_id,
        user_id=memory_ctx["user"].id,
        ticket_id=ticket.id,
        kind=MemoryKind.STABLE_FACT,
        fact="A3 空调控制板故障",
        confidence=0.9,
        source=source,
    )


def test_memory_source_roundtrip(memory_ctx) -> None:
    memory_ctx["memories"].add(_memory(memory_ctx, "m1", "confirmed_closure"))
    memory_ctx["memories"].add(_memory(memory_ctx, "m2", "force_closed"))
    stored = {m.id: m.source for m in memory_ctx["memories"].list_by_user(memory_ctx["user"].id)}
    assert stored == {"m1": "confirmed_closure", "m2": "force_closed"}


def test_remember_stamps_source(memory_ctx) -> None:
    service = MemoryService(memory_ctx["store"], memory_ctx["memories"])
    ticket = memory_ctx["tickets"].create(memory_ctx["user"].id, "A3 空调坏了", "d")
    memory_ctx["tickets"].claim(ticket.id)
    memory_ctx["tickets"].resolve(ticket.id, {"note": "已换保险丝"})
    memory_ctx["tickets"].close(ticket.id)

    stamped = service.remember(ticket.id, source="confirmed_closure")
    assert all(m.source == "confirmed_closure" for m in stamped)


def test_recall_prefers_confirmed_over_force_closed(memory_ctx) -> None:
    memories = memory_ctx["memories"]
    memories.add(_memory(memory_ctx, "mem_force", "force_closed"))
    memories.add(_memory(memory_ctx, "mem_confirmed", "confirmed_closure"))

    service = MemoryService(memory_ctx["store"], memories)
    hits = service.recall(memory_ctx["user"].id, "空调控制板又故障了", top_k=2)

    assert [h.memory.id for h in hits] == ["mem_confirmed", "mem_force"]
