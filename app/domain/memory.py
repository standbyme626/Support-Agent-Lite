"""Memory: stable facts derived from closed tickets, keyed to the user.

Invariant (DOMAIN_MODEL): Memory is derived from closed tickets and
belongs to the canonical user. `summary` memories carry the final ticket
summary; `stable_fact` memories carry reusable facts for next-session
recall (AC-09 / AC-10).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryKind(str, Enum):
    STABLE_FACT = "stable_fact"
    SUMMARY = "summary"


@dataclass(slots=True)
class Memory:
    id: str
    user_id: str
    ticket_id: str  # source ticket
    kind: MemoryKind
    fact: str
    confidence: float
    created_at: datetime = field(default_factory=_now)
