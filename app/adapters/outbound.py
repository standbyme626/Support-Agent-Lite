"""Protocol-accurate outbound clients (Mock network, not protocol).

Request construction follows official documentation recorded in
docs/CHANNEL_PROTOCOL_MATRIX.md. The default transport only records; the
RealHttpTransport is the sole swap point for future real credentials.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from app.adapters.transports import HttpTransport, RealHttpTransport
from app.application.notification_service import (
    ChannelCapabilityDisabled,
    ChannelOutboundClient,
)
from app.domain.outbound import OutboundMessage

REAL_NETWORK_ENV = "REAL_CHANNEL_NETWORK"


def transport_from_env() -> HttpTransport:
    if os.environ.get(REAL_NETWORK_ENV, "").lower() in ("1", "true", "yes"):
        return RealHttpTransport()
    return HttpTransport()


def _network_enabled() -> bool:
    return os.environ.get(REAL_NETWORK_ENV, "").lower() in ("1", "true", "yes")


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""


class FeishuOutboundClient(ChannelOutboundClient):
    """Feishu outbound per official docs:

    POST https://open.feishu.cn/open-apis/im/v1/messages
      ?receive_id_type=open_id|chat_id
    Authorization: Bearer <tenant_access_token>
    Content-Type: application/json; charset=utf-8
    body {receive_id, msg_type: "text", content: '{"text":"..."}', uuid}
    """

    channel = "feishu"
    BASE_URL = "https://open.feishu.cn"
    SEND_PATH = "/open-apis/im/v1/messages"
    TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"

    def __init__(self, config: FeishuConfig | None = None, transport: HttpTransport | None = None) -> None:
        self._config = config or FeishuConfig()
        self._transport = transport or transport_from_env()

    def deliver(self, message: OutboundMessage) -> tuple[bool, str, str | None]:
        if message.target.kind.value == "user":
            receive_id_type = "open_id"
        else:
            receive_id_type = "chat_id"
        content = json.dumps({"text": message.text}, ensure_ascii=False)
        body = {
            "receive_id": message.target.target_id,
            "msg_type": "text",
            "content": content,
        }
        token = self._fetch_tenant_token()
        if not token and _network_enabled():
            return False, "TOKEN_FETCH_FAILED", "tenant_access_token not obtained"
        headers = {
            "Authorization": f"Bearer {token or 'SIMULATED_TENANT_TOKEN'}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            response = self._transport.post(
                f"{self.BASE_URL}{self.SEND_PATH}",
                headers=headers,
                params={"receive_id_type": receive_id_type},
                json=body,
            )
        except Exception as exc:
            return False, "TRANSPORT_ERROR", str(exc)
        if isinstance(response, dict) and response.get("code") not in (None, 0):
            return False, f"FEISHU_CODE_{response.get('code')}", response.get("msg")
        return True, "SENT_FEISHU", None

    def _fetch_tenant_token(self) -> str:
        if _network_enabled() and self._config.app_id and self._config.app_secret:
            response = self._transport.post(
                f"{self.BASE_URL}{self.TOKEN_PATH}",
                json={"app_id": self._config.app_id, "app_secret": self._config.app_secret},
            )
            return str(response.get("tenant_access_token", ""))
        return ""


@dataclass
class WeComConfig:
    corp_id: str = ""
    corp_secret: str = ""
    agent_id: str = ""


class WeComOutboundClient(ChannelOutboundClient):
    """WeCom outbound per official docs:

    - GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid&corpsecret
    - POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=...
      body {touser, msgtype:"text", agentid, text:{content}, safe}
    - POST https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token=...
      body {chatid, msgtype:"text", text:{content}}
    """

    channel = "wecom"
    BASE_URL = "https://qyapi.weixin.qq.com"
    TOKEN_PATH = "/cgi-bin/gettoken"
    SEND_PATH = "/cgi-bin/message/send"
    APPCHAT_PATH = "/cgi-bin/appchat/send"

    def __init__(self, config: WeComConfig | None = None, transport: HttpTransport | None = None) -> None:
        self._config = config or WeComConfig()
        self._transport = transport or transport_from_env()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def deliver(self, message: OutboundMessage) -> tuple[bool, str, str | None]:
        token = self._get_access_token()
        if not token and _network_enabled():
            return False, "TOKEN_FETCH_FAILED", "access_token not obtained"
        access_token = token or "SIMULATED_ACCESS_TOKEN"
        if message.target.kind.value == "user":
            path = self.SEND_PATH
            body: dict[str, Any] = {
                "touser": message.target.target_id,
                "msgtype": "text",
                "agentid": int(self._config.agent_id or 0),
                "text": {"content": message.text},
                "safe": 0,
            }
        else:
            path = self.APPCHAT_PATH
            body = {
                "chatid": message.target.target_id,
                "msgtype": "text",
                "text": {"content": message.text},
                "safe": 0,
            }
        try:
            response = self._transport.post(
                f"{self.BASE_URL}{path}",
                params={"access_token": access_token},
                json=body,
            )
        except Exception as exc:
            return False, "TRANSPORT_ERROR", str(exc)
        if isinstance(response, dict) and response.get("errcode", 0) != 0:
            return False, f"WECOM_ERRCODE_{response.get('errcode')}", response.get("errmsg")
        return True, "SENT_WECOM", None

    def _get_access_token(self) -> str:
        if self._token and self._token_expires_at > 0:
            return self._token
        if _network_enabled() and self._config.corp_id and self._config.corp_secret:
            response = self._transport.get(
                f"{self.BASE_URL}{self.TOKEN_PATH}",
                params={"corpid": self._config.corp_id, "corpsecret": self._config.corp_secret},
            )
            self._token = str(response.get("access_token", ""))
            self._token_expires_at = float(response.get("expires_in", 7200)) - 30
            return self._token
        return ""
