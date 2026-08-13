"""SessionService: session find-or-create scoped to a canonical user.

Invariant #2: Session belongs to a User after identity resolution.
"""
from __future__ import annotations

from uuid import uuid4

from app.domain.identity import Session
from app.infrastructure.repositories import SessionRepository


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:10]}"


class SessionService:
    def __init__(self, repo: SessionRepository) -> None:
        self._repo = repo

    def find_or_create(self, user_id: str, channel: str, channel_conversation_id: str) -> Session:
        for session in self._repo.list_by_user(user_id):
            if (
                session.channel == channel
                and session.channel_conversation_id == channel_conversation_id
            ):
                return session
        return self._repo.create(
            Session(
                id=new_id("sess_"),
                user_id=user_id,
                channel=channel,
                channel_conversation_id=channel_conversation_id,
            )
        )
