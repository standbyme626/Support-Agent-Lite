"""Memory: stable facts derived from closed tickets, keyed to the user.

Invariant (DOMAIN_MODEL): Memory is derived from closed tickets and
belongs to the canonical user. `summary` memories carry the final ticket
summary; `stable_fact` memories carry reusable facts for next-session
recall (AC-09 / AC-10).

`source` marks HOW the ticket was closed (v2.md §49): only a
requester-confirmed closure proves the resolution was real, so confirmed
facts outrank force-closed ones at recall time. Empty string = legacy
row written before the marker existed (neutral at ranking).
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


class MemorySource(str, Enum):
    """Closure provenance of a memory (v2.md §49 semantics)."""

    CONFIRMED_CLOSURE = "confirmed_closure"
    FORCE_CLOSED = "force_closed"


@dataclass(slots=True)
class Memory:
    id: str
    user_id: str
    ticket_id: str  # source ticket
    kind: MemoryKind
    fact: str
    confidence: float
    source: str = ""  # MemorySource value; "" = legacy/neutral
    created_at: datetime = field(default_factory=_now)


@dataclass(slots=True)
class SessionCompaction:
    """Rolling summary of a session's older messages (pi compaction 同款).

    pi CompactionEntry 对应物:summary 替换更早历史进入上下文,
    first_kept_message_id 之后的近期消息原文保留(retained tail)。
    追加式存储:上下文构建只读最新一条,历史条目留作审计。
    """

    id: str
    session_id: str
    summary: str
    first_kept_message_id: str | None  # None = whole session summarized
    messages_compacted: int
    chars_before: int
    summarizer: str  # "llm" | "deterministic"
    created_at: datetime = field(default_factory=_now)
