"""TargetResolver: notification target -> concrete DeliveryTarget.

Ticket context may span many conversations (requester group, requester
DM, operator group, approver DM). Targets are resolved here; nothing
guesses an implicit "ticket.session".
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.conversation import ConversationType
from app.domain.outbound import DeliveryTarget, TargetKind
from app.domain.ticket import Ticket
from app.infrastructure.repositories import ChannelIdentityRepository, ConversationRepository, SessionRepository


@dataclass(frozen=True)
class ResolvedTarget:
    delivery: DeliveryTarget | None
    reason: str


class TargetResolver:
    def __init__(
        self,
        conversations: ConversationRepository,
        sessions: SessionRepository,
        identities: ChannelIdentityRepository | None = None,
    ) -> None:
        self._conversations = conversations
        self._sessions = sessions
        self._identities = identities

    def requester_public(self, ticket: Ticket) -> ResolvedTarget:
        """The requester conversation where the ticket was created."""
        if not ticket.source_conversation_id:
            return ResolvedTarget(None, "no_source_conversation")
        conversation = self._conversations.find_by_channel_conversation_id(ticket.source_conversation_id)
        if conversation is None:
            return ResolvedTarget(None, "source_conversation_not_registered")
        if not conversation.enabled:
            return ResolvedTarget(None, "conversation_disabled")
        return ResolvedTarget(
            DeliveryTarget(conversation.channel, TargetKind.CONVERSATION, conversation.channel_conversation_id),
            f"requester_public:{conversation.id}",
        )

    def requester_private(self, ticket: Ticket, requester_user_id: str) -> ResolvedTarget:
        """The requester's DM conversation on the ticket's source channel.

        DM notifications are delivered to the channel USER identity
        (open_id / userid), not to a chat id: wecom message/send uses
        touser, feishu send uses receive_id_type=open_id for p2p.
        """
        source = self._conversations.find_by_channel_conversation_id(ticket.source_conversation_id)
        channel = source.channel if source else "wecom"
        sessions = self._sessions.list_by_user(requester_user_id)
        for session in sessions:
            if session.channel != channel:
                continue
            conversation = self._conversations.find(session.channel, session.channel_conversation_id)
            if conversation is not None and conversation.conversation_type == ConversationType.DM:
                target_id = self._channel_user_id(channel, requester_user_id)
                if target_id is None:
                    target_id = session.channel_conversation_id  # wecom convention: DM id == user id
                return ResolvedTarget(
                    DeliveryTarget(channel, TargetKind.USER, target_id),
                    f"requester_dm:{session.id}",
                )
        return ResolvedTarget(None, "no_requester_dm_conversation")

    def _channel_user_id(self, channel: str, user_id: str) -> str | None:
        if self._identities is None:
            return None
        identity = self._identities.find_by_user_on_channel(channel, user_id)
        return identity.channel_user_id if identity else None

    def operator_queue(self, queue: str | None) -> ResolvedTarget:
        conversation = self._conversations.find_operator_conversation(queue)
        if conversation is None:
            return ResolvedTarget(None, "no_operator_conversation")
        return ResolvedTarget(
            DeliveryTarget(conversation.channel, TargetKind.CONVERSATION, conversation.channel_conversation_id),
            f"operator_queue:{queue or 'default'}",
        )

    def action_origin(self, channel: str, conversation_id: str) -> ResolvedTarget:
        return ResolvedTarget(
            DeliveryTarget(channel, TargetKind.CONVERSATION, conversation_id),
            "action_origin",
        )

    def approver(self) -> ResolvedTarget:
        conversation = self._conversations.find_approval_conversation()
        if conversation is None:
            return ResolvedTarget(None, "no_approval_conversation")
        return ResolvedTarget(
            DeliveryTarget(conversation.channel, TargetKind.CONVERSATION, conversation.channel_conversation_id),
            "approval_conversation",
        )

    def by_conversation(self, channel: str, conversation_id: str) -> ResolvedTarget:
        conversation = self._conversations.find(channel, conversation_id)
        if conversation is None:
            return ResolvedTarget(
                DeliveryTarget(channel, TargetKind.CONVERSATION, conversation_id),
                "unregistered_conversation",
            )
        if not conversation.enabled:
            return ResolvedTarget(None, "conversation_disabled")
        return ResolvedTarget(
            DeliveryTarget(conversation.channel, TargetKind.CONVERSATION, conversation.channel_conversation_id),
            f"conversation:{conversation.id}",
        )
