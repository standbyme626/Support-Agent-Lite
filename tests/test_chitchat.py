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


# --- LLM-backed chitchat (B-fix v2) -----------------------------------------------


def test_chitchat_goes_through_llm_when_available(app_ctx):
    from tests.fake_llm import make_decision

    from tests.conftest import WECOM_REPAIR_GROUP

    class ChatLLM:
        model = "chat-fake"

        def complete(self, system: str, user: str) -> str:
            assert "闲聊" in user or "日常对话" in user  # chitchat pack rendered
            import json

            return make_decision(summary="闲聊", category="general", action="faq_answer", reply="嗨～我是支持助手，有什么可以帮你？")

    app_ctx.with_llm(ChatLLM())
    resp = app_ctx.client.post(
        "/webhooks/wecom",
        json={"MsgId": "chat-llm-1", "FromUserName": "zhangsan", "Content": "你好呀",
              "CreateTime": 2000, "conversation_id": WECOM_REPAIR_GROUP},
    )
    body = resp.json()
    assert body["workflow"] == "chitchat"
    assert "嗨" in body["reply"]


def test_test_noise_message_never_creates_ticket(app_ctx):
    from tests.conftest import WECOM_REPAIR_GROUP

    before = len(list(app_ctx.store.list_by_user(app_ctx.users["zhangsan"])))
    for i, text in enumerate(["测试", "test", "在么"]):
        resp = app_ctx.client.post(
            "/webhooks/wecom",
            json={"MsgId": f"noise-{i}", "FromUserName": "zhangsan", "Content": text,
                  "CreateTime": 3000 + i, "conversation_id": WECOM_REPAIR_GROUP},
        )
        assert resp.json()["workflow"] == "chitchat", f"{text} should be chitchat"
    after = len(list(app_ctx.store.list_by_user(app_ctx.users["zhangsan"])))
    assert after == before


def test_casual_chat_with_llm_no_ticket(app_ctx):
    from tests.conftest import WECOM_REPAIR_GROUP

    app_ctx.with_llm(_NeedsHumanLLM(False))
    before = len(list(app_ctx.store.list_by_user(app_ctx.users["zhangsan"])))
    resp = app_ctx.client.post(
        "/webhooks/wecom",
        json={"MsgId": "casual-1", "FromUserName": "zhangsan", "Content": "给我讲个笑话吧",
              "CreateTime": 4000, "conversation_id": WECOM_REPAIR_GROUP},
    )
    body = resp.json()
    assert body["workflow"] == "chitchat" and body["ticket_id"] is None
    assert len(list(app_ctx.store.list_by_user(app_ctx.users["zhangsan"]))) == before


def test_llm_flagged_request_still_creates_ticket(app_ctx):
    from tests.conftest import WECOM_REPAIR_GROUP

    app_ctx.with_llm(_NeedsHumanLLM(True))
    resp = app_ctx.client.post(
        "/webhooks/wecom",
        json={"MsgId": "casual-2", "FromUserName": "zhangsan", "Content": "帮我订下周的会议室并且通知所有人",
              "CreateTime": 4001, "conversation_id": WECOM_REPAIR_GROUP},
    )
    body = resp.json()
    assert body["workflow"] == "other" and body["ticket_id"]


class _NeedsHumanLLM:
    """Chitchat-pack LLM whose needs_human flag is configurable."""

    model = "needs-human-fake"

    def __init__(self, needs_human: bool) -> None:
        self._needs_human = needs_human

    def complete(self, system: str, user: str) -> str:
        from tests.fake_llm import make_decision

        return make_decision(
            summary="闲聊判定", category="general", action="faq_answer",
            reply="好的～" if not self._needs_human else "这个需求我帮您转给人工同事处理。",
            needs_human=self._needs_human,
        )


def test_ticket_reference_never_degrades_to_chitchat():
    router = IntentRouter()
    d = router.route("帮我看看T0004现在到哪一步了")
    assert d.intent == "progress_query"


def test_explicit_ticket_id_after_cjk_resolves():
    """「看看T0004」——CJK 与 T 相邻时 \\b 边界失效曾导致按会话错绑工单。"""
    from app.application.ticket_service import EXPLICIT_TICKET_RE

    assert EXPLICIT_TICKET_RE.search("帮我看看T0004现在到哪一步了")
    assert EXPLICIT_TICKET_RE.search("T0004进度")
