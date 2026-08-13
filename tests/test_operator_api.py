"""Phase 5 operator API tests: AC-04 lifecycle + AC-08 HITL.

The full pipeline (webhook -> ticket) plus operator endpoints is tested
locally; real WeCom/Feishu channels are wired later without changing
these contracts.
"""
from fastapi.testclient import TestClient

from app.main import build_ingress, build_ops, create_app


def _client():
    ingress, conn, store = build_ingress(db_path=":memory:")
    ops = build_ops(conn, store)
    return TestClient(create_app(ingress, ops)), store


def _make_ticket(client) -> dict:
    """Create a ticket through the real webhook pipeline (AC-02)."""
    resp = client.post(
        "/webhooks/wecom",
        json={"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000},
    )
    assert resp.status_code == 200
    assert resp.json()["ticket_id"] == "T0001"
    return resp.json()


# --- AC-04: claim / resolve / close lifecycle ---


def test_ac04_claim_open_to_in_progress() -> None:
    client, store = _client()
    _make_ticket(client)

    resp = client.post("/tickets/T0001/claim")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "T0001"
    assert body["status"] == "IN_PROGRESS"

    events = store.events("T0001")
    assert [e.event_type.value for e in events] == ["created", "started"]


def test_claim_requires_open_ticket() -> None:
    client, store = _client()
    _make_ticket(client)
    client.post("/tickets/T0001/claim")

    resp = client.post("/tickets/T0001/claim")
    assert resp.status_code == 409


def test_full_lifecycle_claim_resolve_close() -> None:
    client, store = _client()
    _make_ticket(client)

    claimed = client.post("/tickets/T0001/claim")
    assert claimed.json()["status"] == "IN_PROGRESS"

    resolved = client.post("/tickets/T0001/resolve", json={"note": "已更换设备"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"

    closed = client.post("/tickets/T0001/close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "CLOSED"

    events = store.events("T0001")
    assert [e.event_type.value for e in events] == ["created", "started", "resolved", "closed"]


def test_invalid_transitions_return_409() -> None:
    client, _ = _client()
    _make_ticket(client)

    # OPEN -> RESOLVED is not allowed
    assert client.post("/tickets/T0001/resolve").status_code == 409
    # OPEN -> CLOSED is not allowed
    assert client.post("/tickets/T0001/close").status_code == 409


def test_unknown_ticket_returns_404() -> None:
    client, _ = _client()
    assert client.post("/tickets/T9999/claim").status_code == 404
    assert client.post("/tickets/T9999/escalate").status_code == 404


# --- AC-08: escalate + approval ---


def test_ac08_escalate_creates_pending_approval_ticket_unchanged() -> None:
    client, store = _client()
    _make_ticket(client)

    resp = client.post("/tickets/T0001/escalate", json={"reason": "用户强烈要求"})
    assert resp.status_code == 200
    approval = resp.json()
    assert approval["ticket_id"] == "T0001"
    assert approval["status"] == "PENDING"
    assert approval["action"] == "escalate"

    # ticket remains valid (status untouched, still OPEN)
    ticket = store.get("T0001")
    assert ticket is not None and ticket.status.value == "OPEN"

    # approval is independent: not stored on the ticket
    assert [e.event_type.value for e in store.events("T0001")] == ["created"]


def test_approvals_list_and_status_filter() -> None:
    client, _ = _client()
    _make_ticket(client)
    client.post("/tickets/T0001/escalate", json={"reason": "r1"})

    resp = client.get("/approvals")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["status"] == "PENDING"

    filtered = client.get("/approvals?status=APPROVED")
    assert filtered.json() == []


def test_approve_moves_to_approved() -> None:
    client, store = _client()
    _make_ticket(client)
    approval_id = client.post("/tickets/T0001/escalate").json()["id"]

    resp = client.post(f"/approvals/{approval_id}/approve", json={"decided_by": "manager"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["decided_by"] == "manager"
    assert body["decided_at"] is not None

    # ticket still valid after approval
    assert store.get("T0001").status.value == "OPEN"


def test_reject_moves_to_rejected() -> None:
    client, _ = _client()
    _make_ticket(client)
    approval_id = client.post("/tickets/T0001/escalate").json()["id"]

    resp = client.post(f"/approvals/{approval_id}/reject", json={"reason": "无需升级"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    assert resp.json()["reason"] == "无需升级"


def test_double_decision_returns_409() -> None:
    client, _ = _client()
    _make_ticket(client)
    approval_id = client.post("/tickets/T0001/escalate").json()["id"]

    assert client.post(f"/approvals/{approval_id}/approve").status_code == 200
    assert client.post(f"/approvals/{approval_id}/approve").status_code == 409
    assert client.post(f"/approvals/{approval_id}/reject").status_code == 409


def test_unknown_approval_returns_404() -> None:
    client, _ = _client()
    assert client.post("/approvals/apr_missing/approve").status_code == 404


def test_invalid_approval_status_filter_returns_400() -> None:
    client, _ = _client()
    assert client.get("/approvals?status=WEIRD").status_code == 400


def test_escalate_custom_action() -> None:
    client, _ = _client()
    _make_ticket(client)
    resp = client.post("/tickets/T0001/escalate", json={"action": "emergency_restart"})
    assert resp.json()["action"] == "emergency_restart"


# --- end-to-end: webhook -> operator lifecycle ---


def test_webhook_to_operator_end_to_end() -> None:
    client, store = _client()
    ticket = _make_ticket(client)

    assert client.post("/tickets/T0001/claim").json()["status"] == "IN_PROGRESS"
    assert client.post("/tickets/T0001/resolve").json()["status"] == "RESOLVED"

    # cross-channel progress query still resolves to the same ticket
    from app.application.identity_service import IdentityResolver
    from app.infrastructure.repositories import ChannelIdentityRepository, UserRepository

    IdentityResolver(UserRepository(store._conn), ChannelIdentityRepository(store._conn)).bind(  # noqa: SLF001
        "feishu", "ou_001", ticket["user_id"]
    )
    feishu = client.post(
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
    assert feishu.status_code == 200
    assert feishu.json()["ticket_id"] == "T0001"
    assert feishu.json()["workflow"] == "progress"
    assert store.get("T0001").status.value == "RESOLVED"
