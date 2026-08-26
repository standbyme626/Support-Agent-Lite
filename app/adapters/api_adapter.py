"""ApiChannelAdapter: minimal JSON channel for the streaming demo API.

Boundary respected: normalizes payloads to InboundEnvelope ONLY (no
workflow/RAG access). Used by GET /api/chat/stream (C5 SSE); the channel
webhooks stay untouched.

Idempotency contract: clients SHOULD send a stable `message_id` for
exactly-once retry semantics; without one, each delivery gets a fresh
key (re-sending the same text is a legitimate new message — hashing
text would wrongly dedupe "还是不行").
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.adapters.base import ChannelAdapterError, HttpInbound
from app.domain.envelope import InboundEnvelope


class ApiChannelAdapter:
    channel = "api"

    capabilities = frozenset()

    def build_inbound(self, payload: dict[str, Any]) -> InboundEnvelope:
        text = str(payload.get("text") or "").strip()
        user = str(payload.get("channel_user_id") or "").strip()
        if not text:
            raise ChannelAdapterError(self.channel, "empty_text", "text is required")
        if not user:
            raise ChannelAdapterError(self.channel, "missing_user", "channel_user_id is required")
        return InboundEnvelope(
            channel=self.channel,
            message_id=str(payload.get("message_id") or f"api-{uuid4().hex[:12]}"),
            channel_user_id=user,
            conversation_id=str(payload.get("conversation_id") or f"api-{user}"),
            text=text,
            metadata={"display_name": str(payload.get("name") or user)},
        )

    def idempotency_key(self, payload: dict[str, Any]) -> str | None:
        message_id = payload.get("message_id")
        return f"api:{message_id}" if message_id else f"api:auto-{uuid4().hex}"

    def handle_http(self, method: str, query: dict, raw_body: bytes) -> HttpInbound:
        raise ChannelAdapterError(
            self.channel, "wrong_endpoint", "api channel is served by /api/chat/stream"
        )
