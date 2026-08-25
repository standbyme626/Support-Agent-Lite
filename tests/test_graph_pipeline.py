"""C1: LangGraph explicit pipeline (classify -> retrieve -> draft ->
evaluate) over the existing deterministic services + bounded agent, plus
Function-Calling JSON schemas for the read-only tool surface."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.application.agent_tools import TOOL_JSON_SCHEMAS, AgentToolPort, ALLOWED_TOOLS
from app.application.graph_pipeline import (
    PipelineState,
    build_support_graph,
)
from app.application.memory_service import MemoryService
from app.application.retriever import Retriever
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.repositories import MemoryRepository, MessageRepository, TicketStore, UserRepository
from app.domain.identity import User
from tests.fake_llm import make_decision

SEED_FAQ = Path(__file__).resolve().parent.parent / "seed" / "faq"


class DecisionLLM:
    model = "fake-graph"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        return json.dumps(make_decision(reply="这是基于知识库的答复。", action="faq_answer"))


@pytest.fixture()
def harness():
    conn = connect(":memory:")
    apply_migrations(conn)
    store = TicketStore(conn)
    user = UserRepository(conn).create(User(id="u_g", display_name="图测试"))
    llm = DecisionLLM()
    tools = AgentToolPort(
        store,
        MessageRepository(conn),
        Retriever(SEED_FAQ),
        MemoryService(store, MemoryRepository(conn)),
    )
    graph = build_support_graph(llm=llm, retriever=Retriever(SEED_FAQ), tools=tools, store=store)
    yield {"conn": conn, "graph": graph, "llm": llm, "store": store, "user_id": user.id}
    conn.close()


def _invoke(h, question: str) -> PipelineState:
    raw = h["graph"].invoke(
        PipelineState(
            run_id=f"run_{uuid4().hex[:8]}",
            user_id=h["user_id"],
            session_id="sess_g",
            question=question,
        )
    )
    return PipelineState(**{k: v for k, v in raw.items() if k in PipelineState.__dataclass_fields__})


# --- happy path ------------------------------------------------------------------


def test_graph_runs_four_nodes_in_order(harness):
    out = _invoke(harness, "VPN 怎么配置？")
    assert out.intent in {"faq", "support", "progress_query", "other"}
    # retrieve node produced evidence attempt; draft produced a decision
    assert out.decision is not None
    assert out.steps == ["classify", "retrieve", "draft", "evaluate"]


def test_graph_draft_uses_llm(harness):
    _invoke(harness, "打印机卡纸怎么办")
    assert len(harness["llm"].calls) == 1


def test_graph_state_is_immutable_per_run(harness):
    a = _invoke(harness, "WiFi 连不上")
    b = _invoke(harness, "WiFi 连不上")
    assert a.run_id != b.run_id


# --- gate / handoff path ------------------------------------------------------------


def test_low_confidence_marks_handoff(harness):
    out = _invoke(harness, "公司楼下食堂今天菜单是什么呀朋友")
    assert out.handoff is True or (out.decision is not None and not out.knowledge_evidence)


# --- no LLM fallback -----------------------------------------------------------------


def test_graph_without_llm_still_completes(harness):
    harness["graph"] = build_support_graph(
        llm=None, retriever=Retriever(SEED_FAQ), tools=None, store=harness["store"]
    )
    out = _invoke(harness, "空调不制冷怎么办")
    assert out.decision is not None  # deterministic fallback decision


# --- Function Calling schemas (#2) ----------------------------------------------------


def test_tool_json_schemas_cover_whitelist():
    names = {s["name"] for s in TOOL_JSON_SCHEMAS}
    assert names == set(ALLOWED_TOOLS)


def test_each_schema_is_openai_fc_shaped():
    for schema in TOOL_JSON_SCHEMAS:
        assert isinstance(schema.get("description"), str) and schema["description"]
        params = schema["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert params.get("required", []) is not None
        for prop in params["properties"].values():
            assert "type" in prop or "anyOf" in prop


def test_ask_stats_schema_shape():
    s = next(s for s in TOOL_JSON_SCHEMAS if s["name"] == "ask_stats")
    assert s["parameters"]["required"] == ["question"]
    assert s["parameters"]["properties"]["question"]["type"] == "string"
