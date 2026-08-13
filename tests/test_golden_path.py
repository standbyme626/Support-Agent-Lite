"""Phase 7: the 10 Golden Path acceptance tests (AC-01 .. AC-10).

One client per test builds the full pipeline (webhook + operator API +
approval + memory + trace), exercising the whole system locally.
"""
from fastapi.testclient import TestClient

from app.application.identity_service import IdentityResolver
from app.infrastructure.repositories import ChannelIdentityRepository, UserRepository
from app.main import build_ingress, build_ops, create_app


def _client():
    ingress, conn, store = build_ingress(db_path=":memory:")
    ops = build_ops(conn, store)
    return TestClient(create_app(ingress, ops)), store


def _wecom(client, content, msg_id, conversation_id="conv_1", user="zhangsan"):
    return client.post(
        "/webhooks/wecom",
        json={
            "MsgId": msg_id,
            "FromUserName": user,
            "Content": content,
            "CreateTime": 1000,
            "conversation_id": conversation_id,
        },
    )


def _feishu(client, text, msg_id, open_id="ou_001"):
    return client.post(
        "/webhooks/feishu",
        json={
            "event_id": f"evt_{msg_id}",
            "event": {
                "message": {"message_id": msg_id, "text": text},
                "sender": {"sender_id": {"open_id": open_id}},
                "chat_id": f"oc_{msg_id}",
            },
        },
    )


# --- AC-01 WeCom FAQ ---


def test_ac01_wecom_faq() -> None:
    client, store = _client()
    resp = _wecom(client, "年假怎么申请？", "m1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow"] == "faq_answer"
    assert "faq-001" in body["reply"] and "来源" in body["reply"]
    assert body["ticket_id"] is None
    assert store.list_by_user(body["user_id"]) == []  # NO ticket


# --- AC-02 WeCom auto ticket ---


def test_ac02_wecom_auto_ticket() -> None:
    client, store = _client()
    resp = _wecom(client, "A3 空调坏了", "m1")

    body = resp.json()
    assert body["workflow"] == "ticket"
    assert body["ticket_id"] == "T0001"

    ticket = store.get("T0001")
    assert ticket is not None and ticket.status.value == "OPEN"
    assert [e.event_type.value for e in store.events("T0001")] == ["created"]


# --- AC-03 Webhook idempotency ---


def test_ac03_webhook_idempotency() -> None:
    client, store = _client()
    first = _wecom(client, "A3 空调坏了", "m1")
    second = _wecom(client, "A3 空调坏了", "m1")

    assert first.status_code == 200
    assert second.status_code == 202
    assert second.json()["duplicate"] is True

    user_id = first.json()["user_id"]
    tickets = store.list_by_user(user_id)
    assert len(tickets) == 1  # 1 processed, 1 ticket, 0 duplicates


# --- AC-04 Operator claim ---


def test_ac04_operator_claim() -> None:
    client, store = _client()
    _wecom(client, "A3 空调坏了", "m1")

    resp = client.post("/tickets/T0001/claim")
    assert resp.status_code == 200
    assert resp.json()["status"] == "IN_PROGRESS"
    assert [e.event_type.value for e in store.events("T0001")] == ["created", "started"]


# --- AC-05 Feishu cross-channel continuation ---


def test_ac05_feishu_cross_channel_continuation() -> None:
    client, store = _client()
    wecom = _wecom(client, "A3 空调坏了", "m1").json()
    user_id = wecom["user_id"]

    # seed: wecom/zhangsan + feishu/ou_001 -> same canonical user
    IdentityResolver(UserRepository(store._conn), ChannelIdentityRepository(store._conn)).bind(  # noqa: SLF001
        "feishu", "ou_001", user_id
    )

    resp = _feishu(client, "昨天空调那个事情怎么样了？", "om_1")
    body = resp.json()
    assert body["user_id"] == user_id
    assert body["workflow"] == "progress"
    assert body["ticket_id"] == "T0001"
    assert [t.id for t in store.list_by_user(user_id)] == ["T0001"]  # NO T0002


# --- AC-06 Multi-ticket disambiguation ---


def test_ac06_multi_ticket_disambiguation() -> None:
    client, store = _client()
    user_id = _wecom(client, "A3 空调坏了", "m1").json()["user_id"]
    from app.application.ticket_service import TicketService

    TicketService(store).create(user_id, "VPN 连不上", "报错 619")

    resp = _wecom(client, "处理了吗？", "m3", conversation_id="conv_other")
    body = resp.json()
    assert body["workflow"] == "clarify"
    assert "T0001" in body["reply"] and "T0002" in body["reply"]
    assert body["ticket_id"] is None  # LLM must NOT pick arbitrarily


# --- AC-07 Agent summary / context ---


