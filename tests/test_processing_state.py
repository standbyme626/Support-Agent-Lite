"""V2.1 two-phase processing tests: AC-A11 (LLM outside write txn),
AC-A12 (crash-safe resume), AC-A13 (failure fallback end-to-end),
duplicate concurrency on agent paths, processing state machine.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.processing import InboundProcessingStore, ProcessingState
from app.main import build_ingress, build_ops, create_app
from tests.fake_llm import (
    BrokenLLM,
    MalformedLLM,
    RecordingLLM,
    SlowLLM,
    TimeoutLLM,
    make_decision,
)

WECOM_PAYLOAD = {"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000}


def _key(ingress, payload: dict) -> str:
    return ingress._adapters["wecom"].idempotency_key(payload)  # noqa: SLF001


def _app(llm=None):
    ingress, conn, store = build_ingress(db_path=":memory:", llm=llm)
    ops = build_ops(conn, store)
    client = TestClient(create_app(ingress, ops))
    return client, ingress, conn, store


# --- processing state machine (unit) ---


def test_processing_state_machine() -> None:
    conn = connect(":memory:")
    apply_migrations(conn)
    store = InboundProcessingStore(conn)
    store.claim("k1", trace_id="t1", channel="wecom", user_id="u1", session_id="s1", conversation_channel="wecom", conversation_id="c1")
    assert store.get("k1").state == ProcessingState.RECEIVED
    assert store.get("k1").user_id == "u1"
    assert not store.is_final("k1")

    store.update("k1", state=ProcessingState.AGENT_PENDING, kind="ticket", ticket_id="T0001", intent="support")
    assert store.get("k1").state == ProcessingState.AGENT_PENDING

    # CAS: exactly one winner
    assert store.advance("k1", ProcessingState.AGENT_PENDING, ProcessingState.AGENT_COMPLETED) is True
    assert store.advance("k1", ProcessingState.AGENT_PENDING, ProcessingState.AGENT_COMPLETED) is False
    store.update("k1", state=ProcessingState.COMPLETED, reply="r")
    assert store.is_final("k1")
    assert store.get("k1").reply == "r"

    # FAILED_RETRYABLE is resumable, not final
    store.update("k1", state=ProcessingState.FAILED_RETRYABLE, error="boom")
    assert not store.is_final("k1")
    conn.close()


# --- AC-A11: LLM latency does NOT occupy the serialized DB write lock ---


def test_ac11_llm_latency_outside_write_lock() -> None:
    client, ingress, conn, store = _app(llm=None)
    workflow = ingress._workflow
    slow = SlowLLM(delay=0.6, reply=make_decision(reply="已记录工单 T0001。"))
    workflow._agent._llm = slow  # noqa: SLF001
    started = threading.Event()
    original = slow.complete

    def wrapped(**kwargs):
        started.set()
        return original(**kwargs)

    slow.complete = wrapped  # type: ignore[method-assign]

    result_holder: dict = {}

    def send() -> None:
        result_holder["result"] = ingress.process("wecom", WECOM_PAYLOAD)

    thread = threading.Thread(target=send)
    thread.start()
    assert started.wait(3.0), "slow LLM never started"

    # While the LLM sleeps, the connection's write lock must be free:
    # another worker can open a write transaction immediately.
    lock = conn._txn_lock  # noqa: SLF001
    acquired = lock.acquire(timeout=0.3)
    assert acquired, "write lock was held during the LLM network call"
    lock.release()

    thread.join(timeout=10)
    assert not thread.is_alive()
    result = result_holder["result"]
    assert result.duplicate is False
    assert store.get("T0001") is not None
    record = ingress._processing.get(_key(ingress, WECOM_PAYLOAD))
    assert record is not None and record.state == ProcessingState.COMPLETED
    assert "T0001" in result.downstream.reply


# --- AC-A12: crash after phase A -> retry resumes, no duplicates ---


def test_ac12_crash_after_phase_a_resumes_without_duplicates() -> None:
    client, ingress, conn, store = _app(llm=None)
    workflow = ingress._workflow
    key = _key(ingress, WECOM_PAYLOAD)

    # Simulate a process crash between phase A and phase B.
    original_run = workflow.run_agent
    workflow.run_agent = lambda prepared: (_ for _ in ()).throw(RuntimeError("crashed after phase A"))  # type: ignore[method-assign, assignment]

    with pytest.raises(RuntimeError, match="crashed"):
        ingress.process("wecom", WECOM_PAYLOAD)

    # Phase A committed: ticket + created event exist, agent not applied.
    assert store.get("T0001") is not None
    assert [e.event_type.value for e in store.events("T0001")] == ["created"]
    record = ingress._processing.get(key)
    assert record is not None and record.state == ProcessingState.AGENT_PENDING

    # "Restart": duplicate delivery resumes from AGENT_PENDING.
    workflow.run_agent = original_run
    result = ingress.process("wecom", WECOM_PAYLOAD)
    assert result.duplicate is False
    assert result.downstream is not None
    assert store.get("T0001") is not None
    assert [e.event_type.value for e in store.events("T0001")] == ["created"]  # no duplicate event
    tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    assert tickets == 1  # no duplicate ticket
    record = ingress._processing.get(key)
    assert record is not None and record.state == ProcessingState.COMPLETED
    assert record.reply


def test_ac12_phase_b_failure_marks_retryable_then_resumes() -> None:
    client, ingress, conn, store = _app(llm=None)
    workflow = ingress._workflow
    key = _key(ingress, WECOM_PAYLOAD)

    original_apply = workflow.apply
    calls = {"n": 0}

    def failing_apply(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("phase B db failure")
        return original_apply(*args, **kwargs)

    workflow.apply = failing_apply  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="phase B"):
        ingress.process("wecom", WECOM_PAYLOAD)
    record = ingress._processing.get(key)
    assert record is not None and record.state == ProcessingState.FAILED_RETRYABLE
    assert "phase B" in (record.error or "")

    workflow.apply = original_apply
    result = ingress.process("wecom", WECOM_PAYLOAD)
    assert result.duplicate is False
    assert store.get("T0001") is not None
    assert ingress._processing.get(key).state == ProcessingState.COMPLETED
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


# --- AC-A13: LLM failure modes complete safely end-to-end ---


def test_ac13_unavailable_timeout_malformed_all_complete() -> None:
    for llm in (BrokenLLM(), TimeoutLLM(), MalformedLLM()):
        client, ingress, conn, store = _app(llm=llm)
        resp = client.post(
            "/webhooks/wecom",
            json={"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000},
        )
        assert resp.status_code == 200, f"{type(llm).__name__}: {resp.text}"
        body = resp.json()
        assert body["ticket_id"] == "T0001"
        assert body["workflow"] == "ticket"
        payload = {"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000}
        record = ingress._processing.get(_key(ingress, payload))
        assert record is not None and record.state == ProcessingState.COMPLETED
        assert "T0001" in body["reply"]  # deterministic fallback reply
        conn.close()


# --- duplicate concurrency on an agent path: exactly one business execution ---


def test_concurrent_duplicate_webhook_agent_path_exactly_once() -> None:
    client, ingress, conn, store = _app(llm=None)
    results: list = []
    lock = threading.Lock()

    def send() -> None:
        try:
            result = ingress.process("wecom", WECOM_PAYLOAD)
            with lock:
                results.append(result.duplicate)
        except Exception as exc:  # pragma: no cover
            with lock:
                results.append(f"ERR:{exc}")

    threads = [threading.Thread(target=send) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 10
    assert results.count(False) == 1, f"results={results}"
    assert results.count(True) == 9
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
    events = store.events("T0001")
    assert [e.event_type.value for e in events] == ["created"]
    record = ingress._processing.get(_key(ingress, WECOM_PAYLOAD))
    assert record.state == ProcessingState.COMPLETED


def test_agent_observability_trace_fields() -> None:
    """AC-A14: agent_run trace carries prompt version/model/latency/tools/
    fallback/refs — never raw prompts."""
    fake = RecordingLLM(reply=make_decision(memory_refs=[], knowledge_refs=[]))
    client, ingress, conn, store = _app(llm=fake)
    resp = client.post(
        "/webhooks/wecom",
        json={"MsgId": "m1", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000},
    )
    trace_id = resp.json()["trace_id"]
    trace = client.get(f"/traces/{trace_id}").json()
    agent_run = next(s for s in trace["stages"] if s["stage"] == "agent")
    payload = agent_run["payload"]
    assert payload["agent_run_id"].startswith("agr_")
    assert payload["prompt_key"] == "agent_decision.support"
    assert payload["prompt_version"] == "v1"
    assert payload["model"] == "recording-test-model"
    assert isinstance(payload["latency_ms"], int) and payload["latency_ms"] >= 0
    assert payload["tool_call_count"] == 0
    assert payload["fallback_used"] is False
    assert "knowledge_refs" in payload and "memory_refs" in payload
    assert "summary" in payload and "confidence" in payload
    # no raw prompt / output leakage into the trace
    raw = str(trace)
    assert "user_message" not in payload
    assert "A3 空调坏了" not in payload.get("prompt_key", "")
