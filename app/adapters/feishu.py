"""Feishu channel adapter.

ADAPTED from legacy payload parsing (event.message.message_id,
sender.sender_id.open_id). Only raw -> InboundEnvelope; no downstream access.
"""
from __future__ import annotations

from typing import Any

from app.adapters.base import ChannelAdapterError
from app.domain.envelope import InboundEnvelope


class FeishuAdapter:
    channel = "feishu"

    def idempotency_key(self, payload: dict[str, Any]) -> str | None:
        message_id = payload.get("event", {}).get("message", {}).get("message_id")
        if message_id:
            return f"{self.channel}:{message_id}"
        event_id = payload.get("event_id")
        if event_id:
            return f"{self.channel}:{event_id}"
        return None

    def build_inbound(self, payload: dict[str, Any]) -> InboundEnvelope:
        event = payload.get("event", {})
        sender_id = event.get("sender", {}).get("sender_id", {})
        channel_user_id = str(sender_id.get("open_id") or payload.get("session_id") or "")
        text = str(event.get("message", {}).get("text") or payload.get("text") or "")
        if not channel_user_id:
            raise ChannelAdapterError(
                channel=self.channel,
                code="missing_channel_user_id",
                message="feishu payload missing sender.sender_id.open_id",
            )
        if not text:
            raise ChannelAdapterError(
                channel=self.channel,
                code="missing_text",
                message="feishu payload missing event.message.text",
            )
        message_id = event.get("message", {}).get("message_id")
        event_id = payload.get("event_id")
        conversation_id = str(payload.get("conversation_id") or event.get("chat_id") or channel_user_id)
        return InboundEnvelope(
            channel=self.channel,
            message_id=str(message_id or event_id or ""),
            channel_user_id=channel_user_id,
            conversation_id=conversation_id,
            text=text,
            metadata={
                "event_id": event_id,
                "chat_id": event.get("chat_id"),
                "tenant_key": payload.get("tenant_key"),
            },
        )
