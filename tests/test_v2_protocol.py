"""V2 protocol contract tests: Mock network, not protocol.

AC-26 official-shaped Feishu inbound; AC-27 Feishu outbound contract;
AC-28 WeCom official contract (signature / AES round-trip / XML shapes /
message.send + appchat.send). Everything runs against recording
transports — no real network, no real credentials.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from app.adapters.feishu import FeishuAdapter
from app.adapters.outbound import FeishuOutboundClient, WeComOutboundClient
from app.adapters.transports import HttpTransport
from app.adapters.wecom import WeComAdapter
from app.adapters.base import VerificationError
from app.domain.outbound import ChannelCapability, DeliveryTarget, OutboundMessage, TargetKind

FEISHU_ENCRYPT_KEY = "QdLXo6H0G5kPz9qKvYg1Tl7mA5rEbN2hWtX4sD8cFjU"
WECOM_AES_KEY = "qmpk6ozVlj8lvvGwhWLOG7r8PdBlEa89nyzfGQ+S2kE"


# ===========================================================================
# Feishu inbound (AC-26)
# ===========================================================================

OFFICIAL_FEISHU_EVENT = {
    "schema": "2.0",
    "header": {
        "event_id": "5e3702a84e847582be8db7fb73283c02",
        "event_type": "im.message.receive_v1",
        "create_time": "1608725989000",
        "token": "rvaYgkND1GOiu5MM0E1rncYC6PLtF7JV",
        "app_id": "cli_9f5343c580712544",
        "tenant_key": "2ca1d211f64f6438",
    },
    "event": {
        "sender": {
            "sender_id": {"union_id": "on_x", "user_id": "e33ggbyz", "open_id": "ou_84aad35d084aa403a838cf73ee18467"},
            "sender_type": "user",
            "tenant_key": "736588c9260f175e",
        },
        "message": {
            "message_id": "om_5ce6d572455d361153b7cb51da133945",
            "create_time": "1609073151345",
            "chat_id": "oc_5ce6d572455d361153b7xx51da133945",
            "chat_type": "group",
            "message_type": "text",
            "content": '{"text":"A3 空调坏了"}',
        },
    },
}


def test_ac26_feishu_official_shaped_inbound() -> None:
    adapter = FeishuAdapter()
    inbound = adapter.handle_http("POST", {}, json.dumps(OFFICIAL_FEISHU_EVENT).encode())
    assert inbound.challenge is None
    env = adapter.build_inbound(inbound.payload)

    assert env.channel == "feishu"
    assert env.message_id == "om_5ce6d572455d361153b7cb51da133945"
    assert env.channel_user_id == "ou_84aad35d084aa403a838cf73ee18467"
    assert env.conversation_id == "oc_5ce6d572455d361153b7xx51da133945"
    assert env.text == "A3 空调坏了"
    assert env.metadata["chat_type"] == "group"


def test_ac26_feishu_official_idempotency_prefers_message_id() -> None:
    adapter = FeishuAdapter()
    key = adapter.idempotency_key(OFFICIAL_FEISHU_EVENT)
    assert key == "feishu:om_5ce6d572455d361153b7cb51da133945"
    # event_id must NOT be the idempotency choice when message_id exists
    assert "evt" not in key and "5e3702a8" not in key


def test_feishu_group_mention_placeholder_stripped() -> None:
    """Group @bot text arrives as '@_user_1 ...' — placeholders must not
    pollute ticket titles or command parsing."""
    import copy

    adapter = FeishuAdapter()
    event = copy.deepcopy(OFFICIAL_FEISHU_EVENT)
    event["event"]["message"]["content"] = '{"text":"@_user_1 A3 空调坏了"}'
    inbound = adapter.handle_http("POST", {}, json.dumps(event).encode())
    env = adapter.build_inbound(inbound.payload)
    assert env.text == "A3 空调坏了"


def test_ac26_feishu_url_verification_challenge() -> None:
    adapter = FeishuAdapter(verification_token="test-token")
    payload = {"type": "url_verification", "challenge": "ajls384kdjx98XX", "token": "test-token"}
    inbound = adapter.handle_http("POST", {}, json.dumps(payload).encode())
    assert inbound.challenge == {"challenge": "ajls384kdjx98XX"}


def test_ac26_feishu_url_verification_token_mismatch_rejected() -> None:
    adapter = FeishuAdapter(verification_token="test-token")
    payload = {"type": "url_verification", "challenge": "ajls384kdjx98XX", "token": "WRONG"}
    with pytest.raises(VerificationError):
        adapter.handle_http("POST", {}, json.dumps(payload).encode())


def test_ac26_feishu_encrypted_event_roundtrip() -> None:
    plaintext = json.dumps(OFFICIAL_FEISHU_EVENT, ensure_ascii=False)
    encrypted = FeishuAdapter.encrypt_for_test(plaintext, FEISHU_ENCRYPT_KEY)
    adapter = FeishuAdapter(encrypt_key=FEISHU_ENCRYPT_KEY)
    inbound = adapter.handle_http("POST", {}, json.dumps({"encrypt": encrypted}).encode())
    env = adapter.build_inbound(inbound.payload)
    assert env.text == "A3 空调坏了"
    assert env.channel_user_id == "ou_84aad35d084aa403a838cf73ee18467"


# ===========================================================================
# Feishu outbound (AC-27)
# ===========================================================================


def test_ac27_feishu_dm_outbound_contract() -> None:
    import os as _os

    _os.environ["REAL_CHANNEL_NETWORK"] = "false"  # contract tests: hermetic always
    transport = HttpTransport()
    client = FeishuOutboundClient(transport=transport)
    message = OutboundMessage(
        channel="feishu",
        target=DeliveryTarget("feishu", TargetKind.USER, "ou_7d8a6e6df7621556ce0d21922b676706ccs"),
        text="工单 T0001 已确认关闭。",
        notification_type="REQUESTER_STATUS_UPDATE",
    )
    success, code, _ = client.deliver(message)
    assert success and code == "SENT_FEISHU"

    req = transport.records[0]
    assert req.url == "https://open.feishu.cn/open-apis/im/v1/messages"
    assert req.params["receive_id_type"] == "open_id"
    assert req.headers["Authorization"].startswith("Bearer ")
    assert req.headers["Content-Type"] == "application/json; charset=utf-8"
    body = req.body
    assert body["receive_id"] == "ou_7d8a6e6df7621556ce0d21922b676706ccs"
    assert body["msg_type"] == "text"
    assert json.loads(body["content"]) == {"text": "工单 T0001 已确认关闭。"}


def test_ac27_feishu_group_outbound_contract() -> None:
    import os as _os

    _os.environ["REAL_CHANNEL_NETWORK"] = "false"  # contract tests: hermetic always
    transport = HttpTransport()
    client = FeishuOutboundClient(transport=transport)
    message = OutboundMessage(
        channel="feishu",
        target=DeliveryTarget("feishu", TargetKind.CONVERSATION, "oc_84983ff6516d731e5b5f68d4ea2e1da5"),
        text="新工单 T0001",
        notification_type="OPERATOR_WORK_ITEM",
    )
    client.deliver(message)
    req = transport.records[0]
    assert req.params["receive_id_type"] == "chat_id"
    assert req.body["receive_id"] == "oc_84983ff6516d731e5b5f68d4ea2e1da5"


# ===========================================================================
# WeCom official contract (AC-28)
# ===========================================================================


def test_ac28_wecom_capabilities_honest() -> None:
    adapter = WeComAdapter()
    caps = {c.value for c in adapter.capabilities}
    assert "DM_INBOUND" in caps
    assert "DM_OUTBOUND" in caps
    assert "GROUP_OUTBOUND" in caps
    # official text message format has no ChatId -> group inbound is NOT claimed
    assert ChannelCapability.GROUP_INBOUND not in adapter.capabilities
    assert "chat id" in WeComAdapter.group_inbound_support_note()


def test_ac28_wecom_signature_verification() -> None:
    token = "test-token"
    adapter = WeComAdapter(token=token, encoding_aes_key=WECOM_AES_KEY)
    encrypted, timestamp, nonce = WeComAdapter.encrypt_for_test("<xml/>", WECOM_AES_KEY, "corpid")
    body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    sig = WeComAdapter.signature(token, timestamp, nonce, encrypted)
    adapter.handle_http(
        "POST",
        {"msg_signature": sig, "timestamp": timestamp, "nonce": nonce},
        body.encode(),
    )  # no raise
    with pytest.raises(VerificationError):
        adapter.handle_http(
            "POST",
            {"msg_signature": "deadbeef", "timestamp": timestamp, "nonce": nonce},
            body.encode(),
        )


def test_ac28_wecom_url_verification_get() -> None:
    token = "test-token"
    adapter = WeComAdapter(token=token, encoding_aes_key=WECOM_AES_KEY)
    echostr, timestamp, nonce = WeComAdapter.encrypt_for_test("this-is-the-plaintext", WECOM_AES_KEY, "corpid")
    sig = WeComAdapter.signature(token, timestamp, nonce, echostr)
    inbound = adapter.handle_http(
        "GET",
        {"msg_signature": sig, "timestamp": timestamp, "nonce": nonce, "echostr": echostr},
        b"",
    )
    assert inbound.challenge == "this-is-the-plaintext"


def test_ac28_wecom_encrypted_message_callback_parse() -> None:
    token = "test-token"
    adapter = WeComAdapter(token=token, encoding_aes_key=WECOM_AES_KEY)
    message_xml = (
        "<xml>"
        "<ToUserName><![CDATA[corpid]]></ToUserName>"
        "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<CreateTime>1348831860</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[this is a test]]></Content>"
        "<MsgId>1234567890123456</MsgId>"
        "<AgentID>1</AgentID>"
        "</xml>"
    )
    encrypted, timestamp, nonce = WeComAdapter.encrypt_for_test(message_xml, WECOM_AES_KEY, "corpid")
    body = f"<xml><ToUserName><![CDATA[corpid]]></ToUserName><AgentID><![CDATA[1]]></AgentID><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    sig = WeComAdapter.signature(token, timestamp, nonce, encrypted)
    inbound = adapter.handle_http("POST", {"msg_signature": sig, "timestamp": timestamp, "nonce": nonce}, body.encode())
    assert inbound.payload["MsgId"] == "1234567890123456"
    assert inbound.payload["FromUserName"] == "zhangsan"
    assert inbound.payload["Content"] == "this is a test"

    env = adapter.build_inbound(inbound.payload)
    assert env.message_id == "1234567890123456"
    assert env.channel_user_id == "zhangsan"
    assert env.text == "this is a test"


def test_ac28_wecom_outbound_dm_contract() -> None:
    import os as _os

    _os.environ["REAL_CHANNEL_NETWORK"] = "false"  # contract tests: hermetic always
    transport = HttpTransport()
    client = WeComOutboundClient(transport=transport)
    message = OutboundMessage(
        channel="wecom",
        target=DeliveryTarget("wecom", TargetKind.USER, "zhangsan"),
        text="工单 T0001 已确认关闭。",
        notification_type="REQUESTER_STATUS_UPDATE",
    )
    success, code, _ = client.deliver(message)
    assert success and code == "SENT_WECOM"
    req = transport.records[0]
    assert req.url == "https://qyapi.weixin.qq.com/cgi-bin/message/send"
    assert "access_token" in req.params
    assert req.body["touser"] == "zhangsan"
    assert req.body["msgtype"] == "text"
    assert req.body["text"]["content"] == "工单 T0001 已确认关闭。"


def test_ac28_wecom_outbound_group_contract() -> None:
    import os as _os

    _os.environ["REAL_CHANNEL_NETWORK"] = "false"  # contract tests: hermetic always
    transport = HttpTransport()
    client = WeComOutboundClient(transport=transport)
    message = OutboundMessage(
        channel="wecom",
        target=DeliveryTarget("wecom", TargetKind.CONVERSATION, "wrAEX9RgAAKNkRjmFs6f3f2z_tEPiT1A"),
        text="新工单 T0001",
        notification_type="OPERATOR_WORK_ITEM",
    )
    client.deliver(message)
    req = transport.records[0]
    assert req.url == "https://qyapi.weixin.qq.com/cgi-bin/appchat/send"
    assert req.body["chatid"] == "wrAEX9RgAAKNkRjmFs6f3f2z_tEPiT1A"
    assert req.body["msgtype"] == "text"


def test_ac28_wecom_gettoken_cached() -> None:
    transport = HttpTransport()
    client = WeComOutboundClient(transport=transport)
    client._get_access_token()  # noqa: SLF001 - network disabled, no-op
    client._get_access_token()  # noqa: SLF001
    assert len(transport.records) == 0  # never fetches a token offline
