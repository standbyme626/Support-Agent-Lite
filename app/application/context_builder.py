"""ContextBuilder: assemble agent context (ticket summary + recent messages).

Phase 4 contract (AC-07): after multiple messages, the context must
contain a coherent ticket summary plus the recent conversation so the
agent can reason without channel/session leakage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.domain.memory import Memory
from app.domain.message import Message
from app.domain.ticket import Ticket
from app.infrastructure.repositories import MessageRepository

_RECENT_LIMIT = 6


@dataclass
class AgentContext:
    user_id: str
    session_id: str
    trace_id: str
    ticket: Ticket | None
    ticket_summary: str
    recent_messages: list[Message]
    latest_user_text: str
    recalled_memories: list[Memory] = field(default_factory=list)
    conversation_type: str = ""  # V2: DM | GROUP
    conversation_purpose: str = ""  # V2: REQUESTER | OPERATOR | APPROVAL
    actor_role: str = ""  # V2: requester | operator | approver


class ContextBuilder:
    """Builds an AgentContext for a resolved ticket + session history."""

    def __init__(self, messages: MessageRepository, recent_limit: int = _RECENT_LIMIT) -> None:
        self._messages = messages
        self._recent_limit = recent_limit

    def build(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        ticket: Ticket | None,
        recalled_memories: list[Memory] | None = None,
        conversation_type: str = "",
        conversation_purpose: str = "",
        actor_role: str = "",
    ) -> AgentContext:
        recent = self._messages.recent(session.id, limit=self._recent_limit)
        return AgentContext(
            user_id=user.id,
            session_id=session.id,
            trace_id=envelope.trace_id,
            ticket=ticket,
            ticket_summary=self._summarize_ticket(ticket, recent),
            recent_messages=recent,
            latest_user_text=envelope.text,
            recalled_memories=list(recalled_memories or []),
            conversation_type=conversation_type,
            conversation_purpose=conversation_purpose,
            actor_role=actor_role,
        )

    @staticmethod
    def _summarize_ticket(ticket: Ticket | None, recent: list[Message]) -> str:
        if ticket is None:
            return "（暂无关联工单）"
        lines = [f"工单 {ticket.id}（{ticket.status.value}）", f"标题：{ticket.title}", f"描述：{ticket.description}"]
        if recent:
            last = recent[-1]
            lines.append(f"最近消息：{last.text}")
        return "\n".join(lines)
