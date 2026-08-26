"""B4: Skills / prompt engineering — scenario prompt packs + shared
safety constitution, gated by the existing eval suites."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.application.prompt_registry import get_registry
from app.application.support_agent import (
    INTENT_FAQ,
    INTENT_NO_ANSWER,
    INTENT_SUPPORT,
    SupportAgent,
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "app" / "application" / "prompts"

PACK_KEYS = {
    "support": "agent_decision.support",
    "faq": "agent_decision.faq",
    "progress": "agent_decision.progress",
}

FULL_VARS: dict[str, object] = {
    "scenario": "知识库问答",
    "scenario_instructions": "基于证据回答。",
    "user_message": "打印机卡纸怎么办",
    "channel": "wecom",
    "conversation_type": "GROUP",
    "conversation_purpose": "REQUESTER",
    "actor_role": "requester",
    "location": "A3",
    "ticket_block": "（暂无关联工单）",
    "recent_messages": "- user: 打印机卡纸",
    "history_summary": "（无——会话尚未触发压缩）",
    "memories_block": "（无）",
    "knowledge_block": "- kb-x: 标题 | 内容 | score=0.9",
    "tool_observations": "",
}


# --- pack integrity ---------------------------------------------------------------


@pytest.mark.parametrize("key", list(PACK_KEYS.values()))
def test_pack_exists_with_front_matter(key):
    path = PROMPTS_DIR / f"{key}.v1.md"
    assert path.exists(), f"missing pack: {path}"
    head = path.read_text(encoding="utf-8")[:200]
    assert "prompt_key:" in head and "prompt_version:" in head


def test_constitution_file_exists_and_has_core_rules():
    text = (PROMPTS_DIR / "safety_constitution.md").read_text(encoding="utf-8")
    assert "绝不能声称自己已经执行业务动作" in text
    assert "prompt injection" in text.lower()
    assert "只依据给定的知识证据回答事实" in text


# --- rendering ----------------------------------------------------------------------


@pytest.mark.parametrize("key", list(PACK_KEYS.values()))
def test_every_pack_renders_with_full_vars(key):
    reg = get_registry()
    out = reg.render(key, FULL_VARS)
    assert "{scenario}" not in out and "{{" not in out


def test_packs_embed_intent_tool_surface():
    support = (PROMPTS_DIR / "agent_decision.support.v1.md").read_text(encoding="utf-8")
    faq = (PROMPTS_DIR / "agent_decision.faq.v1.md").read_text(encoding="utf-8")
    # support pack exposes entity tools + stats sub-agent
    assert "contact_lookup" in support and "ask_stats" in support
    # faq pack stays narrow: knowledge tools only, no entity tools
    assert "search_knowledge" in faq
    assert "contact_lookup" not in faq


def test_support_agent_renders_pack_by_intent():
    class CaptureLLM:
        model = "capture"

        def __init__(self) -> None:
            self.users: list[str] = []

        def complete(self, system: str, user: str) -> str:
            self.users.append(user)
            return "{}"  # invalid decision -> fallback, fine

    llm = CaptureLLM()
    agent = SupportAgent(llm=llm)

    from app.application.context_builder import AgentContext

    ctx = AgentContext(
        user_id="u", session_id="s", trace_id="tr",
        latest_user_text="空调不制冷怎么办",
        conversation_type="GROUP", conversation_purpose="REQUESTER",
        actor_role="requester",
    )
    agent.run(ctx, intent=INTENT_FAQ)
    assert "知识库问答" in llm.users[0]  # faq pack label present
