"""V2.1 offline demo: the upgraded Golden Path (multi-turn + urgency +
memory repeat) + FAQ RAG + Agent-proposed HITL + full case trace.

Fully local: recording transports only, fake scripted LLMs, no real
network. Doubles as AC-29 (full case trace) and the runnable demo.
"""
from tests.fake_llm import RecordingLLM, ScriptedLLM, make_decision
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
    """The §38 demo: requester group -> T0001 -> agent decision ->
    receipts/work item -> operator claim -> user follow-up (urgency) ->
    operator update -> resolve -> cross-channel confirm -> CLOSED ->
    memory -> new session repeat-issue with memory_refs."""
    client, store = app_ctx.client, app_ctx.store

    fake = ScriptedLLM(
        [
            make_decision(
                summary="A3 空调无法制冷",
                category="device",
                priority="normal",
                action="dispatch_repair",
                reply="工单 T0001 已记录：A3 空调无法制冷。当前状态：OPEN，我们会持续跟进。",
            ),
            make_decision(
                summary="A3 空调问题，用户表示下午领导到访，非常紧急",
                category="device",
                priority="high",
                action="dispatch_repair",
                rationale="领导到访，业务影响升级，建议优先处理",
                reply="已收到，工单 T0001 已标记紧急，会优先安排处理。",
            ),
        ]
    )
    app_ctx.with_llm(fake)

    # 1-3. requester group reports -> canonical identity -> T0001
    created = _step("1 requester group: A3 空调坏了", wecom_group(client, "A3 空调坏了", "m1").json())
    assert created["ticket_id"] == "T0001"
    assert created["user_id"] == app_ctx.users["zhangsan"]
    assert store.get("T0001").status.value == "OPEN"

    # 4-6. public receipt + private detail + operator work item
    notifications = client.get("/tickets/T0001/case").json()["notifications"]
    types = {n["type"] for n in notifications}
    assert "REACTIVE_REPLY" in types and "OPERATOR_WORK_ITEM" in types
    receipt = next(n for n in notifications if n["type"] == "REACTIVE_REPLY")
    assert "T0001" in receipt["message"] and "已私发给你" not in receipt["message"]

    # 7. user follow-up: business urgency increases (semantic, not keyword)
    follow = _step(
        "7 follow-up: 下午领导要来这里，很急",
        wecom_group(client, "下午领导要来这里，很急", "m1b").json(),
    )
    assert follow["ticket_id"] == "T0001"  # continuation, never T0002
    assert store.get("T0001").priority == "P2"  # agent's high -> P2 (policy accepted)
    operator_notes = [
        n for n in client.get("/tickets/T0001/case").json()["notifications"]
        if n["type"] == "INTERNAL_NOTE"
    ]
    assert any("Agent 更新" in n["message"] and "优先级=high" in n["message"] for n in operator_notes)

    # 8. 李师傅 claims atomically via the operator group
    claimed = _step("8 operator claim", wecom_group(client, "/claim T0001", "m2", conversation_id=WECOM_OPERATOR_GROUP, user="lihua").json())
    assert claimed["workflow"] == "operator_action"
    ticket = store.get("T0001")
    assert ticket.status.value == "IN_PROGRESS"
    assert ticket.assignee_user_id == app_ctx.users["lihua"]

    # 9. requester lifecycle update delivered
    updates = [n for n in client.get("/tickets/T0001/case").json()["notifications"] if n["type"] == "REQUESTER_STATUS_UPDATE"]
    assert updates and "李师傅" in updates[0]["message"]

    # 10. operator resolves
    _step("10 operator resolve", wecom_group(client, "/resolve T0001 已更换空调滤网", "m3", conversation_id=WECOM_OPERATOR_GROUP, user="lihua").json())
    assert store.get("T0001").status.value == "RESOLVED"

    # 11. confirmation request sent
    confirmations = [n for n in client.get("/tickets/T0001/case").json()["notifications"] if n["type"] == "REQUESTER_CONFIRMATION_REQUEST"]
    assert confirmations and "请确认" in confirmations[0]["message"]

    # 12/13. 张三 confirms from another bound channel (feishu DM)
    confirmed = _step("12 cross-channel confirm", feishu_official(client, "T0001 已恢复", "ev_confirm", open_id="ou_zhangsan").json())
    assert confirmed["workflow"] == "confirmation"
    assert store.get("T0001").status.value == "CLOSED"

    # 14. memory extraction
    memories = client.get(f"/memories?user_id={app_ctx.users['zhangsan']}").json()
    assert any("设备问题：A3 空调坏了" in m["fact"] for m in memories)

    # 15/16. new session: repeated issue -> memory reaches the agent,
    # decision references it (AC-A04 demo)
    from tests.test_agent_core import MemoryRefLLM

    app_ctx.with_llm(MemoryRefLLM())
    recall = _step("15 new session: A3 空调又不制冷了", wecom_dm(client, "A3 空调又坏了", "m4").json())
    assert recall["ticket_id"] == "T0002"
    assert any("A3 空调坏了" in f for f in recall["recalled"])
    trace = client.get(f"/traces/{recall['trace_id']}").json()
    agent_run = next(s for s in trace["stages"] if s["stage"] == "agent")
    assert agent_run["payload"]["memory_refs"] != []  # memory really influenced this run
    assert "控制板" in agent_run["payload"].get("rationale", "") or agent_run["payload"]["memory_refs"]


