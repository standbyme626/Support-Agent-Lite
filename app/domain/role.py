"""Roles: canonical users get roles (requester/operator/approver).

One canonical user can hold multiple roles. Role is orthogonal to
channel: a WeCom identity and a Feishu identity of the same operator
resolve to the same canonical user with role=operator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(str, Enum):
    REQUESTER = "requester"
    OPERATOR = "operator"
    APPROVER = "approver"


@dataclass(slots=True)
class RoleAssignment:
    id: str
    user_id: str
    role: UserRole
    queue: str | None = None
    created_at: datetime = field(default_factory=_now)
