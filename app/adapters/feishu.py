"""Feishu channel adapter.

Inbound per official `im.message.receive_v1` event (verified against
https://open.feishu.cn/document/server-docs/im-v1/message/events/receive):
  - message_id is the idempotency field (NOT event_id)
  - chat_type p2p|group
  - content is a JSON string like {"text": "..."}
Legacy flat fixtures (event.message.text) stay supported for backward
compatibility with earlier tests.

Verification per official docs: url_verification challenge + event token
check; encrypted mode (Encrypt Key) decrypts AES-256-CBC payloads.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.adapters.base import ChannelAdapterError, HttpInbound, VerificationError
from app.domain.envelope import InboundEnvelope
from app.domain.outbound import ChannelCapability

_MENTION_RE = re.compile(r"@_user_\d+|@_all\b")


class FeishuAdapter:
    channel = "feishu"

    capabilities = frozenset({
        ChannelCapability.DM_INBOUND,
        ChannelCapability.GROUP_INBOUND,
        ChannelCapability.DM_OUTBOUND,
        ChannelCapability.GROUP_OUTBOUND,
        ChannelCapability.WEBHOOK_VERIFICATION,
    })

    def __init__(self, verification_token: str | None = None, encrypt_key: str | None = None) -> None:
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key

    # --- official HTTP handling ---

    def handle_http(self, method: str, query: dict[str, str], raw_body: bytes) -> HttpInbound:
        if method == "GET":
            return HttpInbound(challenge=query.get("challenge"), error=None)
        try:
            body: dict[str, Any] = json.loads(raw_body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChannelAdapterError(self.channel, "invalid_json", str(exc)) from exc

        if body.get("type") == "url_verification":
            token = body.get("token")
            if self._verification_token and token != self._verification_token:
                raise VerificationError(self.channel, "invalid_token", "url_verification token mismatch")
            return HttpInbound(challenge={"challenge": body.get("challenge", "")})

        if "encrypt" in body:
            decrypted = self._decrypt(str(body["encrypt"]))
            body = json.loads(decrypted)

        if self._verification_token:
            header_token = body.get("header", {}).get("token")
            if header_token != self._verification_token:
                raise VerificationError(self.channel, "invalid_token", "event token mismatch")
        return HttpInbound(payload=body)

    def _decrypt(self, encrypted: str) -> str:
        if not self._encrypt_key:
            raise VerificationError(self.channel, "missing_encrypt_key", "event encrypted but no Encrypt Key configured")
        key = base64.b64decode(self._encrypt_key + "=")
        ciphertext = base64.b64decode(encrypted)
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        pad = plaintext[-1]
        return plaintext[:-pad].decode("utf-8")

    @staticmethod
    def encrypt_for_test(plaintext: str, encrypt_key: str) -> str:
        """AES-256-CBC + PKCS7, matching the official Encrypt Key scheme."""
        import os

        key = base64.b64decode(encrypt_key + "=")
        raw = plaintext.encode("utf-8")
        pad_len = 16 - (len(raw) % 16)
        raw += bytes([pad_len]) * pad_len
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        encryptor = cipher.encryptor()
        return base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode("utf-8")

    # --- idempotency (official: message_id first, never event_id first) ---

    def idempotency_key(self, payload: dict[str, Any]) -> str | None:
        message_id = payload.get("event", {}).get("message", {}).get("message_id")
        if message_id:
            return f"{self.channel}:{message_id}"
        message_id_legacy = payload.get("message", {}).get("message_id")
        if message_id_legacy:
            return f"{self.channel}:{message_id_legacy}"
        event_id = payload.get("event_id") or payload.get("header", {}).get("event_id")
        if event_id:
            return f"{self.channel}:{event_id}"
        return None

    # --- parsing (official shape + legacy compat) ---

    def build_inbound(self, payload: dict[str, Any]) -> InboundEnvelope:
        header = payload.get("header", {})
        event = payload.get("event", payload)
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        message = event.get("message", payload.get("message", {}))
        channel_user_id = str(sender_id.get("open_id") or payload.get("session_id") or "")
        text = self._extract_text(message, payload)
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
                message="feishu payload missing message content",
            )
        message_id = message.get("message_id") or payload.get("message_id")
        event_id = event.get("event_id") or header.get("event_id") or payload.get("event_id")
        chat_id = message.get("chat_id") or event.get("chat_id") or payload.get("chat_id")
        conversation_id = str(payload.get("conversation_id") or chat_id or channel_user_id)
        return InboundEnvelope(
            channel=self.channel,
            message_id=str(message_id or event_id or ""),
            channel_user_id=channel_user_id,
            conversation_id=conversation_id,
            text=text,
            metadata={
                "event_id": event_id,
                "chat_id": chat_id,
                "chat_type": message.get("chat_type"),
                "tenant_key": header.get("tenant_key") or event.get("tenant_key") or payload.get("tenant_key"),
            },
        )

    @staticmethod
    def _extract_text(message: dict[str, Any], payload: dict[str, Any]) -> str:
        text = str(payload.get("text") or payload.get("Content") or "")
        if not text:
            raw = message.get("text")
            if isinstance(raw, str) and raw:
                text = raw
            else:
                content = message.get("content")
                if isinstance(content, str) and content:
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            text = str(parsed.get("text") or "")
                    except ValueError:
                        text = content
        # Group @-mentions arrive as @_user_N placeholders; they carry no
        # business meaning and must not pollute ticket titles / commands.
        return _MENTION_RE.sub("", text).strip()
