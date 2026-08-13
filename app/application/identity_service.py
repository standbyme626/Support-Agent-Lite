"""IdentityResolver: channel identity -> canonical user.

Phase 2 core. Proves invariant #1 (channel identity != canonical user)
and #8 (cross-channel continuation resolves through canonical identity).
"""
from __future__ import annotations

from uuid import uuid4

from app.domain.identity import ChannelIdentity, User
from app.infrastructure.repositories import ChannelIdentityRepository, UserRepository


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:10]}"


class IdentityResolver:
    """Resolves a (channel, channel_user_id) pair to a canonical User.

    If the channel identity is already bound, returns the existing user.
    Otherwise creates a new canonical user and binds the identity.
    """

    def __init__(self, users: UserRepository, identities: ChannelIdentityRepository) -> None:
        self._users = users
        self._identities = identities

    def resolve(self, channel: str, channel_user_id: str, display_name: str | None = None) -> User:
        existing = self._identities.find(channel, channel_user_id)
        if existing is not None:
            user = self._users.get(existing.user_id)
            if user is not None:
                return user
        user = self._users.create(
            User(id=new_id("user_"), display_name=display_name or channel_user_id)
        )
        self._identities.create(
            ChannelIdentity(
                id=new_id("ci_"),
                user_id=user.id,
                channel=channel,
                channel_user_id=channel_user_id,
            )
        )
        return user

    def bind(self, channel: str, channel_user_id: str, user_id: str) -> User:
        """Explicitly bind a channel identity to an existing canonical user.

        Used for cross-channel seeding (AC-05): wecom/zhangsan and
        feishu/ou_001 both bound to the same user_001.
        """
        existing = self._identities.find(channel, channel_user_id)
        if existing is not None:
            if existing.user_id != user_id:
                raise ValueError(
                    f"channel identity already bound to {existing.user_id}, "
                    f"cannot rebind to {user_id}"
                )
        else:
            self._identities.create(
                ChannelIdentity(
                    id=new_id("ci_"),
                    user_id=user_id,
                    channel=channel,
                    channel_user_id=channel_user_id,
                )
            )
        user = self._users.get(user_id)
        if user is None:
            raise KeyError(f"user not found: {user_id}")
        return user
