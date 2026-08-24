"""V2 acceptance: AC-11..AC-21 (collaboration layer).

Covers requester group create + three outputs, cross-conversation
continuation, canonical operator, claim receipts, shared operator
conversation safety, resolve->confirmation, cross-channel confirmation,
resolution rejection, memory after confirmed close, RAG no-answer
handoff.
"""
from tests.v2_fixtures import (
    WECOM_OPERATOR_GROUP,
    feishu_official,
    outbox_for,
    wecom_dm,
    wecom_group,
)


# --- AC-11: Requester Group Create -> three business outputs ---


def test_ac11_requester_group_create_three_outputs(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    body = wecom_group(client, "A3 空调坏了", "m1").json()

    assert body["ticket_id"] == "T0001"
    assert body["conversation"] == "REQUESTER"

    ticket = store.get("T0001")
    assert ticket is not None and ticket.source_conversation_id == "repair_group_1"

    notifications = outbox_for(app_ctx, "T0001")
    types = {n["type"]: n["message"] for n in notifications}
    # 1) public requester receipt: the agent-drafted reply (honest — it
    #    never claims "已私发给你"; no LLM in tests -> deterministic draft)
    receipt = types.get("REACTIVE_REPLY")
    assert receipt is not None
    assert "T0001" in receipt and "OPEN" in receipt
    assert "已私发给你" not in receipt
    # 2) private requester detail (dm session created first in fixture flow below)
    # 3) operator work item
    work_item = types.get("OPERATOR_WORK_ITEM")
    assert work_item is not None
    assert "新工单 T0001" in work_item and "报修人：张三" in work_item and "CLAIM T0001" in work_item


def test_ac11_private_detail_delivered_to_dm(app_ctx) -> None:
    """PRIVATE detail requires a requester DM conversation to exist."""
    client = app_ctx.client
    wecom_dm(client, "你好", "m0")
    body = wecom_group(client, "A3 空调坏了", "m1").json()
    assert body["ticket_id"] == "T0001"

    notifications = outbox_for(app_ctx, "T0001")
    private = [n for n in notifications if n["type"] == "PRIVATE_DETAIL"]
    assert len(private) == 1
    assert "工单：T0001" in private[0]["message"] or "工单 T0001" in private[0]["message"]
    # DM carries a natural explanation (legacy-adapted), not a bare form
    assert "情况分析" in private[0]["message"]

    # delivered via wecom message/send (touser), not appchat
    sent = [r for r in app_ctx.transport.records if r.method == "POST"]
    dm_bodies = [r.body for r in sent if "message/send" in r.url]
    assert any(b and b.get("touser") == "zhangsan" for b in dm_bodies)


def test_ac11_operator_work_item_delivered_to_operator_group(app_ctx) -> None:
    client = app_ctx.client
    wecom_group(client, "A3 空调坏了", "m1")
    app_ctx.conn  # noqa: B018

    sent = [r for r in app_ctx.transport.records if r.method == "POST"]
    group_bodies = [r.body for r in sent if "appchat/send" in r.url]
    assert any(b and b.get("chatid") == WECOM_OPERATOR_GROUP for b in group_bodies)
    work_item = next(b["text"]["content"] for b in group_bodies if b.get("chatid") == WECOM_OPERATOR_GROUP)
    assert "新工单 T0001" in work_item


# --- AC-12: Cross-conversation continuation ---


def test_ac12_cross_conversation_continuation(app_ctx) -> None:
    """Group creates T0001; a DM follow-up continues T0001, no T0002."""
    client, store = app_ctx.client, app_ctx.store
    created = wecom_group(client, "A3 空调坏了", "m1").json()
    assert created["ticket_id"] == "T0001"

    dm = wecom_dm(client, "下午三点以后我才在办公室", "m2")
    body = dm.json()
    assert body["ticket_id"] == "T0001"  # continues, never T0002
    assert [t.id for t in store.list_by_user(created["user_id"])] == ["T0001"]


# --- AC-13: Canonical operator across channels ---


def test_ac13_canonical_operator_across_channels(app_ctx) -> None:
    """wecom:lihua and feishu:ou_lihua resolve to one canonical operator;
    the operator can act from the wecom OR feishu operator group."""
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    assert client.post("/tickets/T0001/claim", json={"actor_user_id": app_ctx.users["lihua"]}).status_code == 200

    # same operator resolves the ticket from the feishu operator group
    resp = feishu_official(
        client,
        "T0001 已更换空调滤网，处理完成",
        "ev_resolve",
        open_id="ou_lihua",
        chat_type="group",
        chat_id="oc_54cd200a81624e7f6ea0a68c2a9eb03f",
    )
    assert resp.status_code == 200
    assert resp.json()["workflow"] == "operator_action"
    ticket = store.get("T0001")
    assert ticket.assignee_user_id == app_ctx.users["lihua"]
    assert ticket.status.value == "RESOLVED"
    events = store.events("T0001")
    assert events[-1].actor_user_id == app_ctx.users["lihua"]


# --- AC-14: Operator claim receipts ---


def test_ac14_operator_claim_receipts(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    resp = wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")

    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow"] == "operator_action"
    assert body["ticket_id"] == "T0001"

    ticket = store.get("T0001")
    assert ticket.assignee_user_id == app_ctx.users["lihua"]
    assert ticket.status.value == "IN_PROGRESS"
    events = store.events("T0001")
    assert [e.event_type.value for e in events] == ["created", "claimed"]
    assert events[1].actor_user_id == app_ctx.users["lihua"]

    notifications = outbox_for(app_ctx, "T0001")
    types = [n["type"] for n in notifications]
    assert "OPERATOR_ACTION_RECEIPT" in types
    assert "REQUESTER_STATUS_UPDATE" in types


# --- AC-16: shared operator conversation has no implicit ticket ---


def test_ac16_operator_no_implicit_ticket(app_ctx) -> None:
    """AC-16 (V2.1 strong form): a plain message in the shared operator
    conversation must NEVER guess an implicit ticket — no auto-resolve,
    no auto-close, no implicit work item. Deterministic routing: lihua has
    no active tickets, so the message resolves to the fixed 'other' reply."""
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    # second ticket for the same user
    user_id = app_ctx.users["zhangsan"]
    from app.application.ticket_service import TicketService

    TicketService(store).create(user_id, "VPN 连不上", "报错 619")
    store.set_operational("T0002", queue="facility")

    # operator says "处理好了" WITHOUT a ticket id -> must NOT guess
    resp = wecom_group(client, "处理好了", "m4", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    body = resp.json()
    # deterministic: "处理好了" matches no command and no requester intent
    # keyword, and the operator has no active tickets -> fixed 'other' reply
    assert body["workflow"] == "other"
    assert body["ticket_id"] is None
    assert store.get("T0001").status.value == "OPEN"  # nothing was auto-resolved
    assert store.get("T0002").status.value == "OPEN"  # nothing was auto-resolved
    assert len(store.events("T0001")) == 1  # only the original created event


# --- AC-17: Resolve -> confirmation request, not auto-close ---


def test_ac17_resolve_waits_for_requester_confirmation(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    resp = wecom_group(client, "/resolve T0001 已更换空调滤网", "m3", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")

    assert resp.status_code == 200
    ticket = store.get("T0001")
    assert ticket.status.value == "RESOLVED"  # NOT CLOSED

    notifications = outbox_for(app_ctx, "T0001")
    confirmation = [n for n in notifications if n["type"] == "REQUESTER_CONFIRMATION_REQUEST"]
    assert len(confirmation) >= 1  # requester really gets asked
    public = [n for n in confirmation if n["visibility"] == "PUBLIC"]
    assert public and "请确认" in public[0]["message"]
    assert store.get("T0001").status.value == "RESOLVED"  # NOT closed yet


# --- AC-18: Cross-channel requester confirmation (wecom create, feishu confirm) ---


def test_ac18_cross_channel_requester_confirmation(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    wecom_group(client, "/resolve T0001 已更换空调滤网", "m3", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")

    resp = feishu_official(client, "T0001 已恢复", "ev_c", open_id="ou_zhangsan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow"] == "confirmation"
    assert body["ticket_id"] == "T0001"

    ticket = store.get("T0001")
    assert ticket.status.value == "CLOSED"
    events = store.events("T0001")
    assert [e.event_type.value for e in events] == ["created", "claimed", "resolved", "closed"]
    assert events[-1].actor_user_id == app_ctx.users["zhangsan"]


# --- AC-19: Resolution rejected -> back to IN_PROGRESS, no new ticket ---


def test_ac19_resolution_rejected(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    user_id = wecom_group(client, "A3 空调坏了", "m1").json()["user_id"]
    wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    wecom_group(client, "/resolve T0001 已更换空调滤网", "m3", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")

    resp = wecom_dm(client, "还没好，空调还在响", "m4")
    body = resp.json()
    assert body["workflow"] == "rejected"
    assert body["ticket_id"] == "T0001"

    ticket = store.get("T0001")
    assert ticket.status.value == "IN_PROGRESS"  # back to processing
    events = store.events("T0001")
    assert events[-1].event_type.value == "resolution_rejected"
    assert [t.id for t in store.list_by_user(user_id)] == ["T0001"]  # no T0002


# --- AC-20: Memory after confirmed closure ---


def test_ac20_memory_after_confirmed_close(app_ctx) -> None:
    client = app_ctx.client
    wecom_group(client, "A3 空调坏了", "m1")
    wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    wecom_group(client, "/resolve T0001 已更换空调滤网", "m3", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    feishu_official(client, "T0001 已恢复", "ev_c", open_id="ou_zhangsan")

    memories = client.get(f"/memories?user_id={app_ctx.users['zhangsan']}").json()
    facts = [m["fact"] for m in memories]
    assert any(f == "设备问题：A3 空调坏了" for f in facts)
    assert any(f == "处理结果：已更换空调滤网" for f in facts)

    # new session recall (AC-10 regression, now after a confirmed closure)
    resp = wecom_dm(client, "空调又坏了", "m5", user="zhangsan")
    body = resp.json()
    assert body["ticket_id"] == "T0002"
    assert any("A3 空调坏了" in f for f in body["recalled"])


# --- AC-21: RAG no-answer real handoff ---


def test_ac21_no_answer_real_handoff(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    resp = wecom_group(client, "今天天气怎么样", "m1")
    body = resp.json()
    assert body["workflow"] == "no_answer"
    assert body["ticket_id"] == "T0001"  # truthful: a real ticket exists
    assert "转人工" in body["reply"]

    notifications = outbox_for(app_ctx, "T0001")
    types = [n["type"] for n in notifications]
    assert "OPERATOR_WORK_ITEM" in types  # operators really got the work item


# --- operator conversation can file tickets; targets split correctly ---


def test_operator_group_can_file_ticket_and_targets_split(app_ctx) -> None:
    """A plain message in an operator conversation creates a ticket; the
    public receipt goes to the requester group (same channel+queue), the
    private detail to the requester's channel identity, and the work item
    to the operator conversation."""
    client, store = app_ctx.client, app_ctx.store
    from tests.v2_fixtures import feishu_official

    # a feishu requester group must exist for the public receipt to land on
    r = client.post(
        "/conversations/register",
        json={"channel": "feishu", "channel_conversation_id": "oc_requester", "purpose": "REQUESTER",
              "conversation_type": "GROUP", "queue": "facility"},
    )
    assert r.status_code == 200

    # feishu user files a ticket from the feishu repair group
    resp = feishu_official(
        client,
        "A3 空调坏了",
        "ev_op_file",
        open_id="ou_ops",
        chat_id="oc_979f6435ef8071bc533ea6123889d712",
    ).json()
    assert resp["ticket_id"] == "T0001"

    from app.infrastructure.repositories import NotificationOutboxRepository
    from app.main import build_ops
    from tests.conftest import _outbound_clients

    ops = build_ops(app_ctx.conn, store, _outbound_clients(app_ctx.transport))
    outbox = NotificationOutboxRepository(app_ctx.conn)
    records = outbox.list_by_ticket("T0001")
    kinds = {r.notification_type.value: r.target_key for r in records}

    # public receipt -> the same feishu repair (requester) group it was filed from
    assert kinds["REACTIVE_REPLY"] == "conversation:feishu:oc_979f6435ef8071bc533ea6123889d712"
    # work item -> the feishu processing group (operator)
    assert kinds["OPERATOR_WORK_ITEM"] == "conversation:feishu:oc_54cd200a81624e7f6ea0a68c2a9eb03f"
    # private detail -> direct to the requester's feishu open_id (no DM session yet)
    assert kinds["PRIVATE_DETAIL"] == "user:feishu:ou_ops"
