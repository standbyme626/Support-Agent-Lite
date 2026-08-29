"""Background dispatch (P0 fix, 2026-08-29): outbound channel HTTP must
never block the webhook response, and failed sends must be retried by the
worker instead of piggybacking on the next inbound message."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from tests.conftest import WECOM_REPAIR_GROUP


def _wecom_payload(msg_id: str, text: str) -> dict:
    return {
        "MsgId": msg_id,
        "FromUserName": "zhangsan",
        "Content": text,
        "CreateTime": 1000,
        "conversation_id": WECOM_REPAIR_GROUP,
    }


def test_auto_dispatch_false_keeps_outbox_pending_until_manual_dispatch():
    """auto_dispatch=False: process() commits business effects + outbox but
    does NOT deliver; an explicit dispatch() (background task / worker) does."""
    from app.adapters.outbound import FeishuOutboundClient, WeComOutboundClient
    from app.adapters.transports import HttpTransport
    from app.main import build_ingress, build_ops

    transport = HttpTransport()
    clients = {"wecom": WeComOutboundClient(transport=transport), "feishu": FeishuOutboundClient(transport=transport)}
    ingress, conn, store = build_ingress(
        db_path=":memory:", outbound_clients=clients, auto_dispatch=False
    )
    ops = build_ops(conn, store, clients)

    from app.domain.conversation import ConversationPurpose, ConversationType

    ops.conversations.register(
        channel="wecom",
        channel_conversation_id=WECOM_REPAIR_GROUP,
        conversation_type=ConversationType.GROUP,
        purpose=ConversationPurpose.REQUESTER,
        queue="facility",
    )

    result = ingress.process("wecom", _wecom_payload("bd-1", "A3 空调坏了"))
    assert result.duplicate is False
    assert result.downstream is not None and result.downstream.ticket is not None

    records = ops.notifications.list_for_ticket(result.downstream.ticket.id)
    assert records, "business event must enqueue outbox records in-process"
    assert all(r.status.value == "pending" for r in records), "auto_dispatch=False must not deliver inline"

    ops.notifications.dispatch()
    records = ops.notifications.list_for_ticket(result.downstream.ticket.id)
    assert all(r.status.value == "sent" for r in records)
    conn.close()


def test_dispatch_worker_sweeps_periodically_and_stops():
    from app.application.notification_service import DispatchWorker

    class _CounterNotifications:
        def __init__(self) -> None:
            self.calls = 0

        def dispatch(self) -> list[str]:
            self.calls += 1
            return []

    notifications = _CounterNotifications()
    worker = DispatchWorker(notifications, interval=0.05)
    worker.start()
    worker.start()  # idempotent: second start must not spawn a second loop
    time.sleep(0.25)
    worker.stop()
    calls_after_stop = notifications.calls
    assert calls_after_stop >= 2, "worker must sweep on its own cadence"
    time.sleep(0.15)
    assert notifications.calls == calls_after_stop, "stop() must end the sweep loop"


def test_production_app_delivers_via_background_task(monkeypatch):
    """The lazy production app (auto_dispatch=False) still delivers: the
    webhook endpoint schedules dispatch as a background task, and TestClient
    runs background tasks before the response is returned."""
    monkeypatch.setenv("SUPPORT_AGENT_DB", ":memory:")
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/wecom",
            json=_wecom_payload("bd-2", "A3 空调坏了"),
        )
        assert resp.status_code == 200, resp.text
        ticket_id = resp.json()["ticket_id"]
        assert ticket_id

        records = app.state.ops.notifications.list_for_ticket(ticket_id)
        assert records, "outbox records must exist"
        assert all(r.status.value == "sent" for r in records), (
            "background dispatch must have delivered after the response"
        )
