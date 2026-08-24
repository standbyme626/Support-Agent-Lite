"""ContextBuilder: assemble the agent's full perception (V2.1).

The contract changed from "summary + recent messages" to "everything the
agent needs to reason": current message, actor/role, conversation
purpose/type/channel/location, current ticket + persisted summary,
recent conversation (chronological, role-labeled), recalled memories,
and initial RAG evidence. What the model actually sees is rendered from
THIS dataclass — nothing is collected-but-unused anymore (AC-A01).
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


@dataclass(frozen=True)
class KnowledgeEvidence:
    """One retrieval hit offered to the agent as grounded evidence."""

    source_id: str
    title: str
    excerpt: str
    retrieval_score: float


@dataclass
class AgentContext:
    # identity / trace
    user_id: str
    session_id: str
    trace_id: str
    # conversation perception
    channel: str = ""
    conversation_id: str = ""
    conversation_type: str = ""  # DM | GROUP
    conversation_purpose: str = ""  # REQUESTER | OPERATOR | APPROVAL
    actor_role: str = ""  # requester | operator | approver
    location: str = ""
    # ticket perception
    ticket: Ticket | None = None
    ticket_summary: str = ""
    # conversation perception (chronological, role-labeled)
    recent_messages: list[Message] = field(default_factory=list)
    latest_user_text: str = ""
    # memory / knowledge perception
    recalled_memories: list[Memory] = field(default_factory=list)
    knowledge_evidence: list[KnowledgeEvidence] = field(default_factory=list)

    @property
    def memory_ids(self) -> set[str]:
        return {m.id for m in self.recalled_memories}

    @property
    def knowledge_ids(self) -> set[str]:
        return {e.source_id for e in self.knowledge_evidence}


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
        channel: str = "",
        conversation_id: str = "",
        location: str = "",
        knowledge_evidence: list[KnowledgeEvidence] | None = None,
    ) -> AgentContext:
        recent = self._messages.recent(session.id, limit=self._recent_limit)
        return AgentContext(
            user_id=user.id,
            session_id=session.id,
            trace_id=envelope.trace_id,
            channel=channel or envelope.channel,
            conversation_id=conversation_id or envelope.conversation_id,
            conversation_type=conversation_type,
            conversation_purpose=conversation_purpose,
            actor_role=actor_role,
            location=location,
            ticket=ticket,
            ticket_summary=self._summarize_ticket(ticket, recent),
            recent_messages=recent,
            latest_user_text=envelope.text,
            recalled_memories=list(recalled_memories or []),
            knowledge_evidence=list(knowledge_evidence or []),
        )

    @staticmethod
    def _summarize_ticket(ticket: Ticket | None, recent: list[Message]) -> str:
        if ticket is None:
            return "（暂无关联工单）"
        lines = [f"工单 {ticket.id}（{ticket.status.value}）", f"标题：{ticket.title}", f"描述：{ticket.description}"]
        if ticket.summary:
            lines.append(f"摘要：{ticket.summary}")
        if recent:
            last = recent[-1]
            lines.append(f"最近消息：{last.text}")
        return "\n".join(lines)
