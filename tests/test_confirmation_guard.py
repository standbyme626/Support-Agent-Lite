"""Confirmation/rejection keyword tightening (P0 fix 6, 2026-08-29).

"好了"/"可以了"/"修好了" used to CONFIRM and "不好" used to REJECT —
substring matches that hit progress questions ("处理好了吗"/"修好了吗")
and everyday moods ("心情不好"), auto-closing or re-opening real tickets.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import WECOM_OPERATOR_GROUP, WECOM_REPAIR_GROUP


def _resolved_ticket(client: TestClient, store) -> str:
    """Create + claim + resolve one ticket through the real channel flow."""
    body = client.post(
        "/webhooks/wecom",
        json={"MsgId": "c1", "FromUserName": "zhangsan", "Content": "A3 空调坏了",
              "CreateTime": 1000, "conversation_id": WECOM_REPAIR_GROUP},
    ).json()
    ticket_id = body["ticket_id"]
    assert ticket_id
    claim = client.post(
        "/webhooks/wecom",
        json={"MsgId": "c2", "FromUserName": "lihua", "Content": f"/claim {ticket_id}",
              "CreateTime": 2000, "conversation_id": WECOM_OPERATOR_GROUP},
    ).json()
    assert claim["workflow"] == "operator_action", claim
    resolve = client.post(
        "/webhooks/wecom",
        json={"MsgId": "c3", "FromUserName": "lihua", "Content": f"/resolve {ticket_id} 已更换配件",
              "CreateTime": 3000, "conversation_id": WECOM_OPERATOR_GROUP},
    ).json()
    assert resolve["workflow"] == "operator_action", resolve
    assert store.get(ticket_id).status.value == "RESOLVED"
    return ticket_id


def _requester(client: TestClient, msg_id: str, text: str, create_time: int):
    return client.post(
        "/webhooks/wecom",
        json={"MsgId": msg_id, "FromUserName": "zhangsan", "Content": text,
              "CreateTime": create_time, "conversation_id": WECOM_REPAIR_GROUP},
    ).json()


def test_progress_question_must_not_confirm(app_ctx):
    """'处理好了吗' is a STATUS QUESTION, not a confirmation: the ticket must
    stay RESOLVED and the user gets the status line."""
    ticket_id = _resolved_ticket(app_ctx.client, app_ctx.store)
    body = _requester(app_ctx.client, "q1", "处理好了吗", 4000)
    assert body["workflow"] == "progress", body
    assert app_ctx.store.get(ticket_id).status.value == "RESOLVED"


def test_mood_word_must_not_reject_resolution(app_ctx):
    """'心情不好' is not a rejection: the ticket must stay RESOLVED."""
    ticket_id = _resolved_ticket(app_ctx.client, app_ctx.store)
    body = _requester(app_ctx.client, "q2", "今天心情不好", 4100)
    assert body["workflow"] != "rejected", body
    assert app_ctx.store.get(ticket_id).status.value == "RESOLVED"


def test_explicit_confirm_still_closes(app_ctx):
    ticket_id = _resolved_ticket(app_ctx.client, app_ctx.store)
    body = _requester(app_ctx.client, "q3", "确认", 4200)
    assert body["workflow"] == "confirmation", body
    assert app_ctx.store.get(ticket_id).status.value == "CLOSED"


def test_real_rejection_still_reopens(app_ctx):
    ticket_id = _resolved_ticket(app_ctx.client, app_ctx.store)
    body = _requester(app_ctx.client, "q4", "还是不行，跟之前一样", 4300)
    assert body["workflow"] == "rejected", body
    assert app_ctx.store.get(ticket_id).status.value == "IN_PROGRESS"
