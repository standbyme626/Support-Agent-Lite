"""C10: stats sub-agent (问数) — NL question -> constrained stat spec ->
read-only execution -> formatted answer. Stateless, read-only, own tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.application.stats_agent import (
    StatsAgent,
    parse_time_window,
    deterministic_spec,
)
from app.domain.ticket import Ticket
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.repositories import TicketStore


@pytest.fixture()
def store():
    conn = connect(":memory:")
    apply_migrations(conn)
    from app.domain.identity import User
    from app.infrastructure.repositories import TicketStore, UserRepository

    users = UserRepository(conn)
    user = users.create(User(id="u_stats", display_name="统计用户"))
    s = TicketStore(conn)
    now = datetime.now()
    # 5 open (3 this month), 2 closed (last month), categories mixed
    for i in range(3):
        s.create(Ticket(id=f"TA{i}", user_id=user.id, title="t", description="d",
                        category="network", queue="general",
                        created_at=now - timedelta(days=3)))
    for _ in range(2):
        s.create(Ticket(id=f"TB{_}", user_id=user.id, title="t", description="d",
                        category="device", queue="facility",
                        created_at=now - timedelta(days=40)))
    yield s
    conn.close()


# --- time window parsing -------------------------------------------------------


def test_parse_last_month():
    since, until = parse_time_window("last_month")
    assert since < until
    assert (datetime.now() - since).days >= 28


def test_parse_presets_all():
    assert parse_time_window("all") == (None, None)


# --- deterministic spec extraction ----------------------------------------------


def test_deterministic_spec_group_by_status():
    spec = deterministic_spec("按状态统计一下工单数量")
    assert spec["group_by"] == "status"


def test_deterministic_spec_time_and_category():
    spec = deterministic_spec("上个月网络类工单有多少个")
    assert spec["time"]["preset"] == "last_month"
    assert spec["category"] == "network"
    assert spec["metric"] == "count"


def test_deterministic_spec_status_filter():
    spec = deterministic_spec("现在有多少待处理工单")
    assert spec["status"] == "OPEN"


# --- filtered stats on the store ---------------------------------------------------


def test_store_stats_filtered_by_category_and_window(store):
    now = datetime.now()
    rows = store.stats_filtered(
        "status", category="device",
        since=(now - timedelta(days=90)).isoformat(),
        until=now.isoformat(),
    )
    assert sum(rows.values()) == 2  # only the two device tickets


def test_store_stats_filtered_rejects_bad_column(store):
    from pytest import raises

    with raises(ValueError):
        store.stats_filtered("user_id; DROP TABLE tickets")


# --- sub-agent ----------------------------------------------------------------------


class ScriptedSpecLLM:
    """Returns a canned stat spec JSON regardless of input."""

    model = "fake"

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def complete(self, system: str, user: str) -> str:
        return json.dumps(self._payload)


def test_stats_agent_llm_path_formats_answer(store):
    llm = ScriptedSpecLLM({"metric": "count", "group_by": "none",
                           "status": None, "queue": None,
                           "category": "network", "priority": None,
                           "time": {"preset": "last_7d"}})
    agent = StatsAgent(llm=llm, tickets=store)
    out = agent.run("最近一周网络类工单有几个")
    assert "3" in out.text
    assert out.spec["category"] == "network"
    assert out.rows


def test_stats_agent_no_llm_falls_back_to_rules(store):
    agent = StatsAgent(llm=None, tickets=store)
    out = agent.run("按状态统计工单")
    assert out.text and out.rows
    assert out.fallback_used is True


def test_stats_agent_invalid_llm_json_degrades(store):
    class BadLLM:
        model = "fake"

        def complete(self, system, user):  # noqa: ANN001
            return "not json at all"

    agent = StatsAgent(llm=BadLLM(), tickets=store)
    out = agent.run("上个月有多少工单")
    assert "2" in out.text or "5" in out.text  # rule-based fallback still answers
    assert out.fallback_used is True


def test_stats_agent_is_read_only(store):
    before = store.stats_grouped("status")
    agent = StatsAgent(llm=None, tickets=store)
    agent.run("按队列统计工单数量")
    assert store.stats_grouped("status") == before


# --- tool port integration ------------------------------------------------------------


def test_ask_stats_tool_registered(store):
    from pathlib import Path

    from app.application.agent_tools import AgentToolPort
    from app.application.memory_service import MemoryService
    from app.application.retriever import Retriever
    from app.infrastructure.repositories import MemoryRepository

    seed_faq = Path(__file__).resolve().parent.parent / "seed" / "faq"
    port = AgentToolPort(
        store, __import__("app.infrastructure.repositories", fromlist=["MessageRepository"]).MessageRepository(store._conn),
        Retriever(seed_faq),
        MemoryService(store, MemoryRepository(store._conn)),
        stats_agent=StatsAgent(llm=None, tickets=store),
    )
    tc = port.call(
        "ask_stats", {"question": "按状态统计工单"},
        user_id="u1", session_id="s1",
        allowed=frozenset({"ask_stats"}),
    )
    assert tc.ok and "OPEN" in tc.observation.upper() or "待处理" in tc.observation
