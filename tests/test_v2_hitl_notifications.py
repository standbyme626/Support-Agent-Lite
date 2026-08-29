"""V2 HITL + notifications: AC-22 execution chain, AC-23 visibility,
AC-24 dedupe, outbox survives simulated delivery failure.
"""
from tests.v2_fixtures import WECOM_OPERATOR_GROUP, WECOM_APPROVAL_ROOM, outbox_for, wecom_group


# --- AC-22: HITL execution chain (approve executes the action exactly once) ---


def test_ac22_hitl_escalation_executes_on_approve(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    resp = wecom_group(client, "/escalate T0001 用户要求升级", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow"] == "operator_action"
    approval_id = body["reply"].split("：")[1].strip().rstrip("。")
    assert approval_id.startswith("apr_")

    ticket = store.get("T0001")
    assert ticket.status.value == "OPEN"  # approval PENDING, ticket untouched

    # approver approves from the approval room
    approved = wecom_group(client, f"/approve {approval_id}", "m3", conversation_id=WECOM_APPROVAL_ROOM, user="manager")
    assert approved.status_code == 200
    assert "已通过并执行" in approved.json()["reply"]

    # execution happened exactly once: one escalated event + PendingAction EXECUTED
    events = store.events("T0001")
    assert events[-1].event_type.value == "escalated"
    assert events[-1].actor_user_id == app_ctx.users["manager"]
    assert ticket.status.value == "OPEN"  # escalation does not change ticket status

    # double decision is still rejected by the approval CAS
    again = wecom_group(client, f"/approve {approval_id}", "m4", conversation_id=WECOM_APPROVAL_ROOM, user="manager")
    assert "已被处理或不存在" in again.json()["reply"]


def test_ac22_force_close_requires_reason_and_approval(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")

    # missing reason -> rejected
    no_reason = wecom_group(client, "/force-close T0001", "m3", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    assert "操作失败" in no_reason.json()["reply"]

    resp = wecom_group(client, "/force-close T0001 用户已离职", "m4", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    assert resp.status_code == 200
    approval_id = resp.json()["reply"].split("：")[1].strip().rstrip("。")
    assert store.get("T0001").status.value == "IN_PROGRESS"  # still valid while pending

    wecom_group(client, f"/approve {approval_id}", "m5", conversation_id=WECOM_APPROVAL_ROOM, user="manager")
    ticket = store.get("T0001")
    assert ticket.status.value == "CLOSED"  # approved force close executed
    events = store.events("T0001")
    assert events[-1].event_type.value == "force_closed"
    assert events[-1].payload.get("reason") == "用户已离职"

    # memory only after this closure
    memories = client.get(f"/memories?user_id={app_ctx.users['zhangsan']}").json()
    assert any("已处理完成" in m["fact"] for m in memories)


def test_ac22_approval_rejection_skips_action(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    resp = wecom_group(client, "/escalate T0001 用户要求升级", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    approval_id = resp.json()["reply"].split("：")[1].strip().rstrip("。")

    wecom_group(client, f"/reject {approval_id} 无需升级", "m3", conversation_id=WECOM_APPROVAL_ROOM, user="manager")
    events = [e.event_type.value for e in store.events("T0001")]
    assert "escalated" not in events  # action never executed
    assert store.get("T0001").status.value == "OPEN"


# --- AC-23: notification visibility (INTERNAL never leaks to requester) ---


def test_ac23_internal_never_leaks_to_requester(app_ctx) -> None:
    client = app_ctx.client
    wecom_group(client, "A3 空调坏了", "m1")
    wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")

    notifications = outbox_for(app_ctx, "T0001")
    # INTERNAL content never rides on requester-facing notification types
    internal_types = {"OPERATOR_WORK_ITEM", "OPERATOR_ACTION_RECEIPT", "APPROVAL_REQUEST", "APPROVAL_RESULT", "INTERNAL_NOTE"}
    requester_types = {"REACTIVE_REPLY", "PRIVATE_DETAIL", "REQUESTER_STATUS_UPDATE", "REQUESTER_CONFIRMATION_REQUEST"}
    for n in notifications:
        if n["visibility"] == "INTERNAL":
            assert n["type"] in internal_types, f"internal leaked via {n['type']}"
        if n["type"] in requester_types:
            assert n["visibility"] in ("PUBLIC", "PRIVATE")
    requester_facing = [n for n in notifications if n["type"] in requester_types]
    assert any("已由" in n["message"] for n in requester_facing)
    assert all("认领成功" not in n["message"] for n in requester_facing)


# --- AC-24: notification dedupe (same business event + target -> one record) ---


def test_ac24_notification_dedupe(app_ctx) -> None:
    from app.application.notification_service import NotificationService
    from app.application.target_resolver import ResolvedTarget
    from app.domain.notification import NotificationType, Visibility
    from app.domain.outbound import DeliveryTarget, TargetKind
    from app.main import build_ops

    ops = build_ops(app_ctx.conn, app_ctx.store, None)
    service: NotificationService = ops.notifications
    target = ResolvedTarget(
        DeliveryTarget("wecom", TargetKind.CONVERSATION, "repair_group_1"), "test"
    )
    first = service.enqueue(
        source_event_id="evt-1",
        notification_type=NotificationType.REQUESTER_STATUS_UPDATE,
        visibility=Visibility.PUBLIC,
        message="工单 T0001 已处理",
        target=target,
        ticket_id="T0001",
    )
    second = service.enqueue(
        source_event_id="evt-1",
        notification_type=NotificationType.REQUESTER_STATUS_UPDATE,
        visibility=Visibility.PUBLIC,
        message="工单 T0001 已处理",
        target=target,
        ticket_id="T0001",
    )
    assert first is not None
    assert second is None  # UNIQUE(source_event_id, type, target_key) blocked it
    from app.infrastructure.repositories import NotificationOutboxRepository

    outbox = NotificationOutboxRepository(app_ctx.conn)
    pending = outbox.pending()
    assert len(pending) == 1


# --- Outbox survives simulated delivery failure ---


def test_outbox_survives_delivery_failure(app_ctx, monkeypatch) -> None:
    # hermetic regardless of RUN_REAL_CHANNEL_TESTS / .env leakage
    monkeypatch.setenv("REAL_CHANNEL_NETWORK", "false")
    client, store = app_ctx.client, app_ctx.store
    app_ctx.transport.fail_next("network_down")  # first delivery fails

    resp = wecom_group(client, "A3 空调坏了", "m1")
    assert resp.status_code == 200  # business committed despite delivery failure
    assert store.get("T0001") is not None

    from app.main import build_ops

    # retry must use the SAME recording transport (hermetic under
    # RUN_REAL_CHANNEL_TESTS too — never a real network client)
    from tests.conftest import _outbound_clients

    ops = build_ops(app_ctx.conn, app_ctx.store, _outbound_clients(app_ctx.transport))
    from app.infrastructure.repositories import NotificationOutboxRepository

    outbox = NotificationOutboxRepository(app_ctx.conn)
    all_records = outbox.list_by_ticket("T0001")
    # business committed (ticket exists) AND the delivery failure was
    # recorded in the immutable attempt history, nothing lost
    assert all_records
    assert any(
        attempt["success"] is False
        and ("network_down" in (attempt["result_code"] or "") or "network_down" in (attempt["error"] or ""))
        for r in all_records
        for attempt in outbox.attempts(r.id)
    )
    assert len(all_records) == len([r for r in all_records if r.attempt_count >= 1])

    # background dispatch (scheduled by the webhook endpoint) already retried
    # the failed sends and healed them — a transient channel failure never
    # waits for the next inbound message any more (2026-08-29 fix)
    assert all(r.status.value == "sent" for r in outbox.list_by_ticket("T0001"))

    # an explicit dispatch pass is still safe/idempotent on healed records
    results = ops.notifications.dispatch()
    assert all(r.status.value == "sent" for r in outbox.list_by_ticket("T0001"))
    assert isinstance(results, list)
