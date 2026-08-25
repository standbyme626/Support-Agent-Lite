"""V2.1 agent tests: ContextBuilder (AC-07) + SupportAgent (invariant #4,
full perception AC-A01, fallback AC-A08/A-A13).
"""
import pytest

from app.application.context_builder import ContextBuilder
from app.application.identity_service import IdentityResolver
from app.application.session_service import SessionService
from app.application.support_agent import INTENT_SUPPORT, SupportAgent
from app.application.ticket_service import TicketService
from app.domain.envelope import InboundEnvelope
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.repositories import (
    ChannelIdentityRepository,
    MessageRepository,
    SessionRepository,
    TicketStore,
    UserRepository,
)
from tests.fake_llm import BrokenLLM, RecordingLLM, make_decision


@pytest.fixture()
def ctx():
    conn = connect(":memory:")
    apply_migrations(conn)
    users = UserRepository(conn)
    identities = ChannelIdentityRepository(conn)
    sessions = SessionRepository(conn)
    store = TicketStore(conn)
    messages = MessageRepository(conn)
    identity = IdentityResolver(users, identities)
    session_service = SessionService(sessions)
    user = identity.resolve("wecom", "zhangsan", "张三")
    session = session_service.find_or_create(user.id, "wecom", "conv_1")
    yield {
        "conn": conn,
        "messages": messages,
        "tickets": TicketService(store),
        "store": store,
        "user": user,
        "session": session,
    }
    conn.close()


def envelope(text: str, message_id: str = "m1") -> InboundEnvelope:
    return InboundEnvelope(
        channel="wecom",
        message_id=message_id,
        channel_user_id="zhangsan",
        conversation_id="conv_1",
        text=text,
    )


def test_context_builder_includes_ticket_summary(ctx) -> None:
    """AC-07: ticket summary + recent messages build the correct context."""
    ticket = ctx["tickets"].create(ctx["user"].id, "A3 空调坏了", "A3 空调不制冷")

    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("处理了吗？"), ctx["user"], ctx["session"], ticket)

    assert context.ticket is ticket
    assert "A3 空调坏了" in context.ticket_summary
    assert "A3 空调不制冷" in context.ticket_summary
    assert context.ticket.id in context.ticket_summary
    assert context.latest_user_text == "处理了吗？"


def test_context_builder_recent_messages_in_order(ctx) -> None:
    from app.domain.message import Message
    from uuid import uuid4

    for i, text in enumerate(["空调坏了", "很急", "谁来处理"]):
        ctx["messages"].add(
            Message(
                id=uuid4().hex[:12],
                session_id=ctx["session"].id,
                user_id=ctx["user"].id,
                role="user",
                text=text,
            )
        )
    builder = ContextBuilder(ctx["messages"], recent_limit=2)
    context = builder.build(envelope("继续跟进"), ctx["user"], ctx["session"], ticket=None)

    texts = [m.text for m in context.recent_messages]
    assert texts == ["很急", "谁来处理"]  # most recent two, in order


def test_agent_analysis_outputs_advice_only(ctx) -> None:
    """Invariant #4: the agent outputs analysis; it never mutates tickets."""
    ticket = ctx["tickets"].create(ctx["user"].id, "A3 空调坏了", "不制冷")
    status_before = ticket.status
    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("空调坏了，很紧急"), ctx["user"], ctx["session"], ticket)

    run = SupportAgent().run(context)

    decision = run.decision
    assert decision.category == "device"
    assert decision.priority_suggestion == "high"
    assert decision.recommended_action == "dispatch_repair"
    assert "工单" in decision.summary and ticket.id in decision.summary
    assert "A3 空调坏了" in decision.reply_draft
    stored = ctx["store"].get(ticket.id)
    assert stored is not None and stored.status == status_before  # untouched
    assert ctx["store"].events(ticket.id) and ctx["store"].events(ticket.id)[0].event_type.value == "created"
    # deterministic fallback path (no LLM)
    assert run.fallback_used is True
    assert run.fallback_reason == "no_llm"
    assert run.prompt_key == "agent_decision.support"
    assert run.prompt_version == "v1"
    assert run.model == "none"


def test_agent_category_and_priority_rules(ctx) -> None:
    builder = ContextBuilder(ctx["messages"])
    agent = SupportAgent()

    account = agent.analyze(builder.build(envelope("邮箱密码登录不了"), ctx["user"], ctx["session"], None))
    assert account.category == "account"
    assert account.recommended_action == "credential_reset"

    normal = agent.analyze(builder.build(envelope("打印机脱机"), ctx["user"], ctx["session"], None))
    assert normal.priority_suggestion == "normal"

    urgent = agent.analyze(builder.build(envelope("服务器中断，影响工作"), ctx["user"], ctx["session"], None))
    assert urgent.priority_suggestion == "high"


