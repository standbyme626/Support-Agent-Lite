"""SupportWorkflow: conversation-purpose-aware dispatch (V2).

Routes by ConversationPurpose, never by channel:
    REQUESTER -> intent router (faq/support/progress/other) + resolution
                 confirmation/rejection detection + NO_ANSWER real handoff
    OPERATOR  -> explicit-ticket-id actions (no implicit active ticket)
    APPROVAL  -> approve/reject actions

The agent's analysis stays advice-only (invariant #4). Session->ticket
context is persisted; the user-centric resolver remains the primary
algorithm.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from app.application.command_parser import CommandParser
from app.application.context_builder import ContextBuilder
from app.application.conversation_service import ConversationService
from app.application.intent_router import IntentRouter
from app.application.memory_service import MemoryService
from app.application.notification_service import NotificationService
from app.application.retriever import RAGAnswer, RetrievalHit, Retriever
from app.application.role_service import RoleService
from app.application.support_agent import AgentAnalysis, SupportAgent
from app.application.ticket_action_service import TicketActionService
from app.application.ticket_service import ResolutionKind, TicketResolver, TicketService
from app.domain.approval import InvalidApprovalDecision
from app.domain.conversation import Conversation, ConversationPurpose, ConversationType
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.domain.memory import Memory
from app.domain.message import Message
from app.domain.role import UserRole
from app.domain.ticket import AlreadyClaimed, InvalidStateTransition, Ticket
from app.infrastructure.repositories import MessageRepository, SessionTicketContextRepository
from app.infrastructure.trace import TraceLogger

_NO_ANSWER_REPLY = (
    "抱歉，知识库中没有找到足够可靠的答案。已为您创建工单并转人工跟进，请补充更多细节。"
)

_OTHER_REPLY = "您的问题需要人工支持处理，我已记录下来，会尽快由专人跟进。"

_OPERATOR_GUIDANCE = "共享群内操作需要显式工单号，例如：/claim T1001、/resolve T1001 说明、/escalate T1001 原因。"

_APPROVAL_GUIDANCE = "审批命令格式：/approve apr_xxx 或 /reject apr_xxx 原因。"


class WorkflowKind(str, Enum):
    FAQ_ANSWER = "faq_answer"
    NO_ANSWER = "no_answer"
    TICKET = "ticket"
    PROGRESS = "progress"
    CLARIFY = "clarify"
    OTHER = "other"
    OPERATOR_ACTION = "operator_action"
    APPROVAL_ACTION = "approval_action"
    CONFIRMATION = "confirmation"
    REJECTED = "rejected"
    FORBIDDEN = "forbidden"


@dataclass
class WorkflowResult:
    kind: WorkflowKind
    reply: str
    ticket: Ticket | None = None
    sources: list[RetrievalHit] = field(default_factory=list)
    analysis: AgentAnalysis | None = None
    recalled: list[Memory] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)


class SupportWorkflow:
    def __init__(
        self,
        router: IntentRouter,
        retriever: Retriever,
        ticket_service: TicketService,
        resolver: TicketResolver,
        context_builder: ContextBuilder,
        agent: SupportAgent,
        messages: MessageRepository,
        memory: MemoryService | None = None,
        trace: TraceLogger | None = None,
        conversations: ConversationService | None = None,
        actions: TicketActionService | None = None,
        roles: RoleService | None = None,
        parser: CommandParser | None = None,
        session_ctx: SessionTicketContextRepository | None = None,
    ) -> None:
        self._router = router
        self._retriever = retriever
        self._tickets = ticket_service
        self._resolver = resolver
        self._context_builder = context_builder
        self._agent = agent
        self._messages = messages
        self._memory = memory
        self._trace = trace
        self._conversations = conversations
        self._actions = actions
        self._roles = roles
        self._parser = parser or CommandParser()
        self._session_ctx = session_ctx

    def handle(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation | None,
    ) -> WorkflowResult:
        if conversation is None:
            conversation = Conversation(
                id="conv_fallback",
                channel=envelope.channel,
                channel_conversation_id=envelope.conversation_id,
                conversation_type=ConversationType.DM,
                purpose=ConversationPurpose.REQUESTER,
            )
        self._record_reply(envelope.text, user, session, envelope, role="user")

        if conversation.purpose == ConversationPurpose.OPERATOR:
            result = self._handle_operator(envelope, user, session, conversation)
            self._emit_reply_trace(envelope, result)
            return result
        if conversation.purpose == ConversationPurpose.APPROVAL:
            result = self._handle_approval(envelope, user, session, conversation)
            self._emit_reply_trace(envelope, result)
            return result
        result = self._handle_requester(envelope, user, session, conversation)
        self._emit_reply_trace(envelope, result)
        return result

    def _emit_reply_trace(self, envelope: InboundEnvelope, result: WorkflowResult) -> None:
        self._trace_event(
            envelope.trace_id,
            "reply",
            {"workflow": result.kind.value, "reply": result.reply},
        )

    # --- requester conversation ---

    def _handle_requester(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> WorkflowResult:
        confirmation = self._parser.parse_requester_confirmation(envelope.text)
        if confirmation is not None:
            result = self._handle_confirmation(envelope, user, session, conversation, confirmation)
            if result is not None:
                return result

        decision = self._router.route(envelope.text)
        self._trace_event(
            envelope.trace_id,
            "intent",
            {"intent": decision.intent, "confidence": decision.confidence, "reason": decision.reason},
        )
        if decision.intent == "faq":
            return self._handle_faq(envelope, user, session, conversation)
        if decision.intent == "support":
            return self._handle_support(envelope, user, session, conversation)
        if decision.intent == "progress_query":
            return self._handle_progress(envelope, user, session, conversation)
        if decision.intent == "other" and len(self._tickets.active_tickets(user.id)) == 1:
            # Unclassifiable text in a requester conversation with exactly one
            # active ticket: treat as continuation (AC-12) - never a new ticket.
            return self._handle_support(envelope, user, session, conversation)
        return self._handle_other(envelope, user, session, conversation)

    def _handle_confirmation(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation,
        confirmation: tuple[str | None, str],
    ) -> WorkflowResult | None:
        explicit_id, action = confirmation
        candidate = self._resolution_candidate(explicit_id, user.id, session.id)
        if candidate is None or candidate.status.value != "RESOLVED":
            return None
        try:
            if action == "confirm":
                outcome = self._actions.requester_confirm(
                    candidate.id,
                    user.id,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    conversation_id=envelope.conversation_id,
                )
                kind = WorkflowKind.CONFIRMATION
            else:
                outcome = self._actions.reject_resolution(
                    candidate.id,
                    user.id,
                    reason=envelope.text,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    conversation_id=envelope.conversation_id,
                )
                kind = WorkflowKind.REJECTED
        except InvalidStateTransition:
            return None
        self._trace_event(
            envelope.trace_id,
            "ticket",
            {"resolution": "requester_" + action, "ticket_id": candidate.id},
        )
        return self._finish(envelope, user, session, kind, outcome.reply, ticket=outcome.ticket)

    def _resolution_candidate(self, explicit_id: str | None, user_id: str, session_id: str) -> Ticket | None:
        if explicit_id:
            ticket = self._tickets.get(explicit_id)
            if ticket is not None and ticket.user_id == user_id:
                return ticket
        session_ticket_id = self._session_ctx.get(session_id) if self._session_ctx else None
        if session_ticket_id:
            ticket = self._tickets.get(session_ticket_id)
            if ticket is not None and ticket.user_id == user_id:
                return ticket
        return self._tickets.recent(user_id)

    def _handle_faq(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> WorkflowResult:
        answer: RAGAnswer | None = self._retriever.answer(envelope.text)
        if answer is None:
            self._trace_event(envelope.trace_id, "retrieval", {"grounded": False})
            ticket = self._create_handoff_ticket(envelope, user, conversation)
            self._record_reply(_NO_ANSWER_REPLY, user, session, envelope)
            return WorkflowResult(kind=WorkflowKind.NO_ANSWER, reply=_NO_ANSWER_REPLY, ticket=ticket)
        self._trace_event(
            envelope.trace_id,
            "retrieval",
            {
                "grounded": True,
                "hits": [{"doc_id": h.document.doc_id, "score": round(h.score, 3)} for h in answer.hits],
            },
        )
        self._record_reply(answer.text, user, session, envelope)
        return WorkflowResult(kind=WorkflowKind.FAQ_ANSWER, reply=answer.text, sources=answer.hits)

    def _create_handoff_ticket(
        self, envelope: InboundEnvelope, user: User, conversation: Conversation
    ) -> Ticket:
        """NO_ANSWER -> real human handoff: a ticket exists, operators get
        a work item, and the requester gets a truthful reply."""
        ticket = self._actions.create_ticket(
            user.id,
            envelope.text,
            conversation,
            requester_name=user.display_name,
            trace_id=envelope.trace_id,
            source="no_answer_handoff",
        )
        return ticket

    def _handle_support(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> WorkflowResult:
        session_ticket_id = self._session_ctx.get(session.id) if self._session_ctx else None
        resolution = self._resolver.resolve(envelope.text, user.id, session_ticket_id=session_ticket_id)
        if resolution.kind == ResolutionKind.CLARIFY:
            reply = self._clarify_reply(resolution.candidates)
            self._trace_event(
                envelope.trace_id,
                "ticket",
                {"resolution": resolution.kind.value, "candidates": [t.id for t in resolution.candidates]},
            )
            self._record_reply(reply, user, session, envelope)
            return WorkflowResult(kind=WorkflowKind.CLARIFY, reply=reply)

        ticket = resolution.ticket
        created = resolution.kind == ResolutionKind.CREATE_NEW
        if created:
            ticket = self._actions.create_ticket(
                user.id,
                envelope.text,
                conversation,
                requester_name=user.display_name,
                trace_id=envelope.trace_id,
            )
        if ticket is None:
            raise RuntimeError(f"support intent resolved without a ticket: {resolution.kind}")
        self._bind_session_ticket(session, ticket)

        recalled: list[Memory] = []
        if self._memory is not None:
            recalled = [hit.memory for hit in self._memory.recall(user.id, envelope.text)]
        self._trace_event(
            envelope.trace_id,
            "ticket",
            {"resolution": resolution.kind.value, "ticket_id": ticket.id, "created": created},
        )
        if recalled:
            self._trace_event(
                envelope.trace_id,
                "memory_recall",
                {"facts": [m.fact for m in recalled]},
            )
        context = self._context_builder.build(envelope, user, session, ticket, recalled_memories=recalled)
        analysis = self._agent.analyze(context)
        self._trace_event(
            envelope.trace_id,
            "agent",
            {
                "summary": analysis.summary,
                "category": analysis.category,
                "priority": analysis.priority_suggestion,
                "action": analysis.recommended_action,
            },
        )
        self._apply_operational(ticket.id, analysis, conversation)
        self._record_reply(analysis.reply_draft, user, session, envelope)
        return WorkflowResult(
            kind=WorkflowKind.TICKET,
            reply=analysis.reply_draft,
            ticket=ticket,
            analysis=analysis,
            recalled=recalled,
        )

    def _apply_operational(self, ticket_id: str, analysis: AgentAnalysis, conversation: Conversation) -> None:
        """Policy layer accepts agent suggestions as business values.
        The agent never writes the ticket directly (invariant #4)."""
        self._tickets.set_operational(
            ticket_id,
            summary=analysis.summary,
            category=analysis.category,
            priority="P2" if analysis.priority_suggestion == "high" else "P3",
            queue=conversation.queue or "general",
        )

    def _handle_progress(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> WorkflowResult:
        session_ticket_id = self._session_ctx.get(session.id) if self._session_ctx else None
        resolution = self._resolver.resolve(envelope.text, user.id, session_ticket_id=session_ticket_id)
        if resolution.kind == ResolutionKind.CLARIFY:
            reply = self._clarify_reply(resolution.candidates)
            self._trace_event(
                envelope.trace_id,
                "ticket",
                {"resolution": resolution.kind.value, "candidates": [t.id for t in resolution.candidates]},
            )
            self._record_reply(reply, user, session, envelope)
            return WorkflowResult(kind=WorkflowKind.CLARIFY, reply=reply)
        ticket = resolution.ticket
        if ticket is None:
            latest = self._tickets.recent(user.id)
            if latest is not None:
                self._bind_session_ticket(session, latest)
                self._trace_event(
                    envelope.trace_id,
                    "ticket",
                    {"resolution": "recent", "ticket_id": latest.id},
                )
                reply = self._status_line(latest)
                self._record_reply(reply, user, session, envelope)
                return WorkflowResult(kind=WorkflowKind.PROGRESS, reply=reply, ticket=latest)
            reply = "您还没有工单，可以描述您的问题，我会为您创建工单。"
            self._record_reply(reply, user, session, envelope)
            return WorkflowResult(kind=WorkflowKind.PROGRESS, reply=reply)
        self._bind_session_ticket(session, ticket)
        self._trace_event(
            envelope.trace_id,
            "ticket",
            {"resolution": resolution.kind.value, "ticket_id": ticket.id},
        )
        reply = self._status_line(ticket)
        self._record_reply(reply, user, session, envelope)
        return WorkflowResult(kind=WorkflowKind.PROGRESS, reply=reply, ticket=ticket)

    def _status_line(self, ticket: Ticket) -> str:
        base = f"工单 {ticket.id}（{ticket.title}）当前状态：{ticket.status.value}"
        if ticket.status.value == "RESOLVED":
            return base + "，等待您确认。请回复“确认”或说明还未恢复。"
        if ticket.status.value == "IN_PROGRESS":
            assignee = ticket.assignee_user_id or "处理中"
            return base + f"，处理人员：{assignee}，我们会持续跟进。"
        return base + "，我们会持续跟进。"

    def _handle_other(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> WorkflowResult:
        self._record_reply(_OTHER_REPLY, user, session, envelope)
        return WorkflowResult(kind=WorkflowKind.OTHER, reply=_OTHER_REPLY)

    # --- operator conversation ---

    def _handle_operator(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> WorkflowResult:
        if self._roles is None or not self._roles.has_role(user.id, UserRole.OPERATOR):
            return self._finish(
                envelope, user, session, WorkflowKind.FORBIDDEN, "无操作权限：该会话仅限运维人员使用。"
            )
        command = self._parser.parse_operator(envelope.text)
        if command is None:
            active = self._tickets.list_by_queue(conversation.queue)
            listing = "、".join(f"{t.id}（{t.status.value}）" for t in active) or "当前无待处理工单"
            return self._finish(
                envelope,
                user,
                session,
                WorkflowKind.OPERATOR_ACTION,
                f"待处理工单：{listing}\n{_OPERATOR_GUIDANCE}",
            )
        try:
            if command.action.value == "claim":
                outcome = self._actions.claim(
                    command.ticket_id,
                    user.id,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    conversation_id=envelope.conversation_id,
                )
            elif command.action.value == "resolve":
                outcome = self._actions.resolve(
                    command.ticket_id,
                    user.id,
                    command.note,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    conversation_id=envelope.conversation_id,
                )
            elif command.action.value == "escalate":
                outcome = self._actions.escalate(
                    command.ticket_id,
                    user.id,
                    command.reason,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    conversation_id=envelope.conversation_id,
                )
            elif command.action.value == "force_close":
                outcome = self._actions.force_close(
                    command.ticket_id,
                    user.id,
                    command.reason,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    conversation_id=envelope.conversation_id,
                )
            else:
                return self._finish(envelope, user, session, WorkflowKind.OPERATOR_ACTION, _OPERATOR_GUIDANCE)
        except AlreadyClaimed:
            return self._finish(
                envelope, user, session, WorkflowKind.FORBIDDEN, f"工单 {command.ticket_id} 已被其他人认领。"
            )
        except InvalidStateTransition as exc:
            return self._finish(envelope, user, session, WorkflowKind.FORBIDDEN, f"操作失败：{exc}")
        except KeyError:
            return self._finish(envelope, user, session, WorkflowKind.FORBIDDEN, f"工单不存在：{command.ticket_id}")
        return self._finish(envelope, user, session, WorkflowKind.OPERATOR_ACTION, outcome.reply, ticket=outcome.ticket)

    # --- approval conversation ---

    def _handle_approval(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> WorkflowResult:
        if self._roles is None or not self._roles.has_role(user.id, UserRole.APPROVER):
            return self._finish(
                envelope, user, session, WorkflowKind.FORBIDDEN, "无操作权限：该会话仅限审批人员使用。"
            )
        command = self._parser.parse_approver(envelope.text)
        if command is None:
            return self._finish(envelope, user, session, WorkflowKind.APPROVAL_ACTION, _APPROVAL_GUIDANCE)
        try:
            if command.action.value == "approve":
                outcome = self._actions.approve(
                    command.approval_id,
                    user.id,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    conversation_id=envelope.conversation_id,
                )
            else:
                outcome = self._actions.reject(
                    command.approval_id,
                    user.id,
                    command.reason,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    conversation_id=envelope.conversation_id,
                )
        except (InvalidApprovalDecision, InvalidStateTransition, AlreadyClaimed):
            return self._finish(envelope, user, session, WorkflowKind.FORBIDDEN, "该审批已被处理或不存在。")
        except KeyError:
            return self._finish(envelope, user, session, WorkflowKind.FORBIDDEN, "该审批已被处理或不存在。")
        return self._finish(envelope, user, session, WorkflowKind.APPROVAL_ACTION, outcome.reply, ticket=outcome.ticket)

    # --- helpers ---

    def _bind_session_ticket(self, session: Session, ticket: Ticket) -> None:
        if self._session_ctx is not None:
            self._session_ctx.set(session.id, ticket.id)

    def _trace_event(self, trace_id: str, stage: str, payload: dict) -> None:
        if self._trace is not None:
            self._trace.event(trace_id, stage, payload)

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

    def _finish(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        kind: WorkflowKind,
        reply: str,
        ticket: Ticket | None = None,
    ) -> WorkflowResult:
        self._record_reply(reply, user, session, envelope)
        return WorkflowResult(kind=kind, reply=reply, ticket=ticket)
