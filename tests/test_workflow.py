"""Phase 4 workflow tests: AC-01, AC-02, AC-05, AC-06 + no-answer.

Runs the full ingress -> workflow pipeline (build_ingress + webhook).
"""
from fastapi.testclient import TestClient

from app.main import build_ingress, create_app


def _client():
    ingress, conn, store = build_ingress(db_path=":memory:")
    return TestClient(create_app(ingress)), store


def _wecom(client, content, msg_id):
    return client.post(
        "/webhooks/wecom",
        json={"MsgId": msg_id, "FromUserName": "zhangsan", "Content": content, "CreateTime": 1000},
    )


def test_ac01_wecom_faq_no_ticket() -> None:
    """AC-01: 年假怎么申请 -> FAQ -> grounded answer with source -> NO ticket."""
    client, store = _client()
    resp = _wecom(client, "年假怎么申请？", "m1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["workflow"] == "faq_answer"
    assert "faq-001" in body["reply"]
    assert "来源" in body["reply"]
    assert body["ticket_id"] is None
    assert store.list_by_user("whatever") == []


def test_ac02_wecom_auto_ticket() -> None:
    """AC-02: A3 空调坏了 -> support intent -> T0001 OPEN -> created event."""
    client, store = _client()
    resp = _wecom(client, "A3 空调坏了", "m1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow"] == "ticket"
    assert body["ticket_id"] == "T0001"


def test_ac02_ticket_state_and_events() -> None:
    client, store = _client()
    body = _wecom(client, "A3 空调坏了", "m1").json()

    # canonical user id comes back from the response; list their tickets
    user_id = body["user_id"]
    tickets = store.list_by_user(user_id)
    assert [t.id for t in tickets] == ["T0001"]
    ticket = tickets[0]
    assert ticket.title == "A3 空调坏了"
    assert ticket.status.value == "OPEN"

    events = store.events("T0001")
    assert [e.event_type.value for e in events] == ["created"]

    analysis_asserts = client.post(
        "/webhooks/wecom",
        json={"MsgId": "m2", "FromUserName": "zhangsan", "Content": "A3 空调坏了，很紧急", "CreateTime": 2000},
    ).json()
    assert analysis_asserts["workflow"] == "ticket"
    assert analysis_asserts["ticket_id"] == "T0001"


def test_ac03_duplicate_webhook_does_not_create_second_ticket() -> None:
    client, store = _client()
    first = _wecom(client, "A3 空调坏了", "m1")
    second = _wecom(client, "A3 空调坏了", "m1")

    assert first.status_code == 200
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert second.json()["workflow"] is None
    tickets = store.list_by_user(second.json()["user_id"])
    assert [t.id for t in tickets] == ["T0001"]


def test_ac05_feishu_cross_channel_continuation() -> None:
    """AC-05: feishu/ou_001 continues wecom T0001 via canonical user, no T0002."""
    client, store = _client()

    first = _wecom(client, "A3 空调坏了", "m1").json()
    user_id = first["user_id"]

    # bind feishu/ou_001 to the same canonical user on the same connection
    from app.application.identity_service import IdentityResolver
    from app.infrastructure.repositories import ChannelIdentityRepository, UserRepository

    identity = IdentityResolver(UserRepository(store._conn), ChannelIdentityRepository(store._conn))  # noqa: SLF001
    identity.bind("feishu", "ou_001", user_id)

    resp = client.post(
        "/webhooks/feishu",
        json={
            "event_id": "evt_1",
            "event": {
                "message": {"message_id": "om_1", "text": "昨天空调那个事情怎么样了？"},
                "sender": {"sender_id": {"open_id": "ou_001"}},
                "chat_id": "oc_1",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user_id
    assert body["workflow"] == "progress"
    assert body["ticket_id"] == "T0001"
    assert "T0001" in body["reply"]

    tickets = store.list_by_user(user_id)
    assert [t.id for t in tickets] == ["T0001"]  # NO T0002


def test_ac06_multiple_active_tickets_require_clarification() -> None:
    """AC-06: two active tickets -> clarify reply listing candidates."""
    client, store = _client()
    user_id = _wecom(client, "A3 空调坏了", "m1").json()["user_id"]

    # second active ticket (same canonical user), as in AC-06 seed
    from app.application.ticket_service import TicketService

    TicketService(store).create(user_id, "VPN 连不上", "报错 619")

    # new conversation (fresh session) -> no session ticket -> must clarify
    resp = client.post(
        "/webhooks/wecom",
        json={"MsgId": "m3", "FromUserName": "zhangsan", "Content": "处理了吗？", "CreateTime": 3000, "conversation_id": "conv_other"},
    )
    body = resp.json()
    assert body["workflow"] == "clarify"
    assert "T0001" in body["reply"] and "T0002" in body["reply"]
    assert body["ticket_id"] is None


def test_no_answer_protection_for_low_confidence() -> None:
    """AC-21 (V2): low-confidence query -> no free-form answer; the reply is
    truthful and a real human-handoff ticket exists (no fake '转人工')."""
    client, store = _client()
    resp = _wecom(client, "今天天气怎么样", "m1")
    body = resp.json()
    assert body["workflow"] == "no_answer"
    assert body["ticket_id"] == "T0001"  # real handoff ticket, not a lie
    tickets = store.list_by_user(body["user_id"])
    assert [t.id for t in tickets] == ["T0001"]


def test_other_intent_real_handoff() -> None:
    """AC-21 semantics apply to the `other` branch too: a truthful '专人跟进'
    reply must back a real ticket + operator work item (no fake handoff)."""
    client, store = _client()
    resp = _wecom(client, "给我讲个笑话吧", "m1")  # unclassifiable request (was: greeting)
    body = resp.json()
    assert body["workflow"] == "other"
    assert body["ticket_id"] == "T0001"
    assert store.get("T0001") is not None
