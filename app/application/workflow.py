"""SupportWorkflow: conversation-purpose-aware dispatch (V2.1).

Deterministic pre-routing stays deterministic (protocol, identity,
purpose, commands, confirmation, idempotency). The agent handles
semantic understanding (support / continuation / no-answer handoff /
grounded FAQ). The workflow is split for the two-phase transaction
model:

    prepare()   phase A (inside the ingress transaction): all
                deterministic effects (ticket creation, work items,
                confirmation/operator/approval actions) + agent context
    run_agent() between transactions: bounded agent run (NO DB write lock)
    apply()     phase B (second transaction): policy-validated decision
                persistence + requester notifications + HITL proposals
    resume()    rebuild the agent phase after a crash between A and B

Invariants preserved: agent advice never mutates state (invariant #4);
low-confidence RAG still becomes a real handoff (invariant #7); operator
space has no implicit ticket; approval stays an independent state machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from app.application.agent_decision import AgentDecision
from app.application.agent_tools import AgentToolPort
from app.application.command_parser import CommandParser
from app.application.context_builder import AgentContext, ContextBuilder, KnowledgeEvidence
from app.application.conversation_service import ConversationService
from app.application.intent_router import IntentRouter
from app.application.memory_service import MemoryService
from app.application.notification_service import SYSTEM_ACTOR
from app.application.policy import PolicyValidator
from app.application.retriever import RAGAnswer, RetrievalHit, Retriever
from app.application.role_service import RoleService
from app.application.support_agent import (
    INTENT_FAQ,
    INTENT_NO_ANSWER,
    INTENT_SUPPORT,
    AgentRunResult,
    SupportAgent,
)
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
from app.infrastructure.processing import ProcessingRecord
from app.infrastructure.repositories import MessageRepository, SessionTicketContextRepository
from app.infrastructure.trace import TraceLogger

_NO_ANSWER_REPLY = (
    "抱歉，知识库中没有找到足够可靠的答案。已为您创建工单并转人工客服跟进，请补充更多细节。"
)

_OTHER_REPLY = "您的问题需要人工支持处理，我已记录下来，会尽快由专人跟进。"
_STAFF_OTHER_REPLY = "已收到消息。如需操作工单请使用命令（如：认领 T0002 / T0002 已修复 解决）。"

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
    analysis: AgentDecision | None = None
    recalled: list[Memory] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)


@dataclass
class PreparedOutcome:
    """Result of phase A: either a finished deterministic result or an
    agent-ready context that phase B (apply) will finalize."""

    kind: WorkflowKind
    needs_agent: bool = False
    intent: str = ""
    context: AgentContext | None = None
    reply: str = ""
    ticket: Ticket | None = None
    recalled: list[Memory] = field(default_factory=list)
    created: bool = False
    result: WorkflowResult | None = None


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
        policy: PolicyValidator | None = None,
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
        self._policy = policy

    # --- public entry points (two-phase contract) ---

    def handle(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation | None,
    ) -> WorkflowResult:
        """Synchronous full processing (single transaction model; kept for
        direct callers). The ingress path uses prepare/run_agent/apply."""
        prepared = self.prepare(envelope, user, session, conversation)
        if not prepared.needs_agent:
            return prepared.result  # type: ignore[return-value]
        run = self.run_agent(prepared)
        return self.apply(envelope, user, session, conversation, prepared, run)

    def prepare(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation | None,
    ) -> PreparedOutcome:
        """Phase A: deterministic routing + effects. Caller holds txn."""
        conversation = self._ensure_conversation(envelope, conversation)
        self._record_reply(envelope.text, user, session, envelope, role="user")

        if conversation.purpose == ConversationPurpose.OPERATOR:
            return self._prepare_operator(envelope, user, session, conversation)
        if conversation.purpose == ConversationPurpose.APPROVAL:
            return self._prepare_approval(envelope, user, session, conversation)
        return self._prepare_requester(envelope, user, session, conversation)

    def run_agent(self, prepared: PreparedOutcome) -> AgentRunResult:
        """Agent run between transactions — NO database write lock held."""
        if not prepared.needs_agent or prepared.context is None:
            raise RuntimeError("run_agent called for a non-agent prepared outcome")
        allowed, extra = self._tool_whitelist(prepared)
        return self._agent.run(
            prepared.context,
            intent=prepared.intent,
            allowed_tools=allowed,
            extra_instructions=extra,
        )

    def _tool_whitelist(self, prepared: PreparedOutcome) -> tuple[frozenset[str], str]:
        """Intent-driven tool whitelist assembly + L4 entity interception.

        C9 router (升级计划 §7.2): each intent sees only the tools it needs;
        precise-entity patterns (phone/id-card/工号/资产号) lock the run down
        to entity lookups and inject a masking instruction.
        """
        from app.application.agent_tools import ALLOWED_TOOLS
        from app.application.entity_guard import GUARD_INSTRUCTION, detect_entities

        context = prepared.context
        text = context.latest_user_text if context else ""
        guard_kinds = detect_entities(text)
        if guard_kinds:
            return (
                frozenset({"contact_lookup", "asset_lookup"}),
                GUARD_INSTRUCTION,
            )
        base: dict[str, frozenset[str]] = {
            INTENT_FAQ: frozenset({"search_knowledge", "recall_memory"}),
            INTENT_NO_ANSWER: frozenset(),
            INTENT_SUPPORT: frozenset({
                "get_ticket_history",
                "search_knowledge",
                "recall_memory",
                "get_allowed_actions",
                "contact_lookup",
                "asset_lookup",
                "ticket_stats",
            }),
        }
        allowed = base.get(prepared.intent)
        if allowed is None:  # unknown/progress intents keep full read surface
            return ALLOWED_TOOLS, ""
        return allowed, ""

    def apply(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation | None,
        prepared: PreparedOutcome,
        run: AgentRunResult,
    ) -> WorkflowResult:
        """Phase B: persist the validated decision (caller holds txn)."""
        conversation = self._ensure_conversation(envelope, conversation)
        if prepared.intent == INTENT_FAQ:
            result = self._apply_faq(envelope, user, session, conversation, prepared, run)
        elif prepared.intent == INTENT_NO_ANSWER:
            result = self._apply_no_answer(envelope, user, session, conversation, prepared, run)
        else:
            result = self._apply_support(envelope, user, session, conversation, prepared, run)
        self._emit_reply_trace(envelope, result.kind, result.reply)
        return result

    def resume(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation | None,
        record: ProcessingRecord,
    ) -> PreparedOutcome:
        """Rebuild the agent phase after a crash between A and B. Never
        re-runs deterministic business effects (ticket already exists)."""
        conversation = self._ensure_conversation(envelope, conversation)
        if record.intent == INTENT_FAQ:
            answer = self._retriever.answer(envelope.text)
            evidence = self._evidence_from(answer.hits) if answer is not None else []
            context = self._build_context(envelope, user, session, None, conversation, knowledge_evidence=evidence)
            return PreparedOutcome(
                kind=WorkflowKind.FAQ_ANSWER, needs_agent=True, intent=INTENT_FAQ, context=context
            )
        ticket = self._tickets.get(record.ticket_id) if record.ticket_id else None
        recalled = self._recall(user.id, envelope.text)
        actor_role = self._actor_role(user.id)
        context = self._build_context(
            envelope, user, session, ticket, conversation, recalled=recalled, actor_role=actor_role
        )
        return PreparedOutcome(
            kind=WorkflowKind(record.kind) if record.kind else WorkflowKind.TICKET,
            needs_agent=True,
            intent=record.intent or INTENT_SUPPORT,
            context=context,
            ticket=ticket,
            recalled=recalled,
            created=False,
        )

    # --- requester conversation: deterministic pre-routing ---

    def _prepare_requester(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> PreparedOutcome:
        confirmation = self._parser.parse_requester_confirmation(envelope.text)
        if confirmation is not None:
            result = self._handle_confirmation(envelope, user, session, conversation, confirmation)
            if result is not None:
                return self._deterministic(envelope, user, session, result.kind, result.reply, result.ticket)

        decision = self._router.route(envelope.text)
        self._trace_event(
            envelope.trace_id,
            "intent",
            {"intent": decision.intent, "confidence": decision.confidence, "reason": decision.reason},
        )
        if decision.intent == "faq":
            return self._prepare_faq(envelope, user, session, conversation)
        if decision.intent == "support":
            return self._prepare_support(envelope, user, session, conversation)
        if decision.intent == "progress_query":
            return self._prepare_progress(envelope, user, session, conversation)
        if decision.intent == "other" and len(self._tickets.active_tickets(user.id)) == 1:
            # Unclassifiable text with exactly one active ticket: continuation (AC-12).
            return self._prepare_support(envelope, user, session, conversation)
        return self._prepare_other(envelope, user, session, conversation)

    def _prepare_faq(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> PreparedOutcome:
        answer: RAGAnswer | None = self._retriever.answer(envelope.text)
        if answer is None:
            self._trace_event(envelope.trace_id, "retrieval", {"grounded": False})
            ticket = self._create_handoff_ticket(envelope, user, conversation)
            context = self._build_context(envelope, user, session, ticket, conversation)
            return PreparedOutcome(
                kind=WorkflowKind.NO_ANSWER,
                needs_agent=True,
                intent=INTENT_NO_ANSWER,
                context=context,
                ticket=ticket,
            )
        self._trace_event(
            envelope.trace_id,
            "retrieval",
            {
                "grounded": True,
                "hits": [{"doc_id": h.document.doc_id, "score": round(h.score, 3)} for h in answer.hits],
            },
        )
        evidence = self._evidence_from(answer.hits)
        context = self._build_context(envelope, user, session, None, conversation, knowledge_evidence=evidence)
        return PreparedOutcome(
            kind=WorkflowKind.FAQ_ANSWER, needs_agent=True, intent=INTENT_FAQ, context=context
        )

    def _prepare_support(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> PreparedOutcome:
        session_ticket_id = self._session_ctx.get(session.id) if self._session_ctx else None
        resolution = self._resolver.resolve(envelope.text, user.id, session_ticket_id=session_ticket_id)
        if resolution.kind == ResolutionKind.CLARIFY:
            reply = self._clarify_reply(resolution.candidates)
            self._trace_event(
                envelope.trace_id,
                "ticket",
                {"resolution": resolution.kind.value, "candidates": [t.id for t in resolution.candidates]},
            )
            return self._deterministic(envelope, user, session, WorkflowKind.CLARIFY, reply)

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

        recalled = self._recall(user.id, envelope.text)
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
        actor_role = self._actor_role(user.id)
        context = self._build_context(
            envelope, user, session, ticket, conversation, recalled=recalled, actor_role=actor_role
        )
        return PreparedOutcome(
            kind=WorkflowKind.TICKET,
            needs_agent=True,
            intent=INTENT_SUPPORT,
            context=context,
            ticket=ticket,
            recalled=recalled,
            created=created,
        )

    def _prepare_progress(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> PreparedOutcome:
        session_ticket_id = self._session_ctx.get(session.id) if self._session_ctx else None
        resolution = self._resolver.resolve(envelope.text, user.id, session_ticket_id=session_ticket_id)
        if resolution.kind == ResolutionKind.CLARIFY:
            reply = self._clarify_reply(resolution.candidates)
            self._trace_event(
                envelope.trace_id,
                "ticket",
                {"resolution": resolution.kind.value, "candidates": [t.id for t in resolution.candidates]},
            )
            return self._deterministic(envelope, user, session, WorkflowKind.CLARIFY, reply)
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
                return self._deterministic(envelope, user, session, WorkflowKind.PROGRESS, reply, latest)
            reply = "您还没有工单，可以描述您的问题，我会为您创建工单。"
            return self._deterministic(envelope, user, session, WorkflowKind.PROGRESS, reply)
        self._bind_session_ticket(session, ticket)
        self._trace_event(
            envelope.trace_id,
            "ticket",
            {"resolution": resolution.kind.value, "ticket_id": ticket.id},
        )
        reply = self._status_line(ticket)
        return self._deterministic(envelope, user, session, WorkflowKind.PROGRESS, reply, ticket)

    def _prepare_other(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> PreparedOutcome:
        if conversation.purpose in (ConversationPurpose.OPERATOR, ConversationPurpose.APPROVAL):
            # AC-16: shared staff conversations never get implicit tickets;
            # reply neutrally without claiming any follow-up.
            return self._deterministic(envelope, user, session, WorkflowKind.OTHER, _STAFF_OTHER_REPLY)
        # §41/AC-21 honesty: a reply that claims "专人跟进" must back a real
        # ticket with an operator work item — never a fake handoff.
        ticket = self._create_handoff_ticket(envelope, user, conversation)
        self._bind_session_ticket(session, ticket)
        self._trace_event(
            envelope.trace_id,
            "ticket",
            {"resolution": "other_handoff", "ticket_id": ticket.id},
        )
        return self._deterministic(envelope, user, session, WorkflowKind.OTHER, _OTHER_REPLY, ticket)

    def _prepare_operator(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> PreparedOutcome:
        command = self._parser.parse_operator(envelope.text)
        if command is None:
            # Non-command message in an operator conversation: an operator may
            # file a ticket on behalf of someone, or test the pipeline.
            # Commands still require the operator role; plain messages go
            # through the requester flow. No implicit ticket anywhere.
            return self._prepare_requester(envelope, user, session, conversation)
        if self._roles is None or not self._roles.has_role(user.id, UserRole.OPERATOR):
            return self._deterministic(
                envelope, user, session, WorkflowKind.FORBIDDEN, "无操作权限：该会话仅限运维人员使用。"
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
                return self._deterministic(envelope, user, session, WorkflowKind.OPERATOR_ACTION, _OPERATOR_GUIDANCE)
        except AlreadyClaimed:
            return self._deterministic(
                envelope, user, session, WorkflowKind.FORBIDDEN, f"工单 {command.ticket_id} 已被其他人认领。"
            )
        except InvalidStateTransition as exc:
            return self._deterministic(envelope, user, session, WorkflowKind.FORBIDDEN, f"操作失败：{exc}")
        except KeyError:
            return self._deterministic(envelope, user, session, WorkflowKind.FORBIDDEN, f"工单不存在：{command.ticket_id}")
        return self._deterministic(envelope, user, session, WorkflowKind.OPERATOR_ACTION, outcome.reply, outcome.ticket)

    def _prepare_approval(
        self, envelope: InboundEnvelope, user: User, session: Session, conversation: Conversation
    ) -> PreparedOutcome:
        if self._roles is None or not self._roles.has_role(user.id, UserRole.APPROVER):
            return self._deterministic(
                envelope, user, session, WorkflowKind.FORBIDDEN, "无操作权限：该会话仅限审批人员使用。"
            )
        command = self._parser.parse_approver(envelope.text)
        if command is None:
            return self._deterministic(envelope, user, session, WorkflowKind.APPROVAL_ACTION, _APPROVAL_GUIDANCE)
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
        except (InvalidApprovalDecision, InvalidStateTransition, AlreadyClaimed, KeyError):
            return self._deterministic(envelope, user, session, WorkflowKind.FORBIDDEN, "该审批已被处理或不存在。")
        return self._deterministic(envelope, user, session, WorkflowKind.APPROVAL_ACTION, outcome.reply, outcome.ticket)

    # --- phase B: apply validated agent decisions (policy-gated) ---

    def _apply_support(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation,
        prepared: PreparedOutcome,
        run: AgentRunResult,
    ) -> WorkflowResult:
        decision = run.decision
        ticket = prepared.ticket
        if ticket is None:
            raise RuntimeError("support apply without a ticket")
        reply, proposal_note = self._proposal_step(envelope, conversation, prepared, run, ticket)

        # Policy accepts agent suggestions as business values; the agent
        # never writes the ticket directly (invariant #4).
        self._tickets.set_operational(
            ticket.id,
            summary=decision.summary,
            category=decision.category,
            priority="P2" if decision.priority_suggestion == "high" else "P3",
            queue=conversation.queue or "general",
        )
        self._requester_notifications(envelope, user, conversation, ticket, decision, run, reply)
        if not prepared.created:
            self._operator_update_note(envelope, conversation, ticket, decision, run)

        self._record_reply(reply, user, session, envelope)
        self._trace_agent_run(envelope.trace_id, run)
        self._trace_proposal(envelope.trace_id, prepared, proposal_note)
        return WorkflowResult(
            kind=WorkflowKind.TICKET,
            reply=reply,
            ticket=ticket,
            analysis=decision,
            recalled=prepared.recalled,
        )

    def _apply_no_answer(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation,
        prepared: PreparedOutcome,
        run: AgentRunResult,
    ) -> WorkflowResult:
        decision = run.decision
        ticket = prepared.ticket
        if ticket is None:
            raise RuntimeError("no_answer apply without a ticket")
        reply = decision.reply_draft
        self._tickets.set_operational(
            ticket.id,
            summary=decision.summary,
            category=decision.category,
            priority="P2" if decision.priority_suggestion == "high" else "P3",
            queue=conversation.queue or "general",
        )
        self._requester_notifications(envelope, user, conversation, ticket, decision, run, reply)
        self._record_reply(reply, user, session, envelope)
        self._trace_agent_run(envelope.trace_id, run)
        return WorkflowResult(
            kind=WorkflowKind.NO_ANSWER,
            reply=reply,
            ticket=ticket,
            analysis=decision,
            recalled=prepared.recalled,
        )

    def _apply_faq(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        conversation: Conversation,
        prepared: PreparedOutcome,
        run: AgentRunResult,
    ) -> WorkflowResult:
        decision = run.decision
        reply = decision.reply_draft
        self._record_reply(reply, user, session, envelope)
        if self._actions is not None:
            self._actions.conversation_reply(
                channel=conversation.channel,
                conversation_id=conversation.channel_conversation_id,
                text=reply,
                trace_id=envelope.trace_id,
                source_event_id=f"faq:{run.run_id}",
            )
        self._trace_agent_run(envelope.trace_id, run)
        return WorkflowResult(
            kind=WorkflowKind.FAQ_ANSWER,
            reply=reply,
            analysis=decision,
            recalled=[],
            sources=[hit for hit in self._evidence_hits(prepared)],
        )

    # --- proposal handling: Agent proposes -> Policy validates -> HITL ---

    def _proposal_step(
        self,
        envelope: InboundEnvelope,
        conversation: Conversation,
        prepared: PreparedOutcome,
        run: AgentRunResult,
        ticket: Ticket,
    ) -> tuple[str, str]:
        """Validate the decision's action_proposal; if allowed, create the
        approval + pending action (HITL). Returns (final_reply, note)."""
        decision = run.decision
        proposal = decision.action_proposal
        verdict = (
            self._policy.validate_proposal(proposal, prepared.context)
            if self._policy is not None and proposal is not None
            else None
        )
        note = ""
        if proposal is not None:
            allowed = verdict is not None and verdict.allowed
            self._trace_event(
                envelope.trace_id,
                "policy",
                {"proposal": proposal.action, "verdict": verdict.reason if verdict else "no-policy"},
            )
            if allowed and self._actions is not None:
                if proposal.action == "ESCALATE":
                    self._actions.escalate(
                        ticket.id,
                        SYSTEM_ACTOR,
                        proposal.reason,
                        trace_id=envelope.trace_id,
                        channel=conversation.channel,
                    )
                else:
                    self._actions.force_close(
                        ticket.id,
                        SYSTEM_ACTOR,
                        proposal.reason,
                        trace_id=envelope.trace_id,
                        channel=conversation.channel,
                    )
                note = f"（已发起{proposal.action}审批，等待审批人处理。）"
        reply = decision.reply_draft
        if note and note not in reply:
            reply = f"{reply}{note}"
        return reply, note

    def _requester_notifications(
        self,
        envelope: InboundEnvelope,
        user: User,
        conversation: Conversation,
        ticket: Ticket,
        decision: AgentDecision,
        run: AgentRunResult,
        reply: str,
    ) -> None:
        """Honest receipt: the public reply is the agent draft (it never
        claims private delivery); PRIVATE_DETAIL is enqueued only when a
        private target actually resolves (H7 fix)."""
        if self._actions is None:
            return
        priority = "P2" if decision.priority_suggestion == "high" else "P3"
        status_hint = "待维修人员认领" if ticket.status.value == "OPEN" else f"当前{ticket.status.value}"
        # Legacy-adapted (reference/scripts/wecom_bridge_server.py:889): the
        # private DM carries the real detailed explanation, not a bare form.
        parts = [
            f"工单 {ticket.id} 已受理：{ticket.title}",
            f"状态：{ticket.status.value}（{status_hint}）；优先级：{priority}",
        ]
        analysis = decision.understanding or decision.summary
        if analysis:
            parts.append(f"\n情况分析：{analysis}")
        if decision.missing_information:
            parts.append("为加快处理，请补充：" + "、".join(decision.missing_information))
        parts.append("\n进展会同步给你；你也可以直接在这里补充信息。")
        private_detail = "\n".join(parts)
        self._actions.requester_acknowledgement(
            ticket,
            user.id,
            reply=reply,
            private_detail=private_detail,
            source_event_id=f"{ticket.id}:agent:{run.run_id}",
            trace_id=envelope.trace_id,
            channel=conversation.channel,
        )

    def _operator_update_note(
        self,
        envelope: InboundEnvelope,
        conversation: Conversation,
        ticket: Ticket,
        decision: AgentDecision,
        run: AgentRunResult,
    ) -> None:
        """Continuation: operators get the updated recommendation."""
        if self._actions is None:
            return
        note = (
            f"Agent 更新（工单 {ticket.id}）：分类={decision.category}，"
            f"优先级={decision.priority_suggestion}。{decision.summary}"
            f"（{decision.rationale}）"
        )
        self._actions.operator_update_note(
            ticket,
            note=note,
            source_event_id=f"{ticket.id}:agent_update:{run.run_id}",
            trace_id=envelope.trace_id,
            channel=conversation.channel,
        )

    # --- helpers ---

    def _create_handoff_ticket(
        self, envelope: InboundEnvelope, user: User, conversation: Conversation
    ) -> Ticket:
        """NO_ANSWER -> real human handoff: a ticket exists, operators get
        a work item, and the requester gets a truthful reply."""
        return self._actions.create_ticket(
            user.id,
            envelope.text,
            conversation,
            requester_name=user.display_name,
            trace_id=envelope.trace_id,
            source="no_answer_handoff",
        )

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
        self._record_reply(outcome.reply, user, session, envelope)
        return WorkflowResult(kind=kind, reply=outcome.reply, ticket=outcome.ticket)

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

    def _status_line(self, ticket: Ticket) -> str:
        base = f"工单 {ticket.id}（{ticket.title}）当前状态：{ticket.status.value}"
        if ticket.status.value == "RESOLVED":
            return base + "，等待您确认。请回复“确认”或说明还未恢复。"
        if ticket.status.value == "IN_PROGRESS":
            assignee = ticket.assignee_user_id or "处理中"
            return base + f"，处理人员：{assignee}，我们会持续跟进。"
        return base + "，我们会持续跟进。"

    def _build_context(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        ticket: Ticket | None,
        conversation: Conversation,
        recalled: list[Memory] | None = None,
        actor_role: str = "",
        knowledge_evidence: list[KnowledgeEvidence] | None = None,
    ) -> AgentContext:
        return self._context_builder.build(
            envelope,
            user,
            session,
            ticket,
            recalled_memories=recalled,
            conversation_type=conversation.conversation_type.value,
            conversation_purpose=conversation.purpose.value,
            actor_role=actor_role,
            channel=conversation.channel,
            conversation_id=conversation.channel_conversation_id,
            location=conversation.location or "",
            knowledge_evidence=knowledge_evidence,
        )

    def _recall(self, user_id: str, text: str) -> list[Memory]:
        if self._memory is None:
            return []
        return [hit.memory for hit in self._memory.recall(user_id, text)]

    def _actor_role(self, user_id: str) -> str:
        if self._roles is None:
            return ""
        return self._roles.primary_role(user_id)

    def _deterministic(
        self,
        envelope: InboundEnvelope,
        user: User,
        session: Session,
        kind: WorkflowKind,
        reply: str,
        ticket: Ticket | None = None,
    ) -> PreparedOutcome:
        self._record_reply(reply, user, session, envelope)
        self._emit_reply_trace(envelope, kind, reply)
        return PreparedOutcome(
            kind=kind, reply=reply, ticket=ticket, result=WorkflowResult(kind=kind, reply=reply, ticket=ticket)
        )

    def _ensure_conversation(
        self, envelope: InboundEnvelope, conversation: Conversation | None
    ) -> Conversation:
        if conversation is not None:
            return conversation
        return Conversation(
            id="conv_fallback",
            channel=envelope.channel,
            channel_conversation_id=envelope.conversation_id,
            conversation_type=ConversationType.DM,
            purpose=ConversationPurpose.REQUESTER,
        )

    def _bind_session_ticket(self, session: Session, ticket: Ticket) -> None:
        if self._session_ctx is not None:
            self._session_ctx.set(session.id, ticket.id)

    def _trace_event(self, trace_id: str, stage: str, payload: dict) -> None:
        if self._trace is not None:
            self._trace.event(trace_id, stage, payload)

    def _emit_reply_trace(self, envelope: InboundEnvelope, kind: WorkflowKind, reply: str) -> None:
        self._trace_event(envelope.trace_id, "reply", {"workflow": kind.value, "reply": reply})

    def _trace_agent_run(self, trace_id: str, run: AgentRunResult) -> None:
        self._trace_event(
            trace_id,
            "agent",
            {
                "agent_run_id": run.run_id,
                "prompt_key": run.prompt_key,
                "prompt_version": run.prompt_version,
                "model": run.model,
                "latency_ms": run.latency_ms,
                "steps": run.steps,
                "tool_calls": [t.tool for t in run.tool_calls],
                "tool_call_count": len(run.tool_calls),
                "summary": run.decision.summary,
                "category": run.decision.category,
                "priority": run.decision.priority_suggestion,
                "action": run.decision.recommended_action,
                "confidence": run.decision.confidence,
                "rationale": run.decision.rationale,
                "knowledge_refs": run.decision.knowledge_refs,
                "memory_refs": run.decision.memory_refs,
                "fallback_used": run.fallback_used,
                "fallback_reason": run.fallback_reason,
                "error_type": run.error_type,
            },
        )

    def _trace_proposal(self, trace_id: str, prepared: PreparedOutcome, note: str) -> None:
        if note:
            self._trace_event(trace_id, "proposal", {"note": note, "ticket_id": prepared.ticket.id if prepared.ticket else None})

    @staticmethod
    def _evidence_from(hits: list[RetrievalHit]) -> list[KnowledgeEvidence]:
        return [
            KnowledgeEvidence(
                source_id=h.document.doc_id,
                title=h.document.title,
                excerpt=h.document.content,
                retrieval_score=h.score,
            )
            for h in hits
        ]

    @staticmethod
    def _evidence_hits(prepared: PreparedOutcome) -> list[RetrievalHit]:
        """Rebuild RetrievalHit list from the agent's evidence (for result
        sources on the FAQ path)."""
        from app.application.retriever import FaqDocument

        return [
            RetrievalHit(
                document=FaqDocument(doc_id=e.source_id, title=e.title, content=e.excerpt),
                score=e.retrieval_score,
            )
            for e in (prepared.context.knowledge_evidence if prepared.context else [])
        ]

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
