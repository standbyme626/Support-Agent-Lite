"""RoleService: canonical users carry roles (requester/operator/approver).

Role and channel are orthogonal. Same canonical user across channels
keeps one role set.
"""
from __future__ import annotations

from uuid import uuid4

from app.domain.role import RoleAssignment, UserRole
from app.infrastructure.repositories import RoleRepository


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:10]}"


class RoleService:
    def __init__(self, repo: RoleRepository) -> None:
        self._repo = repo

    def ensure_role(self, user_id: str, role: UserRole, queue: str | None = None) -> RoleAssignment:
        if self._repo.has_role(user_id, role):
            return RoleAssignment(id="", user_id=user_id, role=role, queue=queue)
        return self._repo.create(
            RoleAssignment(id=new_id("role_"), user_id=user_id, role=role, queue=queue)
        )

    def has_role(self, user_id: str, role: UserRole) -> bool:
        return self._repo.has_role(user_id, role)

    def primary_role(self, user_id: str) -> str:
        roles = self._repo.list_by_user(user_id)
        if not roles:
            return ""
        return roles[0].role.value

    def roles(self, user_id: str) -> list[RoleAssignment]:
        return self._repo.list_by_user(user_id)
