"""Chitchat intent: greetings/identity/thanks/help must NEVER create tickets."""
from __future__ import annotations

from pathlib import Path

from app.application.intent_router import IntentRouter


def test_router_detects_chitchat_variants():
    router = IntentRouter()
    for text in (
        "你好", "您好呀", "在吗", "你好你是谁", "你是谁",
        "你是机器人吗", "你叫什么名字", "谢谢", "多谢啦", "感谢",
        "再见", "拜拜", "/help", "help", "帮助", "你能做什么",
    ):
        d = router.route(text)
        assert d.intent == "chitchat", f"{text!r} -> {d.intent}({d.confidence})"


def test_real_requests_not_hijacked_by_chitchat():
    router = IntentRouter()
    assert router.route("空调坏了 A3 会议室").intent == "support"
    assert router.route("T0002 处理了吗？谢谢").intent != "chitchat"  # 长句含谢仍按业务路由
    assert router.route("打印机坏了").intent == "support"


def test_workflow_chitchat_creates_no_ticket(app_ctx):
    from tests.conftest import WECOM_REPAIR_GROUP

    before = len(list(app_ctx.store.list_by_user(app_ctx.users["zhangsan"])))
    resp = app_ctx.client.post(
        "/webhooks/wecom",
        json={"MsgId": "chat-m1", "FromUserName": "zhangsan", "Content": "你好你是谁",
              "CreateTime": 1000, "conversation_id": WECOM_REPAIR_GROUP},
    )
    assert resp.status_code == 200
    after = len(list(app_ctx.store.list_by_user(app_ctx.users["zhangsan"])))
    assert after == before, "chitchat must never create a ticket"
    body = resp.json()
    assert body["workflow"] == "chitchat"
    assert body["reply"]
    # identity question answers with self-intro marker
    assert ("支持" in body["reply"]) or ("工单" in body["reply"])


def test_help_shows_usage_without_ticket(app_ctx):
    from tests.conftest import WECOM_REPAIR_GROUP

    resp = app_ctx.client.post(
        "/webhooks/wecom",
        json={"MsgId": "chat-m2", "FromUserName": "zhangsan", "Content": "/help",
              "CreateTime": 1001, "conversation_id": WECOM_REPAIR_GROUP},
    )
    assert resp.status_code == 200
    assert resp.json()["workflow"] == "chitchat"
    assert "报修" in resp.json()["reply"]


def test_regression_repair_still_creates_ticket(app_ctx):
    from tests.conftest import WECOM_REPAIR_GROUP

    resp = app_ctx.client.post(
        "/webhooks/wecom",
        json={"MsgId": "chat-m3", "FromUserName": "zhangsan", "Content": "A3 空调坏了",
              "CreateTime": 1002, "conversation_id": WECOM_REPAIR_GROUP},
    )
    assert resp.status_code == 200
    assert resp.json()["ticket_id"], "real repair must still create a ticket"
