"""Message: a chat message stored per session (Phase 4 context building)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Message:
    id: str
    session_id: str
    user_id: str
    role: str  # "user" | "assistant"
    text: str
    trace_id: str | None = None
    created_at: datetime = field(default_factory=_now)
