"""Channel adapter layer: Raw payload -> InboundEnvelope only.

Boundary (invariant): adapters MUST NOT access Ticket / RAG / Workflow /
Memory. They only normalize inbound payloads and derive idempotency keys.
"""
from __future__ import annotations

from typing import Any, Protocol

from app.domain.envelope import InboundEnvelope


class ChannelAdapterError(ValueError):
    """Raised when an inbound payload is malformed or unparseable."""

    def __init__(self, channel: str, code: str, message: str) -> None:
        super().__init__(message)
        self.channel = channel
        self.code = code


class ChannelAdapter(Protocol):
    channel: str

    def idempotency_key(self, payload: dict[str, Any]) -> str | None: ...
    def build_inbound(self, payload: dict[str, Any]) -> InboundEnvelope: ...
