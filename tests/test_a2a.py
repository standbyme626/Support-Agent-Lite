"""A2A minimal surface: agent card discovery + JSON-RPC message/send."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.application.a2a import (
    A2AHandler,
    build_agent_card,
    sanitize_for_a2a,
)


@pytest.fixture(scope="module")
def client():
    from app.main import create_app

    app = create_app()
    return TestClient(app)


# --- agent card -----------------------------------------------------------------


def test_agent_card_discoverable(client):
    res = client.get("/.well-known/agent.json")
    assert res.status_code == 200
    card = res.json()
    assert card["name"] == "support-agent-lite"
    assert card["url"].endswith("/a2a/rpc")
    skills = {s["id"] for s in card["skills"]}
    assert {"faq", "progress"} <= skills


def test_card_protocol_version_present(client):
    card = client.get("/.well-known/agent.json").json()
    assert "protocolVersion" in card and card["capabilities"] is not None


# --- json-rpc message/send ----------------------------------------------------------


def _send(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}},
    }


def test_message_send_faq_returns_text(client):
    res = client.post("/a2a/rpc", json=_send("打印机脱机打印不了怎么办"))
    assert res.status_code == 200
    out = res.json()
    parts = out["result"]["parts"]
    assert parts and isinstance(parts[0]["text"], str) and parts[0]["text"]
    assert out["result"]["taskId"].startswith("task_")


def test_message_send_progress_with_ticket_id(client):
    from tests.conftest import WECOM_REPAIR_GROUP

    # create a real ticket through the webhook first
    client.post(
        "/webhooks/wecom",
        json={"MsgId": "a2a-m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了",
              "CreateTime": 1000, "conversation_id": WECOM_REPAIR_GROUP},
    )
    res = client.post("/a2a/rpc", json=_send("T0001 现在什么进度了"))
    assert res.status_code == 200
    text = res.json()["result"]["parts"][0]["text"]
    assert "T0001" in text


def test_message_send_empty_parts_is_param_error(client):
    res = client.post("/a2a/rpc", json={
        "jsonrpc": "2.0", "id": 2, "method": "message/send",
        "params": {"message": {"role": "user", "parts": []}},
    })
    assert res.status_code == 400
    assert res.json()["error"]["code"] == -32602


def test_method_not_found(client):
    res = client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": 3, "method": "agent/destroy"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == -32601


def test_tasks_get_roundtrip(client):
    sent = client.post("/a2a/rpc", json=_send("WiFi 连不上")).json()
    task_id = sent["result"]["taskId"]
    got = client.post("/a2a/rpc", json={
        "jsonrpc": "2.0", "id": 9, "method": "tasks/get", "params": {"id": task_id},
    })
    assert got.status_code == 200
    assert got.json()["result"]["status"]["state"] == "completed"


def test_bad_jsonrpc_version_rejected(client):
    res = client.post("/a2a/rpc", json={"jsonrpc": "1.0", "id": 4, "method": "message/send", "params": {}})
    assert res.json()["error"]["code"] == -32600


# --- masking ------------------------------------------------------------------------


def test_reply_masks_phone_numbers():
    leaked = "请联系 13912345678"
    assert "13912345678" not in sanitize_for_a2a(leaked)
    assert "****0000" in sanitize_for_a2a(leaked)


# --- handler unit --------------------------------------------------------------------


def test_handler_internal_error_is_wrapped():
    handler = A2AHandler(handle_message=lambda t: 1 / 0)  # type: ignore[arg-type,return-value]
    out = handler.dispatch(_send("anything"))
    assert not out.ok
    assert out.payload["error"]["code"] == -32603
