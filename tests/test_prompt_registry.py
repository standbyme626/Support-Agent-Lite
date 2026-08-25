"""PromptRegistry hardening tests (V2.1 §7.3/§31).

Covers: versioned meta, deterministic loading, variable validation,
safe literal-brace handling, unbalanced-brace rejection, JSON extraction.
"""
from __future__ import annotations

import pytest

from app.application.prompt_registry import (
    PROMPTS_ROOT,
    PromptNotFound,
    PromptRenderError,
    PromptRegistry,
)


@pytest.fixture()
def registry(tmp_path):
    (tmp_path / "sample.v1.md").write_text(
        """---
prompt_key: sample
prompt_version: v2
scenario: test
expected_schema: application/json
---
你好 {name}，工单 {ticket_id}。JSON 示例：{{"a": 1}}，大括号 {braces}
""",
        encoding="utf-8",
    )
    return PromptRegistry(tmp_path)


def test_meta_parsing(registry) -> None:
    meta = registry.meta("sample")
    assert meta.prompt_key == "sample"
    assert meta.prompt_version == "v2"
    assert meta.scenario == "test"
    assert meta.expected_schema == "application/json"


def test_render_with_variables(registry) -> None:
    rendered = registry.render("sample", {"name": "张三", "ticket_id": "T0001", "braces": "ok"})
    assert "你好 张三，工单 T0001" in rendered
    # literal braces survived escaped (JSON example in the body)
    assert '{"a": 1}' in rendered
    assert "大括号 ok" in rendered
    assert "---" not in rendered.split("\n")[0]  # front matter stripped


def test_missing_variable_raises(registry) -> None:
    with pytest.raises(PromptRenderError, match="ticket_id"):
        registry.render("sample", {"name": "张三"})


def test_unbalanced_braces_raise(tmp_path) -> None:
    (tmp_path / "bad.v1.md").write_text("未转义的大括号 { 在这里", encoding="utf-8")
    with pytest.raises(PromptRenderError, match="unbalanced"):
        PromptRegistry(tmp_path).render("bad", {})


def test_unknown_prompt_raises(registry) -> None:
    with pytest.raises(PromptNotFound):
        registry.render("missing", {})


def test_extract_json_with_fences(registry) -> None:
    raw = '```json\n{"summary": "s", "reply_draft": "r"}\n```'
    assert registry.extract_json(raw) == {"summary": "s", "reply_draft": "r"}


def test_extract_json_plain() -> None:
    assert PromptRegistry.extract_json('前缀 {"a": 1} 后缀') == {"a": 1}


@pytest.mark.parametrize(
    "raw",
    ["", "抱歉，我无法回答", "[]", "[1,2]", '{"a": 1} 文本 {b}'],
)
def test_extract_json_invalid_raises(registry, raw: str) -> None:
    with pytest.raises(ValueError):
        registry.extract_json(raw)


def test_real_agent_prompt_renders_with_all_variables() -> None:
    """The production agent_decision prompt must render with the agent's
    variable set (schema braces are escaped, no missing vars)."""
    registry = PromptRegistry(PROMPTS_ROOT)
    meta = registry.meta("agent_decision")
    assert meta.prompt_key == "agent_decision"
    assert meta.prompt_version == "v1"
    rendered = registry.render(
        "agent_decision",
        {
            "scenario": "新工单受理",
            "scenario_instructions": "指令",
            "user_message": "A3 空调坏了",
            "channel": "wecom",
            "conversation_type": "GROUP",
            "conversation_purpose": "REQUESTER",
            "actor_role": "requester",
            "location": "A3栋",
            "ticket_block": "- 工单号：T0001",
            "recent_messages": "- user: A3 空调坏了",
            "memories_block": "（无）",
            "knowledge_block": "（无）",
            "tool_observations": "",
            "skill_digest": "- reply_handoff: 转人工回复（摘要）",
        },
    )
    assert "<user_message>" in rendered
    assert "A3 空调坏了" in rendered
    assert '"understanding"' in rendered  # schema section present
    assert "工具调用记录" not in rendered  # empty tool observations section omitted
