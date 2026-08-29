"""IngressService: webhook entry for a channel (V2.1 two-phase).

Pipeline:

    Phase A (one transaction):
        claim idempotency key -> identity -> session -> conversation
        -> deterministic workflow effects (ticket/events/work items,
           operator/approval/confirmation actions)
        -> processing state = AGENT_PENDING (agent paths) or COMPLETED

    Agent run (BETWEEN transactions): bounded agent loop (LLM + read
    tools). NO database write lock is held while the network call runs
    (AC-A11). Read-only tool queries are autocommit reads.

    Phase B (one transaction, CAS-guarded):
        policy-validated decision -> operational fields -> requester
        notifications -> HITL proposals -> state = COMPLETED

    Post-commit: dispatch notifications (outbox).

Crash safety (AC-A12): phase A commits atomically with the claim. If the
process dies after A, a later duplicate delivery resumes from
AGENT_PENDING/FAILED_RETRYABLE — deterministic effects are never
re-run (no duplicate tickets/events). COMPLETED duplicates are no-ops.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.adapters.base import ChannelAdapter
from app.application.conversation_service import ConversationService
from app.application.identity_service import IdentityResolver
from app.application.notification_service import NotificationService
from app.application.session_service import SessionService
from app.application.workflow import SupportWorkflow
from app.domain.conversation import ConversationPurpose, ConversationType
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.infrastructure.idempotency import IdempotencyStore
from app.infrastructure.processing import InboundProcessingStore, ProcessingState
from app.infrastructure.repositories import txn
from app.infrastructure.trace import TraceLogger


@dataclass
class IngressResult:
    envelope: InboundEnvelope
    user: User
    session: Session
    conversation: object | None = None
    duplicate: bool = False
    downstream: object | None = None


class IngressService:
    """Processes one raw channel payload end to end (ingress stage)."""

    def __init__(
        self,
        adapters: dict[str, ChannelAdapter],
        identity: IdentityResolver,
        sessions: SessionService,
        idempotency: IdempotencyStore,
        downstream: Callable[[InboundEnvelope, User, Session, object], object] | None = None,
        trace: TraceLogger | None = None,
        conversations: ConversationService | None = None,
        notifications: NotificationService | None = None,
        workflow: SupportWorkflow | None = None,
        auto_dispatch: bool = True,
    ) -> None:
        self._adapters = adapters
        self._identity = identity
        self._sessions = sessions
        self._idempotency = idempotency
        self._downstream = downstream
        self._trace = trace
        self._conversations = conversations
        self._notifications = notifications
        self._workflow = workflow
        # auto_dispatch=True (default, direct/test callers): dispatch runs
        # synchronously at the end of process(). auto_dispatch=False (the
        # production app): the HTTP layer schedules dispatch as a background
        # task so outbound channel HTTP never blocks the webhook response.
        self._auto_dispatch = auto_dispatch
        self._processing = InboundProcessingStore(idempotency._conn)  # noqa: SLF001

    def process(self, channel: str, payload: dict, on_stage: Callable[[str, dict], None] | None = None) -> IngressResult:
        """Process one raw payload. `on_stage(stage, data)` (C5 SSE) is called
        between pipeline phases so a streaming client can observe progress;
        it must never affect processing (exceptions swallowed by _emit)."""
        adapter = self._adapters[channel]
        envelope = adapter.build_inbound(payload)
        key = adapter.idempotency_key(payload)
        if self._workflow is not None:
            return self._process_two_phase(envelope, key, on_stage)
        return self._process_legacy(envelope, key)

    # --- two-phase processing (V2.1) ---

    def _emit(self, on_stage: Callable[[str, dict], None] | None, stage: str, data: dict) -> None:
        if on_stage is None:
            return
        try:
            on_stage(stage, data)
        except Exception:  # noqa: BLE001 - 观测回调绝不影响主流程
            pass

    def _process_two_phase(self, envelope: InboundEnvelope, key: str, on_stage: Callable[[str, dict], None] | None = None) -> IngressResult:
        conn = self._idempotency._conn  # noqa: SLF001
        # ---- Phase A: claim + identity/session/conversation + deterministic ----
        with txn(conn):
            claimed = self._idempotency.claim(key, envelope.trace_id)
            user = self._identity.resolve(envelope.channel, envelope.channel_user_id)
            session = self._sessions.find_or_create(user.id, envelope.channel, envelope.conversation_id)
            conversation = self._resolve_conversation(envelope)
            if not claimed:
                record = self._processing.get(key)
                if record is None or record.state in ProcessingState.FINAL:
                    self._trace_event(
                        envelope.trace_id,
                        "channel",
                        {"channel": envelope.channel, "message_id": envelope.message_id, "duplicate": True},
                    )
                    return IngressResult(envelope=envelope, user=user, session=session, conversation=conversation, duplicate=True)
                # Crash between A and B (or phase-B failure): resume the agent
                # phase WITHOUT re-running deterministic business effects.
                self._trace_event(
                    envelope.trace_id,
                    "channel",
                    {
                        "channel": envelope.channel,
                        "message_id": envelope.message_id,
                        "duplicate": True,
                        "resume": record.state.value,
                    },
                )
                if record.state == ProcessingState.FAILED_RETRYABLE:
                    self._processing.update(key, state=ProcessingState.AGENT_PENDING, error=None)
                prepared = self._workflow.resume(envelope, user, session, conversation, record)
            else:
                self._trace_event(
                    envelope.trace_id,
                    "channel",
                    {"channel": envelope.channel, "message_id": envelope.message_id, "text": envelope.text},
                )
                self._trace_event(
                    envelope.trace_id,
                    "identity",
                    {
                        "channel_identity": f"{envelope.channel}/{envelope.channel_user_id}",
                        "user_id": user.id,
                        "session_id": session.id,
                    },
                )
                self._processing.claim(
                    key,
                    trace_id=envelope.trace_id,
                    channel=envelope.channel,
                    user_id=user.id,
                    session_id=session.id,
                    conversation_channel=getattr(conversation, "channel", envelope.channel),
                    conversation_id=getattr(conversation, "channel_conversation_id", envelope.conversation_id),
                )
                prepared = self._workflow.prepare(envelope, user, session, conversation)
                self._emit(
                    on_stage,
                    "received",
                    {"trace_id": envelope.trace_id, "user_id": user.id, "session_id": session.id},
                )
                self._emit(
                    on_stage,
                    "prepared",
                    {
                        "intent": getattr(prepared, "intent", None),
                        "kind": getattr(prepared.kind, "value", None),
                        "ticket_id": prepared.ticket.id if prepared.ticket else None,
                        "needs_agent": bool(prepared.needs_agent),
                    },
                )
                if prepared.needs_agent:
                    self._processing.update(
                        key,
                        state=ProcessingState.AGENT_PENDING,
                        kind=prepared.kind.value,
                        ticket_id=prepared.ticket.id if prepared.ticket else None,
                        intent=prepared.intent,
                    )
                else:
                    self._processing.update(
                        key,
                        state=ProcessingState.COMPLETED,
                        kind=prepared.kind.value,
                        ticket_id=prepared.ticket.id if prepared.ticket else None,
                        reply=prepared.reply,
                    )

        # ---- Agent run: OUTSIDE any write transaction (no DB write lock) ----
        if prepared.needs_agent:
            self._emit(on_stage, "agent_started", {"trace_id": envelope.trace_id})
            run = self._workflow.run_agent(prepared)
            self._emit(on_stage, "agent_completed", {"trace_id": envelope.trace_id})
            # ---- Phase B: CAS-guarded decision application ----
            try:
                with txn(conn):
                    if not self._processing.advance(key, ProcessingState.AGENT_PENDING, ProcessingState.AGENT_COMPLETED):
                        # A concurrent duplicate already applied the decision.
                        return IngressResult(envelope=envelope, user=user, session=session, conversation=conversation, duplicate=True)
                    result = self._workflow.apply(envelope, user, session, conversation, prepared, run)
                    self._processing.update(key, state=ProcessingState.COMPLETED, reply=result.reply)
                    self._emit(
                        on_stage,
                        "completed",
                        {
                            "trace_id": envelope.trace_id,
                            "reply": getattr(result, "reply", None),
                            "ticket_id": getattr(getattr(result, "ticket", None), "id", None),
                        },
                    )
            except Exception as exc:
                try:
                    with txn(conn):
                        self._processing.update(key, state=ProcessingState.FAILED_RETRYABLE, error=str(exc)[:500])
                except Exception:
                    pass
                raise
        else:
            result = prepared.result
            self._emit(
                on_stage,
                "completed",
                {
                    "trace_id": envelope.trace_id,
                    "reply": getattr(result, "reply", None),
                    "ticket_id": getattr(getattr(result, "ticket", None), "id", None),
                },
            )

        if self._notifications is not None and self._auto_dispatch:
            self._notifications.dispatch()
        return IngressResult(
            envelope=envelope,
            user=user,
            session=session,
            conversation=conversation,
            duplicate=False,
            downstream=result,
        )

    # --- legacy single-transaction path (downstream callable only) ---

    def _process_legacy(self, envelope: InboundEnvelope, key: str) -> IngressResult:
        conn = self._idempotency._conn  # noqa: SLF001
        try:
            with txn(conn):
                if key and not self._idempotency.claim(key, envelope.trace_id):
                    user = self._identity.resolve(envelope.channel, envelope.channel_user_id)
                    session = self._sessions.find_or_create(user.id, envelope.channel, envelope.conversation_id)
                    self._trace_event(
                        envelope.trace_id,
                        "channel",
                        {"channel": envelope.channel, "message_id": envelope.message_id, "duplicate": True},
                    )
                    return IngressResult(envelope=envelope, user=user, session=session, duplicate=True)

                user = self._identity.resolve(envelope.channel, envelope.channel_user_id)
                session = self._sessions.find_or_create(user.id, envelope.channel, envelope.conversation_id)
                conversation = self._resolve_conversation(envelope)
                self._trace_event(
                    envelope.trace_id,
                    "channel",
                    {"channel": envelope.channel, "message_id": envelope.message_id, "text": envelope.text},
                )
                self._trace_event(
                    envelope.trace_id,
                    "identity",
                    {
                        "channel_identity": f"{envelope.channel}/{envelope.channel_user_id}",
                        "user_id": user.id,
                        "session_id": session.id,
                    },
                )
                downstream_result = None
                if self._downstream is not None:
                    downstream_result = self._downstream(envelope, user, session, conversation)
        except Exception:
            if key:
                self._release_key(key)
            raise

        if self._notifications is not None and self._auto_dispatch:
            self._notifications.dispatch()
        return IngressResult(
            envelope=envelope,
            user=user,
            session=session,
            conversation=conversation,
            duplicate=False,
            downstream=downstream_result,
        )

    def _resolve_conversation(self, envelope: InboundEnvelope):
        if self._conversations is None:
            return None
        chat_type = envelope.metadata.get("chat_type")
        if chat_type == "p2p":
            hint_type = ConversationType.DM
        elif chat_type == "group":
            hint_type = ConversationType.GROUP
        elif envelope.metadata.get("chat_id"):
            hint_type = ConversationType.GROUP
        else:
            hint_type = None
        return self._conversations.resolve(
            envelope.channel,
            envelope.conversation_id,
            hint_type=hint_type,
            hint_purpose=ConversationPurpose.REQUESTER,
        )

    def _release_key(self, key: str) -> None:
        try:
            with txn(self._idempotency._conn):  # noqa: SLF001
                self._idempotency._conn.execute(  # noqa: SLF001
                    "DELETE FROM processed_messages WHERE idempotency_key = ?", (key,)
                )
        except Exception:
            pass

    def _trace_event(self, trace_id: str, stage: str, payload: dict) -> None:
        if self._trace is not None:
            self._trace.event(trace_id, stage, payload)
