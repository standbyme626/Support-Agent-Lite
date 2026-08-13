"""Channel adapter layer: Raw payload -> InboundEnvelope only.

Boundary (invariant): adapters MUST NOT access Ticket / RAG / Workflow /
Memory. They only normalize inbound payloads, derive idempotency keys,
verify channel HTTP callbacks and declare channel capabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.envelope import InboundEnvelope
from app.domain.outbound import ChannelCapability


class ChannelAdapterError(ValueError):
    """Raised when an inbound payload is malformed or unparseable."""

    def __init__(self, channel: str, code: str, message: str) -> None:
        super().__init__(message)
        self.channel = channel
        self.code = code


class VerificationError(ChannelAdapterError):
    pass


@dataclass
class HttpInbound:
    """Result of channel HTTP processing.

    - `challenge` set: this request was a URL/verification challenge.
    - `payload` set: verified, normalized payload ready for build_inbound.
    """

    challenge: Any | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None


class ChannelAdapter(Protocol):
    channel: str
    capabilities: frozenset[ChannelCapability]

    def idempotency_key(self, payload: dict[str, Any]) -> str | None: ...
    def build_inbound(self, payload: dict[str, Any]) -> InboundEnvelope: ...
