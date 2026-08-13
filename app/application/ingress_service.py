"""IngressService: webhook entry for a channel.

Pipeline: adapter normalize -> atomic idempotency claim -> canonical
identity -> session -> conversation -> downstream (workflow) -> dispatch
notifications. The idempotency claim and the business effect share the
connection; a business failure rolls the claim back with it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.adapters.base import ChannelAdapter
from app.application.conversation_service import ConversationService
from app.application.identity_service import IdentityResolver
from app.application.notification_service import NotificationService
from app.application.session_service import SessionService
from app.domain.conversation import ConversationPurpose, ConversationType
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.infrastructure.idempotency import IdempotencyStore
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
    ) -> None:
        self._adapters = adapters
        self._identity = identity
        self._sessions = sessions
        self._idempotency = idempotency
        self._downstream = downstream
        self._trace = trace
        self._conversations = conversations
        self._notifications = notifications

    def process(self, channel: str, payload: dict) -> IngressResult:
        adapter = self._adapters[channel]
        envelope = adapter.build_inbound(payload)
        key = adapter.idempotency_key(payload)

        # One transaction for claim + identity + session + downstream:
        #  - concurrent duplicates see the winner's committed claim AND identity
        #  - a business failure rolls the claim back (message stays retryable)
        try:
            with txn(self._idempotency._conn):  # noqa: SLF001
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

        if self._notifications is not None:
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
