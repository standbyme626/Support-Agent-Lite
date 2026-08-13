"""Phase 3 tests: channel adapters, ingress idempotency (AC-03), webhooks."""
from fastapi.testclient import TestClient

from app.adapters.feishu import FeishuAdapter
from app.adapters.wecom import WeComAdapter
from app.application.ingress_service import IngressService
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.idempotency import IdempotencyStore
from app.main import build_ingress, create_app


# --- Adapter parsing ---


def test_wecom_build_inbound() -> None:
    adapter = WeComAdapter()
    env = adapter.build_inbound(
        {"MsgId": "msg_1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000}
    )
    assert env.channel == "wecom"
    assert env.channel_user_id == "zhangsan"
    assert env.text == "A3 空调坏了"
    assert env.message_id == "msg_1"


def test_wecom_idempotency_key() -> None:
    adapter = WeComAdapter()
    assert adapter.idempotency_key({"MsgId": "m1"}) == "wecom:m1"
    assert adapter.idempotency_key({"FromUserName": "zhangsan", "CreateTime": 1000}) == "wecom:zhangsan:1000"


def test_feishu_build_inbound() -> None:
    adapter = FeishuAdapter()
    env = adapter.build_inbound(
        {
            "event_id": "evt_1",
            "event": {
                "message": {"message_id": "om_1", "text": "昨天空调那个事情怎么样了？"},
                "sender": {"sender_id": {"open_id": "ou_001"}},
                "chat_id": "oc_1",
            },
        }
    )
    assert env.channel == "feishu"
    assert env.channel_user_id == "ou_001"
    assert env.conversation_id == "oc_1"
    assert env.message_id == "om_1"


def test_feishu_idempotency_key() -> None:
    adapter = FeishuAdapter()
    assert adapter.idempotency_key({"event": {"message": {"message_id": "om_1"}}}) == "feishu:om_1"
    assert adapter.idempotency_key({"event_id": "evt_1"}) == "feishu:evt_1"


# --- Ingress + idempotency (AC-03) ---


def _build_ingress():
    conn = connect(":memory:")
    apply_migrations(conn)
    from app.application.identity_service import IdentityResolver
    from app.application.session_service import SessionService
    from app.infrastructure.repositories import ChannelIdentityRepository, SessionRepository, UserRepository

    users = UserRepository(conn)
    identities = ChannelIdentityRepository(conn)
    sessions = SessionRepository(conn)
    idem = IdempotencyStore(conn)
    service = IngressService(
        adapters={"wecom": WeComAdapter(), "feishu": FeishuAdapter()},
        identity=IdentityResolver(users, identities),
        sessions=SessionService(sessions),
        idempotency=idem,
    )
    return service, conn


def test_ingress_dedupes_same_message_id() -> None:
    """AC-03: same message_id twice -> one processed, one duplicate."""
    service, conn = _build_ingress()
    payload = {"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000}

    first = service.process("wecom", payload)
    second = service.process("wecom", payload)

    assert first.duplicate is False
    assert second.duplicate is True
    assert first.user.id == second.user.id
    assert IdempotencyStore(conn).count() == 1


def test_ingress_different_messages_not_deduped() -> None:
    service, _ = _build_ingress()
    r1 = service.process("wecom", {"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000})
    r2 = service.process("wecom", {"MsgId": "m2", "FromUserName": "zhangsan", "Content": "VPN 连不上", "CreateTime": 2000})
    assert r1.duplicate is False
    assert r2.duplicate is False


# --- Webhook endpoints ---


def test_webhook_wecom_endpoint() -> None:
    ingress, _, _ = build_ingress(db_path=":memory:")
    client = TestClient(create_app(ingress))
    resp = client.post(
        "/webhooks/wecom",
        json={"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["duplicate"] is False
    assert body["user_id"].startswith("user_")


def test_webhook_wecom_duplicate_returns_202() -> None:
    ingress, _, _ = build_ingress(db_path=":memory:")
    client = TestClient(create_app(ingress))
    payload = {"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000}
    assert client.post("/webhooks/wecom", json=payload).status_code == 200
    resp = client.post("/webhooks/wecom", json=payload)
    assert resp.status_code == 202
    assert resp.json()["duplicate"] is True


def test_webhook_feishu_endpoint() -> None:
    ingress, _, _ = build_ingress(db_path=":memory:")
    client = TestClient(create_app(ingress))
    resp = client.post(
        "/webhooks/feishu",
        json={
            "event_id": "evt_1",
            "event": {
                "message": {"message_id": "om_1", "text": "昨天空调那个事情怎么样了？"},
                "sender": {"sender_id": {"open_id": "ou_001"}},
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_webhook_bad_payload_returns_400() -> None:
    ingress, _, _ = build_ingress(db_path=":memory:")
    client = TestClient(create_app(ingress))
    resp = client.post("/webhooks/wecom", json={"MsgId": "m1", "Content": "缺 FromUserName"})
    assert resp.status_code == 400


def test_webhook_unknown_channel_returns_404() -> None:
    ingress, _, _ = build_ingress(db_path=":memory:")
    client = TestClient(create_app(ingress))
    resp = client.post("/webhooks/telegram", json={})
    assert resp.status_code == 404
