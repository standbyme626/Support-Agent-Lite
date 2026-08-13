"""Conversation: the conversation-level domain model (V2).

A Conversation is the first-class place where messages happen. It
carries channel, type (DM/GROUP), purpose (REQUESTER/OPERATOR/APPROVAL),
queue and location. Purpose is a business concept: Channel != Role.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationType(str, Enum):
    DM = "DM"
    GROUP = "GROUP"


class ConversationPurpose(str, Enum):
    REQUESTER = "REQUESTER"
    OPERATOR = "OPERATOR"
    APPROVAL = "APPROVAL"


@dataclass(slots=True)
class Conversation:
    id: str
    channel: str
    channel_conversation_id: str
    conversation_type: ConversationType
    purpose: ConversationPurpose
    queue: str | None = None
    location: str | None = None
    enabled: bool = True
    created_at: datetime = field(default_factory=_now)
