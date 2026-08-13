"""Canonical identity, channel identities and sessions."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class User:
    """Canonical user. Cross-channel continuation resolves here."""

    id: str
    display_name: str
    created_at: datetime = field(default_factory=_now)


@dataclass(slots=True)
class ChannelIdentity:
    """Binding between a channel user id and a canonical user.

    Invariant: belongs to a User. UNIQUE(channel, channel_user_id).
    """

    id: str
    user_id: str
    channel: str
    channel_user_id: str
    created_at: datetime = field(default_factory=_now)


@dataclass(slots=True)
class Session:
    """A conversation session.

    Invariant: belongs to a User after identity resolution.
    Session is NOT the user identity. Session is NOT memory.
    """

    id: str
    user_id: str
    channel: str
    channel_conversation_id: str
    created_at: datetime = field(default_factory=_now)
