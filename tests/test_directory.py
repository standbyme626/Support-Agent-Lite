"""C9: enterprise retrieval router — directory/asset lookup, entity guard,
constrained ticket stats, per-run tool whitelists."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.application.agent_tools import AgentToolDenied, AgentToolPort
from app.application.entity_guard import detect_entities
from app.infrastructure.directory import DirectoryService

SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "directory"


@pytest.fixture(scope="module")
def directory() -> DirectoryService:
    return DirectoryService(SEED_DIR)


@pytest.fixture()
def ctx():
    from app.application.identity_service import IdentityResolver
    from app.application.session_service import SessionService
    from app.infrastructure.db import apply_migrations, connect
    from app.infrastructure.repositories import (
        ChannelIdentityRepository,
        MessageRepository,
        SessionRepository,
        TicketStore,
        UserRepository,
    )

    conn = connect(":memory:")
    apply_migrations(conn)
    identity = IdentityResolver(UserRepository(conn), ChannelIdentityRepository(conn))
    session_service = SessionService(SessionRepository(conn))
    user = identity.resolve("wecom", "zhangsan", "张三")
    session = session_service.find_or_create(user.id, "wecom", "conv_dir")
    yield {
        "conn": conn,
        "messages": MessageRepository(conn),
        "store": TicketStore(conn),
        "user": user,
        "session": session,
    }
    conn.close()


# --- seed data validity ------------------------------------------------------


def test_seed_files_exist_and_are_fictitious():
    for name in ("employees.json", "assets.json"):
        data = json.loads((SEED_DIR / name).read_text(encoding="utf-8"))
        assert data.get("fictitious") is True


def test_seed_counts_and_unique_ids():
    d = DirectoryService(SEED_DIR)
    assert len(d.employees) == 30
    assert len(d.assets) == 30
    emp_ids = [e["employee_id"] for e in d.employees]
    ast_ids = [a["asset_id"] for a in d.assets]
    assert len(set(emp_ids)) == 30
    assert len(set(ast_ids)) == 30
    exts = [e["extension"] for e in d.employees]
    assert len(set(exts)) == 30


def test_every_asset_assignee_exists():
    d = DirectoryService(SEED_DIR)
    emp_ids = {e["employee_id"] for e in d.employees}
    for asset in d.assets:
        if asset.get("assigned_to"):
            assert asset["assigned_to"] in emp_ids


# --- contact lookup ----------------------------------------------------------


def test_contact_by_exact_name_requester_masked(directory):
    name = directory.employees[0]["name"]
    out = directory.lookup_contact(name, viewer_role="requester")
    assert name in out
    # requester must never see an unmasked 11-digit phone
    import re

    assert not re.search(r"1[3-9]\d{9}", out)


def test_contact_by_employee_id_operator_sees_full(directory):
    out = directory.lookup_contact("E0016", viewer_role="operator")
    assert "E0016" in out


def test_contact_by_department_lists_multiple(directory):
    out = directory.lookup_contact("运维部", viewer_role="requester")
    assert out.count("E00") >= 2  # dept has several people


def test_contact_partial_name(directory):
    out = directory.lookup_contact("李娜", viewer_role="requester")
    assert "李娜" in out


def test_contact_unknown_returns_miss(directory):
    out = directory.lookup_contact("不存在的名字xyz", viewer_role="requester")
    assert "未找到" in out


# --- asset lookup -------------------------------------------------------------


def test_asset_by_id(directory):
    first = directory.assets[0]["asset_id"]
    out = directory.lookup_asset(first, viewer_role="requester")
    assert first in out


def test_asset_by_assignee_name(directory):
    emp = directory.employees[0]
    out = directory.lookup_asset(emp["name"], viewer_role="operator")
    assert emp["name"] in out


def test_asset_by_type(directory):
    out = directory.lookup_asset("打印机", viewer_role="requester")
    assert "打印机" in out


def test_asset_unknown_returns_miss(directory):
    assert "未找到" in directory.lookup_asset("AST-9999", viewer_role="requester")


# --- entity guard --------------------------------------------------------------


def test_guard_detects_phone():
    kinds = detect_entities("我手机是13812345678帮我登记")
    assert "phone" in kinds


def test_guard_detects_id_card():
    kinds = detect_entities("身份证号110101199001011234丢了")
    assert "id_card" in kinds


def test_guard_detects_employee_and_asset_ids():
    assert "employee_id" in detect_entities("工号E0016的电脑坏了")
    assert "asset_id" in detect_entities("AST-0307 打不开")


def test_guard_ignores_plain_text():
    assert detect_entities("空调不制冷了怎么办") == []


# --- tools port integration -----------------------------------------------------


def _tools(ctx) -> AgentToolPort:
    from app.application.memory_service import MemoryService
    from app.application.retriever import Retriever
    from app.infrastructure.repositories import MemoryRepository

    seed_faq = Path(__file__).resolve().parent.parent / "seed" / "faq"
    retriever = Retriever(seed_faq)
    memory = MemoryService(ctx["store"], MemoryRepository(ctx["conn"]))
    return AgentToolPort(
        ctx["store"], ctx["messages"], retriever, memory,
        directory=DirectoryService(SEED_DIR),
    )


def test_port_contact_lookup(ctx, directory):
    name = directory.employees[0]["name"]
    tc = _tools(ctx).call(
        "contact_lookup", {"query": name}, user_id=ctx["user"].id, session_id=ctx["session"].id
    )
    assert tc.ok and name in tc.observation


def test_port_asset_lookup(ctx):
    tc = _tools(ctx).call(
        "asset_lookup", {"query": "AST-0307"}, user_id=ctx["user"].id, session_id=ctx["session"].id
    )
    assert tc.ok and ("AST-0307" in tc.observation or "未找到" in tc.observation)


def test_port_ticket_stats_count_by_status(ctx):
    from app.domain.ticket import Ticket

    store = ctx["store"]
    store.create(Ticket(id="T9001", user_id=ctx["user"].id, title="t1", description="d1", queue="general", category="device"))
    store.create(Ticket(id="T9002", user_id=ctx["user"].id, title="t2", description="d2", queue="network", category="network"))
    tc = _tools(ctx).call(
        "ticket_stats",
        {"group_by": "status"},
        user_id=ctx["user"].id,
        session_id=ctx["session"].id,
    )
    assert tc.ok and ("open" in tc.observation.lower() or "待处理" in tc.observation)


def test_per_run_whitelist_denies_entity_tool_when_not_listed(ctx, directory):
    tools = _tools(ctx)
    name = directory.employees[0]["name"]
    with pytest.raises(AgentToolDenied):
        tools.call(
            "contact_lookup",
            {"query": name},
            user_id=ctx["user"].id,
            session_id=ctx["session"].id,
            allowed=frozenset({"search_knowledge"}),
        )


def test_per_run_whitelist_allows_listed_tool(ctx, directory):
    tools = _tools(ctx)
    name = directory.employees[0]["name"]
    tc = tools.call(
        "contact_lookup",
        {"query": name},
        user_id=ctx["user"].id,
        session_id=ctx["session"].id,
        allowed=frozenset({"contact_lookup"}),
    )
    assert tc.ok
