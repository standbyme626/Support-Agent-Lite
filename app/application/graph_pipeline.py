"""C1: explicit LangGraph pipeline.

The V2.1 runtime already IS a bounded graph at heart — this module makes
that structure explicit and inspectable with LangGraph 1.x: four nodes
(classify -> retrieve -> draft -> evaluate) over the same deterministic
services, the SAME bounded SupportAgent, and the SAME degradation rules.
No behavior change: the graph is a re-expression of the existing
pipeline, not a new execution model. Every node stays deterministic or
falls back deterministically.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from langgraph.graph import END, START, StateGraph

from app.application.agent_decision import AgentDecision, validate_decision
from app.application.agent_tools import TOOL_JSON_SCHEMAS, AgentToolPort
from app.application.context_builder import KnowledgeEvidence
from app.application.intent_router import IntentRouter
from app.application.retriever import Retriever
from app.application.support_agent import INTENT_FAQ, INTENT_NO_ANSWER, SupportAgent
from app.infrastructure.llm import LLMClient
from app.infrastructure.repositories import TicketStore


@dataclass
class PipelineState:
    """Typed graph state (one message run). Fields are accumulated by nodes."""

    run_id: str
    user_id: str
    session_id: str
    question: str
    intent: str = ""
    intent_reason: str = ""
    knowledge_evidence: list = field(default_factory=list)
    decision: AgentDecision | None = None
    handoff: bool = False
    steps: list[str] = field(default_factory=list)


def _node_classify(state: PipelineState) -> dict:
    decision = IntentRouter().route(state.question)
    return {
        "intent": decision.intent,
        "intent_reason": decision.reason,
        "steps": state.steps + ["classify"],
    }


def _make_retrieve(retriever: Retriever):
    def _node_retrieve(state: PipelineState) -> dict:
        evidence: list[KnowledgeEvidence] = []
        answerability = False
        try:
            rag = retriever.answer(state.question)
            if rag is not None:
                answerability = True
                for hit in rag.hits[:3]:
                    evidence.append(
                        KnowledgeEvidence(
                            source_id=hit.document.doc_id,
                            title=hit.document.title,
                            excerpt=hit.document.content[:200],
                            retrieval_score=round(hit.score, 4),
                        )
                    )
        except Exception:  # noqa: BLE001 - retrieval must never break the graph
            answerability = False
        return {
            "knowledge_evidence": evidence,
            "handoff": state.handoff or (state.intent == INTENT_FAQ and not answerability),
            "steps": state.steps + ["retrieve"],
        }

    return _node_retrieve


def _make_draft(llm: LLMClient | None, tools: AgentToolPort | None):
    agent = SupportAgent(llm=llm, tools=tools)

    def _node_draft(state: PipelineState) -> dict:
        from app.application.support_agent import INTENT_SUPPORT

        intent = state.intent
        if intent not in (INTENT_FAQ, INTENT_NO_ANSWER):
            intent = INTENT_SUPPORT
        run = agent.run(
            _agent_context(state),
            intent=intent,
            allowed_tools=None,
        )
        return {"decision": run.decision, "steps": state.steps + ["draft"]}

    return _node_draft


def _agent_context(state: PipelineState):
    """Minimal AgentContext assembled straight from graph state."""
    from uuid import uuid4

    from app.application.context_builder import AgentContext

    return AgentContext(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=f"tr_{uuid4().hex[:12]}",
        channel="graph",
        conversation_type="GROUP",
        conversation_purpose="REQUESTER",
        actor_role="requester",
        latest_user_text=state.question,
        recent_messages=[],
        recalled_memories=[],
        knowledge_evidence=state.knowledge_evidence,
    )


def _node_evaluate(state: PipelineState) -> dict:
    decision = state.decision
    issue = None
    if decision is not None:
        decision, issue = validate_decision(
            {
                "understanding": decision.understanding,
                "summary": decision.summary,
                "category": decision.category,
                "priority_suggestion": decision.priority_suggestion,
                "recommended_action": decision.recommended_action,
                "confidence": decision.confidence,
                "reply_draft": decision.reply_draft,
                "memory_refs": decision.memory_refs,
                "knowledge_refs": [k for k in decision.knowledge_refs if k],
                "rationale": decision.rationale,
            },
            allowed_memory_ids=[],
            allowed_knowledge_ids=[e.source_id for e in state.knowledge_evidence],
            context_ticket_id=None,
            intent=state.intent or INTENT_FAQ,
        )
    handoff = state.handoff or decision is None or bool(issue)
    return {"decision": decision, "handoff": handoff, "steps": state.steps + ["evaluate"]}


def build_support_graph(
    *,
    llm: LLMClient | None,
    retriever: Retriever,
    tools: AgentToolPort | None,
    store: TicketStore | None = None,
):
    """Compile the four-node support pipeline as a LangGraph StateGraph."""
    builder = StateGraph(PipelineState)
    builder.add_node("classify", _node_classify)
    builder.add_node("retrieve", _make_retrieve(retriever))
    builder.add_node("draft", _make_draft(llm, tools))
    builder.add_node("evaluate", _node_evaluate)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "draft")
    builder.add_edge("draft", "evaluate")
    builder.add_edge("evaluate", END)
    return builder.compile()
