"""B-batch tools: similar_tickets index + case_timeline."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.application.agent_tools import TOOL_JSON_SCHEMAS, ALLOWED_TOOLS, AgentToolDenied, AgentToolPort
from app.application.memory_service import MemoryService
from app.application.retriever import Retriever
from app.application.ticket_insights import TicketSimilarityIndex, format_case_timeline
from app.domain.identity import User
from app.domain.ticket import Ticket
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.repositories import MemoryRepository, MessageRepository, TicketStore, UserRepository

SEED_FAQ = Path(__file__).resolve().parent.parent / "seed" / "faq"


class FakeEmbedding:
    """Same-char hashing vectors; '空调' text clusters together."""

    def embed(self, texts):  # noqa: ANN001
        vecs = []
        for t in texts:
            v = [0.0] * 16
            for ch in t:
                v[ord(ch) % 16] += 1.0
            vecs.append(v)
        return vecs


@pytest.fixture()
def env():
    conn = connect(":memory:")
    apply_migrations(conn)
    store = TicketStore(conn)
    user = UserRepository(conn).create(User(id="u_si", display_name="相似测试"))
    now = datetime.now()
    t1 = store.create(Ticket(id="T8001", user_id=user.id, title="会议室空调漏水",
                             description="d", category="device", queue="facility",
                             created_at=now - timedelta(days=10)))
    t2 = store.create(Ticket(id="T8002", user_id=user.id, title="WiFi 断网",
                             description="d", category="network", queue="general",
                             created_at=now - timedelta(days=5)))
    yield {"conn": conn, "store": store, "user_id": user.id}
    conn.close()


def _tools(env, index=None) -> AgentToolPort:
    return AgentToolPort(
        env["store"], MessageRepository(env["conn"]), Retriever(SEED_FAQ),
        MemoryService(env["store"], MemoryRepository(env["conn"])),
        ticket_index=index,
    )


# --- similarity index -----------------------------------------------------------------


def test_index_build_and_search(tmp_path):
    idx = TicketSimilarityIndex(index_dir=tmp_path, embedding=FakeEmbedding())
    n = idx.build([
        {"ticket_id": "T1", "text": "空调漏水", "status": "OPEN", "category": "device"},
        {"ticket_id": "T2", "text": "打印机卡纸", "status": "CLOSED", "category": "device"},
    ])
    assert n == 2 and idx.available
    hits = idx.search("空调又漏水了", top_k=1)
    assert hits[0][0] == "T1"


def test_index_unavailable_without_embedding(tmp_path):
    idx = TicketSimilarityIndex(index_dir=tmp_path, embedding=None)
    assert not idx.available
    assert idx.search("anything") == []


# --- case timeline ----------------------------------------------------------------------


def test_case_timeline_chronological(env):
    out = format_case_timeline(env["store"], "T8001")
    assert "T8001" in out and "created" in out


def test_case_timeline_missing_ticket(env):
    assert "不存在" in format_case_timeline(env["store"], "T-NOPE")


# --- tool port integration ---------------------------------------------------------------


def test_tool_similar_tickets_with_fake_index(env):
    class ReadyIndex:
        def available(self):  # noqa: ANN202
            return True

        def search(self, q, top_k=3):  # noqa: ANN001, ANN202
            return [("T8001", 0.92)]

    tc = _tools(env, ReadyIndex()).call(
        "similar_tickets", {"query": "空调漏水"}, user_id=env["user_id"], session_id="s",
    )
    assert tc.ok and "T8001" in tc.observation


def test_tool_similar_tickets_unavailable_degrades(env):
    tc = _tools(env).call(
        "similar_tickets", {"query": "x"}, user_id=env["user_id"], session_id="s",
    )
    assert tc.ok and "不可用" in tc.observation


def test_tool_case_timeline_via_port(env):
    tc = _tools(env).call(
        "case_timeline", {"ticket_id": "T8002"},
        user_id=env["user_id"], session_id="s",
        allowed=frozenset({"case_timeline"}),
    )
    assert tc.ok and "T8002" in tc.observation


def test_whitelist_denies_similar_tickets_for_faq(env):
    with pytest.raises(AgentToolDenied):
        _tools(env).call(
            "similar_tickets", {"query": "x"}, user_id=env["user_id"], session_id="s",
            allowed=frozenset({"search_knowledge"}),
        )


def test_schemas_cover_new_tools():
    names = {s["name"] for s in TOOL_JSON_SCHEMAS}
    assert {"similar_tickets", "case_timeline"} <= names
    assert names == set(ALLOWED_TOOLS)
