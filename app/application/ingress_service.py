"""IngressService: webhook entry for a channel.

Boundary (Phase 3): adapters normalize payloads; the service resolves
canonical identity, finds the session, applies message idempotency, and
dispatches to a downstream handler (added in Phase 4).

Adapters never touch tickets/RAG/workflow/memory; only this service
coordinates them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.adapters.base import ChannelAdapter
from app.application.identity_service import IdentityResolver
from app.application.session_service import SessionService
from app.domain.envelope import InboundEnvelope
from app.domain.identity import Session, User
from app.infrastructure.idempotency import IdempotencyStore


@dataclass
class IngressResult:
    envelope: InboundEnvelope
    user: User
    session: Session
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
        downstream: Callable[[InboundEnvelope, User, Session], object] | None = None,
    ) -> None:
        self._adapters = adapters
        self._identity = identity
        self._sessions = sessions
        self._idempotency = idempotency
        self._downstream = downstream

    def process(self, channel: str, payload: dict) -> IngressResult:
        adapter = self._adapters[channel]
        key = adapter.idempotency_key(payload)
        if key and self._idempotency.is_processed(key):
            envelope = adapter.build_inbound(payload)
            user = self._identity.resolve(envelope.channel, envelope.channel_user_id)
            session = self._sessions.find_or_create(user.id, envelope.channel, envelope.conversation_id)
            return IngressResult(envelope=envelope, user=user, session=session, duplicate=True)

        envelope = adapter.build_inbound(payload)
        user = self._identity.resolve(envelope.channel, envelope.channel_user_id)
        session = self._sessions.find_or_create(user.id, envelope.channel, envelope.conversation_id)

        downstream_result = None
        if self._downstream is not None:
            downstream_result = self._downstream(envelope, user, session)

        if key:
            self._idempotency.mark_processed(key, envelope.trace_id)
        return IngressResult(
            envelope=envelope,
            user=user,
            session=session,
            duplicate=False,
            downstream=downstream_result,
        )