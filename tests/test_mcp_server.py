"""C7: read-only MCP server exposing support capabilities.

Governance conditions (AGENTS.md 2026-08-24): READ-ONLY capabilities
only; Invariant 4 applies to anything they could trigger — so there is
deliberately NO claim/resolve/close surface here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.mcp_server import READ_ONLY_DECLARATION, build_mcp_server


@pytest.fixture()
def seeded():
    from app.domain.identity import User
    from app.domain.ticket import Ticket
    from app.infrastructure.db import apply_migrations, connect
    from app.infrastructure.repositories import TicketStore, UserRepository
    from app.main import build_ops

    conn = connect(":memory:")
    apply_migrations(conn)
    store = TicketStore(conn)
    user = UserRepository(conn).create(User(id="u_m", display_name="MCP用户"))
    store.create(Ticket(id="T7001", user_id=user.id, title="空调漏水",
                        description="d", category="device", queue="facility"))
    ops = build_ops(conn, store)
    yield {"ops": ops, "store": store}
    conn.close()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_server_lists_only_readonly_tools(seeded):
    from mcp.client import Client

    server = build_mcp_server(ops=seeded["ops"], seed_dir=Path("seed/faq"))

    async def go():
        async with Client(server, mode="auto") as client:
            return await client.list_tools()

    tools = _run(go())
    names = {t.name for t in tools.tools}
    assert {"get_case", "ticket_stats", "search_knowledge"} <= names
    forbidden = {"claim", "resolve", "close", "force_close", "escalate", "approve"}
    assert not (names & forbidden)


def test_tool_descriptions_declare_readonly(seeded):
    from mcp.client import Client

    server = build_mcp_server(ops=seeded["ops"], seed_dir=Path("seed/faq"))

    async def go():
        async with Client(server, mode="auto") as client:
            return {t.name: t.description or "" for t in (await client.list_tools()).tools}

    descriptions = _run(go())
    assert all(READ_ONLY_DECLARATION in d for d in descriptions.values())


def test_get_case_returns_ticket_view(seeded):
    from mcp.client import Client

    server = build_mcp_server(ops=seeded["ops"], seed_dir=Path("seed/faq"))

    async def go():
        async with Client(server, mode="auto") as client:
            res = await client.call_tool("get_case", {"ticket_id": "T7001"})
            return json.loads(res.content[0].text)

    case = _run(go())
    assert case["ticket"]["id"] == "T7001"
    assert case["ticket"]["status"] == "OPEN"
    assert isinstance(case["events"], list)


def test_get_case_unknown_ticket_reports_miss(seeded):
    from mcp.client import Client

    server = build_mcp_server(ops=seeded["ops"], seed_dir=Path("seed/faq"))

    async def go():
        async with Client(server, mode="auto") as client:
            res = await client.call_tool("get_case", {"ticket_id": "T-NOPE"})
            return json.loads(res.content[0].text)

    out = _run(go())
    assert "not found" in out.get("error", "") or "未找到" in out.get("error", "")


def test_ticket_stats_counts(seeded):
    from mcp.client import Client

    server = build_mcp_server(ops=seeded["ops"], seed_dir=Path("seed/faq"))

    async def go():
        async with Client(server, mode="auto") as client:
            res = await client.call_tool("ticket_stats", {"group_by": "status"})
            return json.loads(res.content[0].text)

    stats = _run(go())
    assert stats["rows"].get("OPEN") == 1


def test_search_knowledge_hits_corpus(seeded):
    from mcp.client import Client

    server = build_mcp_server(ops=seeded["ops"], seed_dir=Path("seed/faq"))

    async def go():
        async with Client(server, mode="auto") as client:
            res = await client.call_tool("search_knowledge", {"query": "电脑一插U盘就蓝屏"})
            return json.loads(res.content[0].text)

    out = _run(go())
    assert any(h["doc_id"] == "kb-hw-0004" for h in out["hits"])


def test_kb_catalog_resource(seeded):
    from mcp.client import Client

    server = build_mcp_server(ops=seeded["ops"], seed_dir=Path("seed/faq"))

    async def go():
        async with Client(server, mode="auto") as client:
            res = await client.read_resource("kb://catalog")
            contents = res.contents if hasattr(res, "contents") else res
            item = contents[0]
            text = item.text if hasattr(item, "text") else str(item)
            return json.loads(text)

    catalog = _run(go())
    assert catalog["total"] > 100
    assert {"doc_id", "title", "source_type"} <= set(catalog["documents"][0].keys())


def test_instructions_carry_invariant4(seeded):
    server = build_mcp_server(ops=seeded["ops"], seed_dir=Path("seed/faq"))
    assert "Invariant 4" in (server.instructions or "")
