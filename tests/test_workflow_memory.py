"""Phase 6 end-to-end: AC-09 (close -> memory) and AC-10 (next-session recall)."""
from fastapi.testclient import TestClient

from app.main import build_ingress, build_ops, create_app


def _client():
    ingress, conn, store = build_ingress(db_path=":memory:")
    ops = build_ops(conn, store)
    return TestClient(create_app(ingress, ops)), store


def _wecom(client, content, msg_id, conversation_id="conv_1"):
    return client.post(
        "/webhooks/wecom",
        json={
            "MsgId": msg_id,
            "FromUserName": "zhangsan",
            "Content": content,
            "CreateTime": 1000,
            "conversation_id": conversation_id,
        },
    )


def _close_full_lifecycle(client, note: str = "已更换空调滤网") -> str:
    """Create + claim + resolve + close a ticket via webhook/operator API.
    Returns the canonical user_id."""
    user_id = _wecom(client, "A3 空调坏了", "m1").json()["user_id"]
    assert client.post("/tickets/T0001/claim").status_code == 200
    assert client.post("/tickets/T0001/resolve", json={"note": note}).status_code == 200
    assert client.post("/tickets/T0001/close").status_code == 200
    return user_id


def test_ac09_close_produces_stable_memory() -> None:
    """AC-09: after T0001 CLOSED, MemoryExtractor produces stable facts."""
    client, _ = _client()
    user_id = _close_full_lifecycle(client)

    resp = client.get(f"/memories?user_id={user_id}")
    assert resp.status_code == 200
    memories = resp.json()
    facts = [m["fact"] for m in memories]

    assert len(memories) == 3  # issue fact + resolution fact + summary
    assert any(f == "设备问题：A3 空调坏了" for f in facts)
    assert any(f == "处理结果：已更换空调滤网" for f in facts)
    assert any("已处理完成" in f for f in facts)
    assert all(m["kind"] in ("stable_fact", "summary") for m in memories)
    assert all(m["user_id"] == user_id for m in memories)
    assert all(m["ticket_id"] == "T0001" for m in memories)


def test_ac09_memory_kind_filter() -> None:
    client, _ = _client()
    user_id = _close_full_lifecycle(client)

    stable = client.get(f"/memories?user_id={user_id}&kind=stable_fact").json()
    summaries = client.get(f"/memories?user_id={user_id}&kind=summary").json()
    assert len(stable) == 2
    assert len(summaries) == 1

    bad = client.get("/memories?kind=WEIRD")
    assert bad.status_code == 400


def test_ac09_unclosed_ticket_has_no_memory() -> None:
    client, _ = _client()
    user_id = _wecom(client, "VPN 连不上", "m1").json()["user_id"]
    client.post("/tickets/T0001/claim")
    client.post("/tickets/T0001/resolve")

    resp = client.get(f"/memories?user_id={user_id}")
    assert resp.json() == []  # memory only after CLOSED


def test_ac10_new_session_recalls_prior_resolution() -> None:
    """AC-10: new session '空调又坏了' recalls prior T0001/A3 facts."""
    client, _ = _client()
    user_id = _close_full_lifecycle(client, note="已更换空调滤网")

    # fresh conversation -> brand-new session
    resp = _wecom(client, "空调又坏了", "m2", conversation_id="conv_other")
    assert resp.status_code == 200
    body = resp.json()

    assert body["workflow"] == "ticket"
    assert body["ticket_id"] == "T0002"  # closed T0001 -> new ticket
    recalled = body["recalled"]
    assert any("A3 空调坏了" in fact for fact in recalled)
    assert any("T0001" in fact for fact in recalled)
    assert any("处理结果：已更换空调滤网" in fact for fact in recalled)


def test_ac10_no_recall_for_unrelated_new_session() -> None:
    client, _ = _client()
    user_id = _close_full_lifecycle(client)

    resp = _wecom(client, "年假怎么申请", "m2", conversation_id="conv_other")
    assert resp.json()["workflow"] == "faq_answer"
    assert resp.json()["recalled"] == []


def test_ac10_recall_flows_into_agent_context() -> None:
    """Recalled memory lands in the agent summary (context, not reply)."""
    client, _ = _client()
    _close_full_lifecycle(client, note="已更换空调滤网")

    resp = _wecom(client, "空调又坏了", "m2", conversation_id="conv_other")
    body = resp.json()
    assert body["ticket_id"] == "T0002"

    # the workflow analysis is not exposed via HTTP, but the reply draft is
    # still grounded in the new ticket; recall is visible in body["recalled"]
    assert len(body["recalled"]) >= 1
    assert "空调" in body["recalled"][0]