def test_agent_llm_polish_with_fallback(ctx) -> None:
    """AC-A01 + AC-A08: valid LLM output is schema-validated and used;
    the prompt contains the full perception (recent messages, role,
    purpose, ticket state, memories, knowledge)."""
    fake = RecordingLLM(
        reply=make_decision(
            summary="LLM摘要",
            reply="LLM回复草稿",
            category="network",
            priority="high",
            action="network_triage",
            memory_refs=[],
            knowledge_refs=[],
        )
    )
    builder = ContextBuilder(ctx["messages"])
    context = builder.build(
        envelope("空调坏了"),
        ctx["user"],
        ctx["session"],
        None,
        conversation_type="GROUP",
        conversation_purpose="REQUESTER",
        actor_role="requester",
    )
    run = SupportAgent(llm=fake).run(context)

    decision = run.decision
    assert decision.summary == "LLM摘要"
    assert decision.reply_draft == "LLM回复草稿"
    assert decision.category == "network"
    assert decision.priority_suggestion == "high"
    assert run.fallback_used is False
    assert run.model == "recording-test-model"

    # AC-A01: the model really sees the perception, not just the message
    prompt = fake.last_prompt()
    assert "空调坏了" in prompt
    assert "REQUESTER" in prompt
    assert "GROUP" in prompt
    assert "requester" in prompt
    assert "无相关记忆" in prompt or "（无）" in prompt
    assert "<user_message>" in prompt  # untrusted-content delimiter


def test_agent_llm_failure_falls_back_to_rules(ctx) -> None:
    """AC-A13: LLM unavailable -> deterministic fallback, no exception."""
    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("空调坏了"), ctx["user"], ctx["session"], None)
    run = SupportAgent(llm=BrokenLLM()).run(context)
    assert "空调坏了" in run.decision.summary
    assert run.decision.reply_draft  # deterministic fallback reply
    assert run.fallback_used is True
    assert run.fallback_reason == "llm_error:RuntimeError"


def test_agent_llm_timeout_falls_back(ctx) -> None:
    """AC-A13: timeout -> deterministic fallback."""
    from tests.fake_llm import TimeoutLLM

    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("空调坏了"), ctx["user"], ctx["session"], None)
    run = SupportAgent(llm=TimeoutLLM()).run(context)
    assert run.fallback_used is True
    assert run.fallback_reason == "llm_error:TimeoutError"
    assert run.error_type == "TimeoutError"
    assert "空调坏了" in run.decision.summary


def test_agent_malformed_output_falls_back(ctx) -> None:
    """AC-A08: plain text / empty / bad JSON -> fallback, never a crash."""
    from tests.fake_llm import MalformedLLM

    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("空调坏了"), ctx["user"], ctx["session"], None)
    for payload in ("", "抱歉我无法回答", '{"summary": "只有摘要"}', "[]"):
        run = SupportAgent(llm=MalformedLLM(payload)).run(context)
        assert run.fallback_used is True
        assert "空调坏了" in run.decision.summary


def test_agent_invalid_enum_falls_back(ctx) -> None:
    """AC-A08: unknown category/action/priority -> fallback."""
    from tests.fake_llm import MalformedLLM

    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("空调坏了"), ctx["user"], ctx["session"], None)
    bad = make_decision(category="alien", action="hack_ticket", priority="urgent")
    run = SupportAgent(llm=MalformedLLM(bad)).run(context)
    assert run.fallback_used is True
    assert "invalid_decision" in run.fallback_reason


def test_agent_oversized_reply_falls_back(ctx) -> None:
    """AC-A08: oversized reply is rejected (bounded output)."""
    from tests.fake_llm import MalformedLLM

    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("空调坏了"), ctx["user"], ctx["session"], None)
    big = make_decision(reply="长" * 500)
    run = SupportAgent(llm=MalformedLLM(big)).run(context)
    assert run.fallback_used is True
    assert "reply-too-long" in run.fallback_reason


def test_agent_refs_are_validated_against_context(ctx) -> None:
    """AC-A04/A-A05: hallucinated memory/knowledge refs are dropped."""
    from app.application.retriever import Retriever
    from app.domain.memory import Memory, MemoryKind
    from pathlib import Path

    memory = Memory(
        id="mem_real",
        user_id=ctx["user"].id,
        ticket_id="T0001",
        kind=MemoryKind.STABLE_FACT,
        fact="A3 空调控制板故障已更换",
        confidence=0.9,
    )
    builder = ContextBuilder(ctx["messages"])
    context = builder.build(
        envelope("A3 空调又不制冷了"),
        ctx["user"],
        ctx["session"],
        None,
        recalled_memories=[memory],
        knowledge_evidence=[],
    )
    fake = RecordingLLM(reply=make_decision(memory_refs=["mem_real", "mem_hallucinated"], knowledge_refs=["faq_ghost"]))
    decision = SupportAgent(llm=fake).run(context).decision
    assert decision.memory_refs == ["mem_real"]  # hallucinated ref dropped
    assert decision.knowledge_refs == []
