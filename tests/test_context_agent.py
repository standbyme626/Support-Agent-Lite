"""Phase 4 tests: ContextBuilder (AC-07) + SupportAgent (invariant #4)."""
import pytest

from app.application.context_builder import ContextBuilder
from app.application.identity_service import IdentityResolver
from app.application.session_service import SessionService
from app.application.support_agent import SupportAgent
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

    analysis = SupportAgent().analyze(context)

    assert analysis.category == "device"
    assert analysis.priority_suggestion == "high"
    assert analysis.recommended_action == "dispatch_repair"
    assert "工单" in analysis.summary and ticket.id in analysis.summary
    assert "A3 空调坏了" in analysis.reply_draft
    stored = ctx["store"].get(ticket.id)
    assert stored is not None and stored.status == status_before  # untouched
    assert ctx["store"].events(ticket.id) and ctx["store"].events(ticket.id)[0].event_type.value == "created"


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
    class FakeLLM:
        def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:  # noqa: ARG002
            return '{"summary": "LLM摘要", "reply_draft": "LLM回复草稿"}'

    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("空调坏了"), ctx["user"], ctx["session"], None)
    analysis = SupportAgent(llm=FakeLLM()).analyze(context)
    assert analysis.summary == "LLM摘要"
    assert analysis.reply_draft == "LLM回复草稿"


def test_agent_llm_failure_falls_back_to_rules(ctx) -> None:
    class BrokenLLM:
        def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:  # noqa: ARG002
            raise RuntimeError("llm down")

    builder = ContextBuilder(ctx["messages"])
    context = builder.build(envelope("空调坏了"), ctx["user"], ctx["session"], None)
    analysis = SupportAgent(llm=BrokenLLM()).analyze(context)
    assert "空调坏了" in analysis.summary
    assert analysis.reply_draft  # deterministic fallback reply
