"""C5: GET /api/chat/stream — SSE 流式端点。

流式语义(诚实版):决策 JSON 是单一 schema 契约,token 级拆流会重蹈
截断 tool_calls 的覆辙(B5 防御立场),因此流的是管线阶段事件
(received→prepared→agent→completed),最终回复在 done 事件交付;
webhooks 路径不受影响。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import build_ingress, build_ops, create_app


def _client():
    ingress, conn, store = build_ingress(db_path=":memory:")
    ops = build_ops(conn, store)
    return TestClient(create_app(ingress, ops)), store


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs."""
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        event = "message"
        data = ""
        for line in lines:
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data += line[len("data:"):].strip()
        if data:
            events.append((event, json.loads(data)))
    return events


def test_missing_text_returns_400() -> None:
    client, _ = _client()
    resp = client.get("/api/chat/stream")
    assert resp.status_code == 400


def test_stream_happy_path_stages_then_done() -> None:
    client, store = _client()
    resp = client.get(
        "/api/chat/stream",
        params={"text": "A3 空调坏了，不制冷", "user": "zhangsan", "message_id": "sse-1"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    stages = [data["stage"] for event, data in events if event == "stage"]

    # 阶段事件按管线顺序出现(received 最先,completed 最后;中间含 agent 阶段)
    assert stages[0] == "received"
    assert "prepared" in stages
    assert stages[-1] == "completed"

    done = [data for event, data in events if event == "done"]
    assert len(done) == 1
    payload = done[0]
    assert payload["duplicate"] is False
    assert payload["reply"], "final reply must be delivered in done"
    assert payload["ticket_id"] == "T0001"  # 全链路:报修 → 工单创建
    assert payload["trace_id"]
    assert store.get("T0001") is not None


def test_stream_deterministic_path_without_agent() -> None:
    """progress 查询走确定性路径,不经过 agent 阶段,直接 completed。"""
    client, _ = _client()
    resp = client.get(
        "/api/chat/stream",
        params={"text": "帮我查一下我的工单进度", "user": "lisi", "message_id": "sse-2"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    stages = [data["stage"] for event, data in events if event == "stage"]
    assert "agent_started" not in stages
    assert stages[0] == "received" and stages[-1] == "completed"
    done = [data for event, data in events if event == "done"][0]
    assert done["reply"]


def test_stream_duplicate_message_id_is_noop() -> None:
    client, store = _client()
    params = {"text": "A3 打印机卡纸", "user": "wangwu", "message_id": "sse-dup"}
    first = client.get("/api/chat/stream", params=params)
    second = client.get("/api/chat/stream", params=params)

    done_first = [d for e, d in _parse_sse(first.text) if e == "done"][0]
    done_second = [d for e, d in _parse_sse(second.text) if e == "done"][0]
    assert done_first["duplicate"] is False
    assert done_second["duplicate"] is True
    assert done_second["ticket_id"] is None  # 幂等:不重复创建工单
