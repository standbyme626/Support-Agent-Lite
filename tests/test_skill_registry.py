"""B4-v2: Skill registry — routing, two-level loading, negative samples,
fallback, injection-reduction metric."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.application.context_builder import AgentContext
from app.application.prompt_registry import PromptRegistry
from app.application.support_agent import SupportAgent

PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "app" / "application" / "prompts"
EXPECTED_SKILLS = {"reply_progress", "reply_handoff", "reply_clarify", "diagnosis", "faq_grounded"}

BASE_VARS: dict[str, object] = {
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
}


class CaptureLLM:
    model = "capture"

    def __init__(self) -> None:
        self.users: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.users.append(user)
        import json

        return json.dumps({
            "understanding": "u", "summary": "s", "category": "device",
            "priority_suggestion": "normal", "recommended_action": "assign_operator",
            "confidence": 0.9, "reply_draft": "好的", "rationale": "r",
        })


@pytest.fixture(scope="module")
def registry() -> PromptRegistry:
    return PromptRegistry(PROMPTS_ROOT)


# --- registry loading & structured meta -------------------------------------------


def test_all_five_skills_load_with_meta(registry):
    metas = {m.key: m for m in registry.skills()}
    assert EXPECTED_SKILLS <= set(metas)
    for m in metas.values():
        assert m.version >= 1
        assert m.applies_to and isinstance(m.applies_to, tuple)
        assert m.failure_mode == "fallback_main"
        assert m.output_format


def test_seven_fields_present(registry):
    m = registry.get("reply_handoff")
    assert m.applies_to
    assert "chitchat" in m.not_applies_to  # 负样本字段
    assert m.requires is not None
    assert "markdown" in m.output_format or "段" in m.output_format


def test_summary_and_full_sections(registry):
    s = registry.get_summary("reply_handoff")
    f = registry.get_full("reply_handoff")
    assert s and f and len(f) > len(s)


def test_digest_lists_every_skill_one_line(registry):
    d = registry.digest()
    for key in EXPECTED_SKILLS:
        assert key in d
    assert len(d.strip().splitlines()) >= len(EXPECTED_SKILLS)


# --- routing -----------------------------------------------------------------------


def test_select_routes_by_applies(registry):
    assert registry.select("progress_query").key == "reply_progress"
    assert registry.select("faq").key == "faq_grounded"
    assert registry.select("no_answer").key == "reply_handoff"


def test_negative_samples_block_wrong_scene(registry):
    # progress 轮绝不能载入 handoff/clarify
    assert registry.select("progress_query").key != "reply_handoff"
    assert registry.select("progress_query").key != "reply_clarify"
    assert registry.select("chitchat") is None  # 闲聊零技能注入


# --- two-level injection ------------------------------------------------------------


def _ctx(text: str) -> AgentContext:
    return AgentContext(
        user_id="u", session_id="s", trace_id="tr", latest_user_text=text,
        conversation_type="GROUP", conversation_purpose="REQUESTER", actor_role="requester",
    )


def test_first_round_injects_summary_only():
    llm = CaptureLLM()
    agent = SupportAgent(llm=llm)
    agent.run(_ctx("T0001 处理得怎么样了"), intent="progress_query")
    p0 = llm.users[0]
    assert "reply_progress" in p0
    assert "（摘要）" in p0
    assert "（完整）" not in p0


def test_execution_round_injects_full_after_tool_call():
    from tests.fake_llm import ToolLLM

    class StubPort:
        def call(self, tool, args, *, user_id, session_id, allowed=None):  # noqa: ANN001
            from app.application.agent_tools import ToolCall

            return ToolCall(tool=tool, args=args, observation="状态 OPEN，已认领")

    final = __import__("json").dumps({
        "understanding": "u", "summary": "s", "category": "device",
        "priority_suggestion": "normal", "recommended_action": "assign_operator",
        "confidence": 0.9, "reply_draft": "好的", "rationale": "r"})
    llm = ToolLLM("get_ticket_history", {"ticket_id": "T0001"}, final)
    prompts_seen: list[str] = []

    class Recording(ToolLLM):
        def complete(self, *, system, user, temperature=0.2):  # noqa: ANN001
            prompts_seen.append(user)
            return super().complete(system=system, user=user)

    agent = SupportAgent(llm=Recording("get_ticket_history", {"ticket_id": "T0001"}, final), tools=StubPort())
    out = agent.run(_ctx("T0001 进度"), intent="progress_query")
    assert out.steps >= 2
    assert "（完整）" in prompts_seen[-1]


def test_chitchat_round_has_no_skill_text():
    llm = CaptureLLM()
    agent = SupportAgent(llm=llm)
    agent.run(_ctx("你好你是谁"), intent="other")
    p0 = llm.users[0]
    for key in ("diagnosis", "reply_handoff"):
        assert f"{key}（" not in p0  # 无任何技能全文/摘要标题


# --- fallback ------------------------------------------------------------------------


def test_missing_skill_dir_falls_back_cleanly(tmp_path):
    llm = CaptureLLM()
    reg = PromptRegistry(tmp_path)  # empty dir: no skills, no templates
    agent = SupportAgent(llm=llm, prompts=reg)
    out = agent.run(_ctx("空调不制冷"), intent="support")
    assert out.decision is not None  # main flow unaffected
    assert out.fallback_used and out.fallback_reason.startswith("prompt_error")
    assert llm.users == []  # never sent a corrupted prompt to the LLM


# --- injection-reduction metric ---------------------------------------------------------


def test_chitchat_round_injection_reduction_ge_60pct(registry):
    """改造前(全量常驻)= 主模板 + 全部场景包正文 + 全部技能 full;
    改造后闲聊轮 = 瘦身主模板(含摘要菜单)。比例必须 ≤40%。
    字符数为代理指标(trace_events 记录 injected_prompt_chars)。"""
    VARS = dict(BASE_VARS, skill_digest=registry.digest())
    base = len(registry.render("agent_decision", VARS))
    legacy = base
    for pack in ("agent_decision.support", "agent_decision.faq", "agent_decision.progress"):
        legacy += len(registry.render(pack, dict(VARS)))
    for key in EXPECTED_SKILLS:
        legacy += len(registry.get_full(key))
    current = base  # chitchat 轮:无场景包、无技能全文,仅摘要菜单
    ratio = current / legacy
    assert ratio <= 0.40, f"injection ratio {ratio:.2f} exceeds 0.40"
