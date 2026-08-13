"""WeCom channel adapter.

Official protocol (verified against developer.work.weixin.qq.com):
  - callback requires URL + Token + EncodingAESKey (path 90930)
  - GET URL verification: msg_signature/timestamp/nonce/echostr, respond
    with decrypted echostr plaintext within 1s
  - POST: query params msg_signature/timestamp/nonce + encrypted XML body
    (ToUserName/AgentID/Encrypt); decrypt to the plaintext message XML
  - text message XML (path 90239): ToUserName, FromUserName, CreateTime,
    MsgType, Content, MsgId, AgentID — NO ChatId field, therefore group
    conversation context is NOT derivable from the official format
    (GROUP_INBOUND: UNSUPPORTED).

The legacy HMAC scheme is NOT reproduced.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import xml.etree.ElementTree as ET
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.adapters.base import ChannelAdapterError, HttpInbound, VerificationError
from app.domain.envelope import InboundEnvelope
from app.domain.outbound import ChannelCapability


class WeComAdapter:
    channel = "wecom"

    # Official-doc-backed capabilities only. GROUP_INBOUND is unsupported:
    # the official text message callback format carries no chat id.
    capabilities = frozenset({
        ChannelCapability.DM_INBOUND,
        ChannelCapability.DM_OUTBOUND,
        ChannelCapability.GROUP_OUTBOUND,
        ChannelCapability.WEBHOOK_VERIFICATION,
    })

    def __init__(self, token: str | None = None, encoding_aes_key: str | None = None) -> None:
        self._token = token
        self._encoding_aes_key = encoding_aes_key

    # --- official HTTP handling ---

    def handle_http(self, method: str, query: dict[str, str], raw_body: bytes) -> HttpInbound:
        if method == "GET" and query.get("echostr"):
            msg_signature = query.get("msg_signature", "")
            timestamp = query.get("timestamp", "")
            nonce = query.get("nonce", "")
            echostr = query.get("echostr", "")
            self._verify_signature(msg_signature, timestamp, nonce, echostr)
            return HttpInbound(challenge=self._decrypt(echostr))
        if method == "POST":
            stripped = raw_body.lstrip()
            if not stripped.startswith(b"<"):
                # Local simulation mode: plain JSON payloads pass through.
                try:
                    return HttpInbound(payload=json.loads(raw_body.decode("utf-8") or "{}"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ChannelAdapterError("wecom", "invalid_json", str(exc)) from exc
            msg_signature = query.get("msg_signature", "")
            timestamp = query.get("timestamp", "")
            nonce = query.get("nonce", "")
            encrypt = self._extract_encrypt(raw_body)
            self._verify_signature(msg_signature, timestamp, nonce, encrypt)
            plaintext = self._decrypt(encrypt)
            return HttpInbound(payload=self._xml_to_dict(plaintext))
        raise ChannelAdapterError(self.channel, "unsupported_http", f"{method} not supported")

    def _verify_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> None:
        if not self._token:
            return  # no token configured -> verification disabled (local simulation)
        expected = self.signature(self._token, timestamp, nonce, encrypt)
        if expected != msg_signature:
            raise VerificationError(self.channel, "signature_mismatch", f"expected {expected}")

    @staticmethod
    def signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
        """msg_signature = sha1(sort([token, timestamp, nonce, encrypt])).

        Algorithm per the official 回调配置 / 加解密方案 docs.
        """
        parts = "".join(sorted([token, timestamp, nonce, encrypt]))
        return hashlib.sha1(parts.encode("utf-8")).hexdigest()

    def _decrypt(self, encrypted: str) -> str:
        if not self._encoding_aes_key:
            raise VerificationError(self.channel, "missing_encoding_aes_key", "EncodingAESKey not configured")
        key = base64.b64decode(self._encoding_aes_key + "=")
        ciphertext = base64.b64decode(encrypted)
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        content = plaintext[16:]  # strip 16-byte random
        msg_len = struct.unpack(">I", content[:4])[0]
        msg = content[4 : 4 + msg_len]
        return msg.decode("utf-8")

    @staticmethod
    def encrypt_for_test(plaintext: str, encoding_aes_key: str, receive_id: str) -> tuple[str, str, str]:
        """Encrypt a message XML per the official scheme for fixtures.

        Returns (encrypt_b64, timestamp, nonce)."""
        import os

        key = base64.b64decode(encoding_aes_key + "=")
        random = os.urandom(16)
        msg_bytes = plaintext.encode("utf-8")
        raw = random + struct.pack(">I", len(msg_bytes)) + msg_bytes + receive_id.encode("utf-8")
        pad_len = 16 - (len(raw) % 16)
        raw += bytes([pad_len]) * pad_len
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        encryptor = cipher.encryptor()
        encrypted = base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode("utf-8")
        return encrypted, "13500001234", "123412323"

    @staticmethod
    def _extract_encrypt(raw_body: bytes) -> str:
        try:
            root = ET.fromstring(raw_body.decode("utf-8"))
        except (ET.ParseError, UnicodeDecodeError) as exc:
            raise ChannelAdapterError("wecom", "invalid_xml", str(exc)) from exc
        encrypt = root.findtext("Encrypt")
        if not encrypt:
            raise ChannelAdapterError("wecom", "missing_encrypt", "POST body has no Encrypt node")
        return encrypt

    @staticmethod
    def _xml_to_dict(xml_text: str) -> dict[str, Any]:
        root = ET.fromstring(xml_text)
        return {child.tag: (child.text or "") for child in root}

    # --- idempotency (MsgId, fallback session+time) ---

    def idempotency_key(self, payload: dict[str, Any]) -> str | None:
        msg_id = payload.get("MsgId")
        if msg_id:
            return f"{self.channel}:{msg_id}"
        session_id = payload.get("session_id") or payload.get("FromUserName")
        create_time = payload.get("CreateTime")
        if session_id and create_time:
            return f"{self.channel}:{session_id}:{create_time}"
        return None

    # --- parsing (official text message fields) ---

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

    @staticmethod
    def group_inbound_support_note() -> str:
        return (
            "UNSUPPORTED: official text message callback format (path 90239) "
            "contains ToUserName/FromUserName/CreateTime/MsgType/Content/MsgId/AgentID "
            "but no chat id — group conversation context cannot be derived."
        )
