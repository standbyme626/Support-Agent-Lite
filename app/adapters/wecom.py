"""WeCom channel adapter.

ADAPTED from legacy payload parsing (MsgId / FromUserName / Content).
Only raw -> InboundEnvelope; no downstream access.
"""
from __future__ import annotations

from typing import Any

from app.adapters.base import ChannelAdapterError
from app.domain.envelope import InboundEnvelope


class WeComAdapter:
    channel = "wecom"

    def idempotency_key(self, payload: dict[str, Any]) -> str | None:
        msg_id = payload.get("MsgId")
        if msg_id:
            return f"{self.channel}:{msg_id}"
        session_id = payload.get("session_id") or payload.get("FromUserName")
        create_time = payload.get("CreateTime")
        if session_id and create_time:
            return f"{self.channel}:{session_id}:{create_time}"
        return None

    def build_inbound(self, payload: dict[str, Any]) -> InboundEnvelope:
        channel_user_id = str(payload.get("session_id") or payload.get("FromUserName") or "")
        text = str(payload.get("Content") or payload.get("text") or "")
        if not channel_user_id:
            raise ChannelAdapterError(
                channel=self.channel,
                code="missing_channel_user_id",
                message="wecom payload missing FromUserName/session_id",
            )
        if not text:
            raise ChannelAdapterError(
                channel=self.channel,
                code="missing_text",
                message="wecom payload missing Content/text",
            )
        conversation_id = str(payload.get("conversation_id") or channel_user_id)
        msg_id = payload.get("MsgId")
        return InboundEnvelope(
            channel=self.channel,
            message_id=str(msg_id or f"{channel_user_id}:{payload.get('CreateTime')}"),
            channel_user_id=channel_user_id,
            conversation_id=conversation_id,
            text=text,
            metadata={
                "agent_id": payload.get("AgentID"),
                "create_time": payload.get("CreateTime"),
            },
        )
