"""Phase 5 operator API tests: AC-04 lifecycle + AC-08 HITL.

V2.1 changes: the REST surface is a trusted control-plane API — every
action resolves the canonical actor and verifies the required role
(AC-A17); `/close` is deprecated and routes through the FORCE_CLOSE
approval pipeline (closure fix, AC-A16).
"""
from fastapi.testclient import TestClient

from app.main import build_ingress, build_ops, create_app
from tests.v2_fixtures import APPROVER_ACTOR, OPERATOR_ACTOR, seed_control_plane


def _client():
    ingress, conn, store = build_ingress(db_path=":memory:")
    ops = build_ops(conn, store)
    seed_control_plane(conn)
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


def _claim(client) -> None:
    resp = client.post("/tickets/T0001/claim", json=OPERATOR_ACTOR)
    assert resp.status_code == 200, resp.text


# --- AC-04: claim / resolve / close lifecycle ---


def test_ac04_claim_open_to_in_progress() -> None:
    client, store = _client()
    _make_ticket(client)

    resp = client.post("/tickets/T0001/claim", json=OPERATOR_ACTOR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "T0001"
    assert body["status"] == "IN_PROGRESS"

    events = store.events("T0001")
    assert [e.event_type.value for e in events] == ["created", "claimed"]


def test_claim_requires_open_ticket() -> None:
    client, store = _client()
    _make_ticket(client)
    _claim(client)

    resp = client.post("/tickets/T0001/claim", json=OPERATOR_ACTOR)
    assert resp.status_code == 409


def test_full_lifecycle_claim_resolve_close() -> None:
    """AC-A16: RESOLVED -> CLOSED requires requester confirmation or an
    approved FORCE_CLOSE — the unapproved direct-close backdoor is gone."""
    client, store = _client()
    _make_ticket(client)
    _claim(client)

    resolved = client.post("/tickets/T0001/resolve", json={**OPERATOR_ACTOR, "note": "已更换设备"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"

    # direct close without reason -> 400 (no unapproved close)
    no_reason = client.post("/tickets/T0001/close", json=OPERATOR_ACTOR)
    assert no_reason.status_code == 400

    # close with reason -> FORCE_CLOSE approval, ticket stays RESOLVED
    closed = client.post("/tickets/T0001/close", json={**OPERATOR_ACTOR, "reason": "用户已离职"})
    assert closed.status_code == 200
    approval = closed.json()
    assert approval["status"] == "PENDING"
    assert approval["action"] == "FORCE_CLOSE"
    assert store.get("T0001").status.value == "RESOLVED"  # not closed yet

    events = store.events("T0001")
    assert [e.event_type.value for e in events] == ["created", "claimed", "resolved"]

    # approver approves -> deterministic executor closes (RESOLVED->CLOSED
    # records the standard 'closed' event with the approval provenance)
    approved = client.post(f"/approvals/{approval['id']}/approve", json=APPROVER_ACTOR)
    assert approved.status_code == 200
    assert store.get("T0001").status.value == "CLOSED"
    events = store.events("T0001")
    assert [e.event_type.value for e in events] == [
        "created", "claimed", "resolved", "closed"
    ]
    assert events[-1].payload.get("approval_id") == approval["id"]
    assert events[-1].payload.get("reason") == "用户已离职"


def test_invalid_transitions_return_409() -> None:
    client, _ = _client()
    _make_ticket(client)

    # OPEN -> RESOLVED is not allowed
    assert client.post("/tickets/T0001/resolve", json=OPERATOR_ACTOR).status_code == 409
    # OPEN -> CLOSED (even with reason) is rejected by policy/state machine
    assert client.post("/tickets/T0001/close", json={**OPERATOR_ACTOR, "reason": "r"}).status_code == 409


def test_unknown_ticket_returns_404() -> None:
    client, _ = _client()
    assert client.post("/tickets/T9999/claim", json=OPERATOR_ACTOR).status_code == 404
    assert client.post("/tickets/T9999/escalate", json=OPERATOR_ACTOR).status_code == 404


# --- AC-A17: REST trust boundary (actor resolution + role verification) ---


def test_rest_requires_actor() -> None:
    client, _ = _client()
    _make_ticket(client)
    assert client.post("/tickets/T0001/claim").status_code == 401


def test_rest_unknown_actor_rejected() -> None:
    client, _ = _client()
    _make_ticket(client)
    resp = client.post("/tickets/T0001/claim", json={"actor_user_id": "user_ghost"})
    assert resp.status_code == 401


def test_rest_requires_operator_role() -> None:
    client, _ = _client()
    _make_ticket(client)
    # zhangsan is a requester, not an operator
    resp = client.post(
        "/tickets/T0001/claim",
        json={"actor": {"channel": "wecom", "channel_user_id": "zhangsan"}},
    )
    assert resp.status_code == 403


def test_rest_approval_requires_approver_role() -> None:
    client, _ = _client()
    _make_ticket(client)
    approval_id = client.post("/tickets/T0001/escalate", json={**OPERATOR_ACTOR, "reason": "r"}).json()["id"]
    # an operator cannot approve
    resp = client.post(f"/approvals/{approval_id}/approve", json=OPERATOR_ACTOR)
    assert resp.status_code == 403


# --- AC-08: escalate + approval ---


def test_ac08_escalate_creates_pending_approval_ticket_unchanged() -> None:
    client, store = _client()
    _make_ticket(client)

    resp = client.post("/tickets/T0001/escalate", json={**OPERATOR_ACTOR, "reason": "用户强烈要求"})
    assert resp.status_code == 200
    approval = resp.json()
    assert approval["ticket_id"] == "T0001"
    assert approval["status"] == "PENDING"
    assert approval["action"] == "ESCALATE"

    # ticket remains valid (status untouched, still OPEN)
    ticket = store.get("T0001")
    assert ticket is not None and ticket.status.value == "OPEN"

    # approval is independent: not stored on the ticket
    assert [e.event_type.value for e in store.events("T0001")] == ["created"]


def test_approvals_list_and_status_filter() -> None:
    client, _ = _client()
    _make_ticket(client)
    client.post("/tickets/T0001/escalate", json={**OPERATOR_ACTOR, "reason": "r1"})

    resp = client.get("/approvals")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["status"] == "PENDING"

    filtered = client.get("/approvals?status=APPROVED")
    assert filtered.json() == []


def test_approve_moves_to_approved() -> None:
    client, store = _client()
    _make_ticket(client)
    approval_id = client.post("/tickets/T0001/escalate", json=OPERATOR_ACTOR).json()["id"]

    resp = client.post(f"/approvals/{approval_id}/approve", json=APPROVER_ACTOR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["decided_by"] is not None
    assert body["decided_at"] is not None

    # ticket still valid after approval
    assert store.get("T0001").status.value == "OPEN"


def test_reject_moves_to_rejected() -> None:
    client, _ = _client()
    _make_ticket(client)
    approval_id = client.post("/tickets/T0001/escalate", json=OPERATOR_ACTOR).json()["id"]

    resp = client.post(f"/approvals/{approval_id}/reject", json={**APPROVER_ACTOR, "reason": "无需升级"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    assert resp.json()["reason"] == "无需升级"


def test_double_decision_returns_409() -> None:
    client, _ = _client()
    _make_ticket(client)
    approval_id = client.post("/tickets/T0001/escalate", json=OPERATOR_ACTOR).json()["id"]

    assert client.post(f"/approvals/{approval_id}/approve", json=APPROVER_ACTOR).status_code == 200
    assert client.post(f"/approvals/{approval_id}/approve", json=APPROVER_ACTOR).status_code == 409
    assert client.post(f"/approvals/{approval_id}/reject", json=APPROVER_ACTOR).status_code == 409


def test_unknown_approval_returns_404() -> None:
    client, _ = _client()
    assert client.post("/approvals/apr_missing/approve", json=APPROVER_ACTOR).status_code == 404


def test_invalid_approval_status_filter_returns_400() -> None:
    client, _ = _client()
    assert client.get("/approvals?status=WEIRD").status_code == 400


def test_escalate_action_whitelist() -> None:
    """V2: approvable actions are a whitelist, never free strings."""
    client, _ = _client()
    _make_ticket(client)
    resp = client.post("/tickets/T0001/escalate", json={**OPERATOR_ACTOR, "action": "emergency_restart"})
    assert resp.status_code == 400
    ok = client.post("/tickets/T0001/escalate", json={**OPERATOR_ACTOR, "action": "ESCALATE", "reason": "r"})
    assert ok.status_code == 200
    assert ok.json()["action"] == "ESCALATE"


# --- end-to-end: webhook -> operator lifecycle ---


def test_webhook_to_operator_end_to_end() -> None:
    client, store = _client()
    ticket = _make_ticket(client)

    assert client.post("/tickets/T0001/claim", json=OPERATOR_ACTOR).json()["status"] == "IN_PROGRESS"
    assert client.post("/tickets/T0001/resolve", json=OPERATOR_ACTOR).json()["status"] == "RESOLVED"

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
