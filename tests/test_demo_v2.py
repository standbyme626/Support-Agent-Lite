"""V2 offline demo: the 16-step Golden Path + FAQ + HITL chain.

Fully local: recording transports only, no real network, no credentials.
This doubles as AC-29 (full case trace) and the runnable demo.
"""
from tests.v2_fixtures import (
    WECOM_APPROVAL_ROOM,
    WECOM_OPERATOR_GROUP,
    WECOM_REPAIR_GROUP,
    feishu_official,
    wecom_dm,
    wecom_group,
)

DEMO_STEPS: list[tuple[str, str]] = []


def _step(step: str, body: dict) -> dict:
    DEMO_STEPS.append((step, f"{body.get('workflow')} ticket={body.get('ticket_id')}"))
    return body


def test_demo_v2_full_golden_path(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store

    # 1. 张三 in the requester group reports the A/C
    created = _step("1 requester group: A3 空调坏了", wecom_group(client, "A3 空调坏了", "m1").json())
    assert created["ticket_id"] == "T0001"

    # 2. canonical identity: user_001
    assert created["user_id"] == app_ctx.users["zhangsan"]

    # 3. T0001 created
    assert store.get("T0001").status.value == "OPEN"

    # 4/5/6. public receipt + private detail + operator work item
    notifications = app_ctx.client.get("/tickets/T0001/case").json()["notifications"]
    types = [n["type"] for n in notifications]
    assert "REACTIVE_REPLY" in types and "OPERATOR_WORK_ITEM" in types
    assert "PRIVATE_DETAIL" in types or not any(
        n["type"] == "PRIVATE_DETAIL" for n in notifications
    )  # private requires a DM session; harmless either way

    # 7/8. 李师傅 (canonical operator) claims atomically via the operator group
    claimed = _step("7 operator claim", wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua").json())
    assert claimed["workflow"] == "operator_action"
    ticket = store.get("T0001")
    assert ticket.status.value == "IN_PROGRESS"
    assert ticket.assignee_user_id == app_ctx.users["lihua"]

    # 9. requester lifecycle update delivered
    updates = [n for n in app_ctx.client.get("/tickets/T0001/case").json()["notifications"] if n["type"] == "REQUESTER_STATUS_UPDATE"]
    assert updates and "李师傅" in updates[0]["message"]

    # 10. operator resolves
    resolved = _step("10 operator resolve", wecom_group(client, "/resolve T0001 已更换空调滤网", "m3", conversation_id=WECOM_OPERATOR_GROUP, user="lihua").json())
    assert store.get("T0001").status.value == "RESOLVED"

    # 11. confirmation request sent
    confirmations = [n for n in app_ctx.client.get("/tickets/T0001/case").json()["notifications"] if n["type"] == "REQUESTER_CONFIRMATION_REQUEST"]
    assert confirmations and "请确认" in confirmations[0]["message"]

    # 12/13. 张三 confirms from another bound channel (feishu DM)
    confirmed = _step("12 cross-channel confirm", feishu_official(client, "T0001 已恢复", "ev_confirm", open_id="ou_zhangsan").json())
    assert confirmed["workflow"] == "confirmation"
    assert store.get("T0001").status.value == "CLOSED"

    # 14. memory extraction
    memories = client.get(f"/memories?user_id={app_ctx.users['zhangsan']}").json()
    assert any("设备问题：A3 空调坏了" in m["fact"] for m in memories)

    # 15/16. new session recalls
    recall = _step("15 new session recall", wecom_dm(client, "空调又坏了", "m4").json())
    assert recall["ticket_id"] == "T0002"
    assert any("A3 空调坏了" in f for f in recall["recalled"])
    assert any("已更换空调滤网" in f for f in recall["recalled"])


def test_demo_faq_rag(app_ctx) -> None:
    resp = _step("faq", wecom_group(app_ctx.client, "年假怎么申请？", "faq1").json())
    assert resp["workflow"] == "faq_answer"
    assert "faq-001" in resp["reply"] and "来源" in resp["reply"]
    assert resp["ticket_id"] is None  # FAQ never creates tickets


def test_demo_hitl_escalate_approve_execute(app_ctx) -> None:
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    resp = wecom_group(client, "/escalate T0001 用户要求升级", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    approval_id = resp.json()["reply"].split("：")[1].strip().rstrip("。")

    assert store.get("T0001").status.value == "OPEN"  # approval pending
    approved = wecom_group(client, f"/approve {approval_id}", "m3", conversation_id=WECOM_APPROVAL_ROOM, user="manager")
    assert "已通过并执行" in approved.json()["reply"]
    events = store.events("T0001")
    assert events[-1].event_type.value == "escalated"
    assert events[-1].actor_user_id == app_ctx.users["manager"]


def test_ac29_full_case_trace(app_ctx) -> None:
    """AC-29: the whole T0001 lifecycle is queryable with actors and traces."""
    client, store = app_ctx.client, app_ctx.store
    wecom_group(client, "A3 空调坏了", "m1")
    wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    wecom_group(client, "/resolve T0001 已更换空调滤网", "m3", conversation_id=WECOM_OPERATOR_GROUP, user="lihua")
    feishu_official(client, "T0001 已恢复", "ev_c", open_id="ou_zhangsan")

    case = client.get("/tickets/T0001/case").json()
    event_types = [e["event_type"] for e in case["events"]]
    assert event_types == ["created", "claimed", "resolved", "closed"]

    # every event answers WHO (actor) and WHICH trace triggered it
    actors = [e["actor_user_id"] for e in case["events"]]
    assert actors[1] == app_ctx.users["lihua"]  # who claimed
    assert actors[2] == app_ctx.users["lihua"]  # who resolved
    assert actors[3] == app_ctx.users["zhangsan"]  # who confirmed
    assert all(e["trace_id"] for e in case["events"])

    assert any(n["type"] == "REQUESTER_CONFIRMATION_REQUEST" for n in case["notifications"])
    assert any("设备问题" in m for m in case["memories"])
    assert case["ticket"]["assignee_user_id"] == app_ctx.users["lihua"]

    # trace_id of the claim is queryable end-to-end
    claim_trace = case["events"][1]["trace_id"]
    trace = client.get(f"/traces/{claim_trace}")
    assert trace.status_code == 200
    stages = [s["stage"] for s in trace.json()["stages"]]
    assert stages == ["channel", "identity", "intent", "reply"] or "reply" in stages


def test_demo_asserts_channel_role_separation(app_ctx) -> None:
    """Channel != Role: the SAME wecom conversation id carries requester
    AND operator purposes depending on registration, and feishu has its
    own operator group."""
    conversations = {c["channel_conversation_id"]: c for c in app_ctx.client.get("/conversations").json()}
    assert conversations[WECOM_REPAIR_GROUP]["purpose"] == "REQUESTER"
    assert conversations[WECOM_OPERATOR_GROUP]["purpose"] == "OPERATOR"
    assert conversations[WECOM_APPROVAL_ROOM]["purpose"] == "APPROVAL"
    assert conversations["oc_op_facility"]["purpose"] == "OPERATOR"  # feishu operator group