def test_ac07_agent_summary_context() -> None:
    client, store = _client()
    user_id = _wecom(client, "A3 空调坏了", "m1").json()["user_id"]
    _wecom(client, "很急，能尽快吗", "m2")

    resp = _wecom(client, "继续跟进", "m3")
    body = resp.json()
    assert body["workflow"] == "progress"  # follow-up routes to the active ticket
    assert body["ticket_id"] == "T0001"

    # recent messages were recorded per session (context building material)
    from app.infrastructure.repositories import MessageRepository

    messages = MessageRepository(store._conn)  # noqa: SLF001
    session_id = body["session_id"]
    user_texts = [m.text for m in messages.recent(session_id, limit=6) if m.role == "user"]
    assert user_texts == ["A3 空调坏了", "很急，能尽快吗", "继续跟进"]  # in order

    # agent analysis is advice only: ticket state unchanged
    assert store.get("T0001").status.value == "OPEN"


# --- AC-08 HITL / approval ---


def test_ac08_hitl_approval() -> None:
    client, store = _client()
    _wecom(client, "A3 空调坏了", "m1")
    client.post("/tickets/T0001/claim")

    escalated = client.post("/tickets/T0001/escalate", json={"reason": "用户要求升级"})
    assert escalated.status_code == 200
    approval = escalated.json()
    assert approval["status"] == "PENDING"
    assert store.get("T0001").status.value == "IN_PROGRESS"  # ticket remains valid

    approved = client.post(f"/approvals/{approval['id']}/approve", json={"decided_by": "manager"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert store.get("T0001").status.value == "IN_PROGRESS"  # still valid after approve

    # approval is independent: no extra ticket events
    assert [e.event_type.value for e in store.events("T0001")] == ["created", "started"]


# --- AC-09 Close -> Memory ---


def test_ac09_close_to_memory() -> None:
    client, store = _client()
    user_id = _wecom(client, "A3 空调坏了", "m1").json()["user_id"]
    client.post("/tickets/T0001/claim")
    client.post("/tickets/T0001/resolve", json={"note": "已更换空调滤网"})
    client.post("/tickets/T0001/close")

    memories = client.get(f"/memories?user_id={user_id}").json()
    facts = [m["fact"] for m in memories]
    assert any(f == "设备问题：A3 空调坏了" for f in facts)
    assert any(f == "处理结果：已更换空调滤网" for f in facts)
    assert any("已处理完成" in f for f in facts)


# --- AC-10 New Session Recall ---


def test_ac10_new_session_recall() -> None:
    client, store = _client()
    _wecom(client, "A3 空调坏了", "m1")
    client.post("/tickets/T0001/claim")
    client.post("/tickets/T0001/resolve", json={"note": "已更换空调滤网"})
    client.post("/tickets/T0001/close")

    resp = _wecom(client, "空调又坏了", "m10", conversation_id="conv_new")
    body = resp.json()
    assert body["workflow"] == "ticket"
    assert body["ticket_id"] == "T0002"  # closed T0001 -> new ticket
    facts = body["recalled"]
    assert any("A3 空调坏了" in f for f in facts)
    assert any("已更换空调滤网" in f for f in facts)
    assert any("T0001" in f for f in facts)


# --- trace: the full journey of one message ---


def test_trace_covers_full_journey() -> None:
    client, store = _client()
    body = _wecom(client, "A3 空调坏了", "m1").json()
    trace_id = body["trace_id"]

    trace = client.get(f"/traces/{trace_id}")
    assert trace.status_code == 200
    stages = [s["stage"] for s in trace.json()["stages"]]

    # channel -> identity -> intent -> ticket -> agent -> reply
    assert stages[0] == "channel"
    assert "identity" in stages
    assert "intent" in stages
    assert "ticket" in stages
    assert "agent" in stages
    assert stages[-1] == "reply"

    intent_payload = next(s["payload"] for s in trace.json()["stages"] if s["stage"] == "intent")
    assert intent_payload["intent"] == "support"
    ticket_payload = next(s["payload"] for s in trace.json()["stages"] if s["stage"] == "ticket")
    assert ticket_payload["ticket_id"] == "T0001"

    # retrieval trace on the FAQ path
    faq = _wecom(client, "年假怎么申请？", "m2").json()
    faq_trace = client.get(f"/traces/{faq['trace_id']}").json()
    retrieval = next(s for s in faq_trace["stages"] if s["stage"] == "retrieval")
    assert retrieval["payload"]["grounded"] is True
    assert retrieval["payload"]["hits"][0]["doc_id"] == "faq-001"

    # memory_recall trace on the recall path (requires a closed ticket first)
    _wecom(client, "A3 空调坏了", "m2b", conversation_id="conv_lifecycle")
    client.post("/tickets/T0001/claim")
    client.post("/tickets/T0001/resolve", json={"note": "已更换空调滤网"})
    client.post("/tickets/T0001/close")
    recall = _wecom(client, "空调又坏了", "m3", conversation_id="conv_recall").json()
    recall_trace = client.get(f"/traces/{recall['trace_id']}").json()
    stages_recall = [s["stage"] for s in recall_trace["stages"]]
    assert "memory_recall" in stages_recall


def test_trace_unknown_returns_404() -> None:
    client, _ = _client()
    assert client.get("/traces/trace_missing").status_code == 404
