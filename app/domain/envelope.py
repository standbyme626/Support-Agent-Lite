"""Unified inbound message envelope (channel-agnostic)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid4().hex[:12]}"


class InboundEnvelope(BaseModel):
    """Normalized message produced by a Channel Adapter.

    Invariant: carries channel identity only; canonical user resolution
    happens downstream.
    """

    channel: str
    message_id: str
    channel_user_id: str
    conversation_id: str
    text: str
    timestamp: datetime = Field(default_factory=_now)
    trace_id: str = Field(default_factory=lambda: new_id("trace_"))
    metadata: dict[str, Any] = Field(default_factory=dict)