def test_demo_faq_rag(app_ctx) -> None:
    """FAQ query -> grounded Agent answer -> source -> NO Ticket."""
    resp = _step("faq", wecom_group(app_ctx.client, "年假怎么申请？", "faq1").json())
    assert resp["workflow"] == "faq_answer"
    assert "faq-001" in resp["reply"] and "来源" in resp["reply"]
    assert resp["ticket_id"] is None  # FAQ never creates tickets
    # grounded answer was delivered to the requester conversation via outbox
    sent = [r for r in app_ctx.transport.records if r.method == "POST"]
    group_bodies = [b for b in [r.body for r in sent if "appchat/send" in r.url]]
    assert any(b and "年假" in b.get("text", {}).get("content", "") for b in group_bodies)


def test_demo_unknown_knowledge_real_handoff(app_ctx) -> None:
    """Unknown knowledge query -> low confidence -> real Ticket + work item."""
    resp = _step("unknown", wecom_group(app_ctx.client, "今天天气怎么样", "unk1").json())
    assert resp["workflow"] == "no_answer"
    assert resp["ticket_id"] == "T0001"  # real ticket
    notifications = app_ctx.client.get("/tickets/T0001/case").json()["notifications"]
    assert any(n["type"] == "OPERATOR_WORK_ITEM" for n in notifications)


def test_demo_agent_proposes_hitl_executes(app_ctx) -> None:
    """§40 demo: Agent proposes ESCALATE -> Policy -> PendingAction ->
    Approval -> approver approves -> deterministic executor -> event."""
    client, store = app_ctx.client, app_ctx.store
    fake = ScriptedLLM(
        [
            make_decision(
                proposal={"action": "ESCALATE", "reason": "重复故障，需升级到厂家", "confidence": 0.92},
                reply="已登记工单 T0001，建议升级处理。",
            )
        ]
    )
    app_ctx.with_llm(fake)
    created = _step("agent proposal", wecom_group(client, "A3 空调坏了", "prop1").json())
    assert created["ticket_id"] == "T0001"
    assert store.get("T0001").status.value == "OPEN"  # proposal has NO effect

    approval_id = client.get("/approvals").json()[0]["id"]
    assert approval_id.startswith("apr_")

    approved = _step(
        "approver approves",
        wecom_group(client, f"/approve {approval_id}", "prop2", conversation_id=WECOM_APPROVAL_ROOM, user="manager").json(),
    )
    assert "已通过并执行" in approved["reply"]
    events = store.events("T0001")
    assert events[-1].event_type.value == "escalated"
    assert events[-1].actor_user_id == app_ctx.users["manager"]  # the agent never approves


def test_demo_hitl_escalate_approve_execute(app_ctx) -> None:
    """Operator-initiated HITL chain (unchanged V2 behavior)."""
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
    """AC-29 (V2.1 closure): the whole T0001 lifecycle is queryable with
    actors, traces, approvals, pending actions and delivery attempts."""
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

    # V2.1 closure: approvals + pending actions + delivery attempts are
    # part of the case trace now
    assert "approvals" in case and case["approvals"] == []
    assert "pending_actions" in case and case["pending_actions"] == []
    for notification in case["notifications"]:
        assert "delivery_attempts" in notification
        assert notification["attempt_count"] >= 1  # dispatch ran
        assert len(notification["delivery_attempts"]) == notification["attempt_count"]

    # trace_id of the claim is queryable end-to-end
    claim_trace = case["events"][1]["trace_id"]
    trace = client.get(f"/traces/{claim_trace}")
    assert trace.status_code == 200
    stages = [s["stage"] for s in trace.json()["stages"]]
    assert "reply" in stages


def test_demo_asserts_channel_role_separation(app_ctx) -> None:
    """Channel != Role: the SAME wecom conversation id carries requester
    AND operator purposes depending on registration, and feishu has its
    own operator group."""
    conversations = {c["channel_conversation_id"]: c for c in app_ctx.client.get("/conversations").json()}
    assert conversations[WECOM_REPAIR_GROUP]["purpose"] == "REQUESTER"
    assert conversations[WECOM_OPERATOR_GROUP]["purpose"] == "OPERATOR"
    assert conversations[WECOM_APPROVAL_ROOM]["purpose"] == "APPROVAL"
    assert conversations["oc_979f6435ef8071bc533ea6123889d712"]["purpose"] == "REQUESTER"  # 维修群 = 上报入口
    assert conversations["oc_54cd200a81624e7f6ea0a68c2a9eb03f"]["purpose"] == "OPERATOR"  # 工单群 = 处理群
