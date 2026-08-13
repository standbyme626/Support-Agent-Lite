"""SupportWorkflow: intent-based dispatch for one inbound message.

Phase 4 (milestone 2 — intent split):

    faq             -> RAG grounded answer, NO ticket (invariant #7)
    support         -> TicketResolver -> create/continue ticket -> agent analysis
    progress_query  -> TicketResolver -> ticket status reply
    other           -> explicit handoff-style reply, no ticket

The agent's analysis is advice only (invariant #4): all ticket state
changes happen through TicketService.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from app.application.context_builder import ContextBuilder
from app.application.intent_router import IntentRouter
from app.application.retriever import RAGAnswer, RetrievalHit, Retriever
from app.application.support_agent import AgentAnalysis, SupportAgent
from app.application.ticket_service import ResolutionKind, TicketResolver, TicketService
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.domain.message import Message
from app.domain.ticket import Ticket
from app.infrastructure.repositories import MessageRepository

_NO_ANSWER_REPLY = (
    "抱歉，我没有找到足够相关的资料来回答这个问题。"
    "为准确起见，这个问题会转交人工支持处理，请您补充更多细节。"
)

_OTHER_REPLY = "您的问题需要人工支持处理，我已记录下来，会尽快由专人跟进。"


class WorkflowKind(str, Enum):
    FAQ_ANSWER = "faq_answer"
    NO_ANSWER = "no_answer"
    TICKET = "ticket"
    PROGRESS = "progress"
    CLARIFY = "clarify"
    OTHER = "other"


@dataclass
class WorkflowResult:
    kind: WorkflowKind
    reply: str
    ticket: Ticket | None = None
    sources: list[RetrievalHit] = field(default_factory=list)
    analysis: AgentAnalysis | None = None


class SupportWorkflow:
    """Coordinates intent routing, retrieval, ticket resolution and the agent."""

    def __init__(
        self,
        router: IntentRouter,
        retriever: Retriever,
        ticket_service: TicketService,
        resolver: TicketResolver,
        context_builder: ContextBuilder,
        agent: SupportAgent,
        messages: MessageRepository,
    ) -> None:
        self._router = router
        self._retriever = retriever
        self._tickets = ticket_service
        self._resolver = resolver
        self._context_builder = context_builder
        self._agent = agent
        self._messages = messages
        self._session_ticket: dict[str, str] = {}

    def handle(self, envelope: InboundEnvelope, user: User, session: Session) -> WorkflowResult:
        self._record_reply(envelope.text, user, session, envelope, role="user")
        decision = self._router.route(envelope.text)

        if decision.intent == "faq":
            return self._handle_faq(envelope, user, session)
        if decision.intent == "support":
            return self._handle_support(envelope, user, session)
        if decision.intent == "progress_query":
            return self._handle_progress(envelope, user, session)
        return self._handle_other(envelope, user, session)

    # --- intent branches ---

    def _handle_faq(self, envelope: InboundEnvelope, user: User, session: Session) -> WorkflowResult:
        answer: RAGAnswer | None = self._retriever.answer(envelope.text)
        if answer is None:
            self._record_reply(_NO_ANSWER_REPLY, user, session, envelope)
            return WorkflowResult(kind=WorkflowKind.NO_ANSWER, reply=_NO_ANSWER_REPLY)
        self._record_reply(answer.text, user, session, envelope)
        return WorkflowResult(kind=WorkflowKind.FAQ_ANSWER, reply=answer.text, sources=answer.hits)

    def _handle_support(self, envelope: InboundEnvelope, user: User, session: Session) -> WorkflowResult:
        resolution = self._resolver.resolve(
            envelope.text, user.id, session_ticket_id=self._session_ticket.get(session.id)
        )
        if resolution.kind == ResolutionKind.CLARIFY:
            reply = self._clarify_reply(resolution.candidates)
            self._record_reply(reply, user, session, envelope)
            return WorkflowResult(kind=WorkflowKind.CLARIFY, reply=reply)

        ticket = resolution.ticket
        if resolution.kind == ResolutionKind.CREATE_NEW:
            ticket = self._tickets.create(user.id, title=envelope.text, description=envelope.text)
        if ticket is None:
            raise RuntimeError(f"support intent resolved without a ticket: {resolution.kind}")
        self._session_ticket[session.id] = ticket.id

        context = self._context_builder.build(envelope, user, session, ticket)
        analysis = self._agent.analyze(context)
        self._record_reply(analysis.reply_draft, user, session, envelope)
        return WorkflowResult(
            kind=WorkflowKind.TICKET,
            reply=analysis.reply_draft,
            ticket=ticket,
            analysis=analysis,
        )

    def _handle_progress(self, envelope: InboundEnvelope, user: User, session: Session) -> WorkflowResult:
        resolution = self._resolver.resolve(
            envelope.text, user.id, session_ticket_id=self._session_ticket.get(session.id)
        )
        if resolution.kind == ResolutionKind.CLARIFY:
            reply = self._clarify_reply(resolution.candidates)
            self._record_reply(reply, user, session, envelope)
            return WorkflowResult(kind=WorkflowKind.CLARIFY, reply=reply)
        ticket = resolution.ticket
        if ticket is None:
            # No active ticket: fall back to the most recent ticket of any
            # status so users can still check on resolved/closed work.
            latest = self._tickets.recent(user.id)
            if latest is not None:
                self._session_ticket[session.id] = latest.id
                reply = f"工单 {latest.id}（{latest.title}）当前状态：{latest.status.value}。"
                self._record_reply(reply, user, session, envelope)
                return WorkflowResult(kind=WorkflowKind.PROGRESS, reply=reply, ticket=latest)
            reply = "您还没有工单，可以描述您的问题，我会为您创建工单。"
            self._record_reply(reply, user, session, envelope)
            return WorkflowResult(kind=WorkflowKind.PROGRESS, reply=reply)
        self._session_ticket[session.id] = ticket.id
        reply = f"工单 {ticket.id}（{ticket.title}）当前状态：{ticket.status.value}，我们会持续跟进。"
        self._record_reply(reply, user, session, envelope)
        return WorkflowResult(kind=WorkflowKind.PROGRESS, reply=reply, ticket=ticket)

    def _handle_other(self, envelope: InboundEnvelope, user: User, session: Session) -> WorkflowResult:
        self._record_reply(_OTHER_REPLY, user, session, envelope)
        return WorkflowResult(kind=WorkflowKind.OTHER, reply=_OTHER_REPLY)

    # --- helpers ---

    def _record_reply(
        self,
        text: str,
        user: User,
        session: Session,
        envelope: InboundEnvelope,
        *,
        role: str = "assistant",
    ) -> None:
        self._messages.add(
            Message(
                id=uuid4().hex[:12],
                session_id=session.id,
                user_id=user.id,
                role=role,
                text=text,
                trace_id=envelope.trace_id,
            )
        )

    @staticmethod
    def _clarify_reply(candidates: list[Ticket]) -> str:
        listed = "、".join(f"{t.id}（{t.title}）" for t in candidates)
        return f"您有多个进行中的工单：{listed}。请告诉我您指的是哪一个？"
