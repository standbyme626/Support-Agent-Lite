"""Bounded read-only Agent tools (V2.1).

The Agent may perform at most `MAX_TOOL_CALLS` read-only lookups per run.
Every tool is READ ONLY: there is no write/execute surface here, and the
whitelist is enforced both by the port and by the agent loop. Anything
that mutates state is deliberately absent (claim/resolve/close/approve/
assign/update are NOT tools — see invariant #4).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.application.memory_service import MemoryService
from app.application.retriever import Retriever
from app.application.stats_agent import StatsAgent
from app.domain.role import UserRole
from app.domain.ticket import TicketStatus
from app.infrastructure.directory import DirectoryService
from app.infrastructure.repositories import MessageRepository, TicketStore

MAX_TOOL_CALLS = 2
MAX_AGENT_STEPS = 3

ALLOWED_TOOLS = frozenset({
    "get_ticket_history",
    "search_knowledge",
    "recall_memory",
    "get_allowed_actions",
    "contact_lookup",
    "asset_lookup",
    "ticket_stats",
    "ask_stats",
})


class AgentToolDenied(RuntimeError):
    """Raised when the model asks for a tool outside the whitelist."""


@dataclass
class ToolCall:
    tool: str
    args: dict
    observation: str
    ok: bool = True


class AgentToolPort:
    """Read-only tool surface the agent loop may call (bounded)."""

    def __init__(
        self,
        tickets: TicketStore,
        messages: MessageRepository,
        retriever: Retriever,
        memory: MemoryService,
        directory: DirectoryService | None = None,
        stats_agent: "StatsAgent | None" = None,
    ) -> None:
        self._tickets = tickets
        self._messages = messages
        self._retriever = retriever
        self._memory = memory
        self._directory = directory
        self._stats_agent = stats_agent

    def call(
        self,
        tool: str,
        args: dict,
        *,
        user_id: str,
        session_id: str,
        allowed: frozenset[str] | None = None,
    ) -> ToolCall:
        whitelist = allowed if allowed is not None else ALLOWED_TOOLS
        if tool not in ALLOWED_TOOLS or tool not in whitelist:
            raise AgentToolDenied(f"tool not allowed: {tool}")
        if tool == "get_ticket_history":
            observation = self._ticket_history(str(args.get("ticket_id") or ""), session_id)
        elif tool == "search_knowledge":
            observation = self._search_knowledge(str(args.get("query") or ""))
        elif tool == "recall_memory":
            observation = self._recall_memory(str(args.get("query") or ""), user_id)
        elif tool == "get_allowed_actions":
            observation = self._allowed_actions(str(args.get("ticket_id") or ""), str(args.get("actor_role") or ""))
        elif tool == "contact_lookup":
            observation = self._contact_lookup(str(args.get("query") or ""))
        elif tool == "asset_lookup":
            observation = self._asset_lookup(str(args.get("query") or ""))
        elif tool == "ticket_stats":
            observation = self._ticket_stats(str(args.get("group_by") or "status"))
        elif tool == "ask_stats":
            question = str(args.get("question") or "")
            if self._stats_agent is None:
                observation = "统计子代理不可用"
            else:
                answer = self._stats_agent.run(question)
                observation = answer.text
        else:  # pragma: no cover - guarded by ALLOWED_TOOLS
            raise AgentToolDenied(f"tool not allowed: {tool}")
        return ToolCall(tool=tool, args=dict(args), observation=observation)

    # --- implementations (all reads) ---

    def _ticket_history(self, ticket_id: str, session_id: str) -> str:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return "工单不存在"
        events = [
            f"{e.event_type.value} by {e.actor_user_id or '-'}" for e in self._tickets.events(ticket_id)
        ]
        lines = [
            f"工单 {ticket.id} 状态={ticket.status.value} 优先级={ticket.priority or '-'} "
            f"处理人={ticket.assignee_user_id or '-'}",
            f"标题：{ticket.title}",
            f"事件：{'；'.join(events) or '无'}",
        ]
        recent = self._messages.recent(session_id, limit=8)
        if recent:
            lines.append("最近会话：" + " | ".join(f"{m.role}:{m.text}" for m in recent))
        return "\n".join(lines)

    def _search_knowledge(self, query: str) -> str:
        if not query:
            return "查询为空"
        hits = self._retriever.search(query, top_k=3)
        if not hits:
            return "知识库无相关条目"
        return "\n".join(
            f"{h.document.doc_id}（score={round(h.score, 3)}）：{h.document.title} — {h.document.content[:120]}"
            for h in hits
        )

    def _recall_memory(self, query: str, user_id: str) -> str:
        if not query:
            return "查询为空"
        hits = self._memory.recall(user_id, query, top_k=5)
        if not hits:
            return "无相关历史记忆"
        return "\n".join(
            f"{hit.memory.id}（score={round(hit.score, 3)}）：{hit.memory.fact}" for hit in hits
        )

    def _contact_lookup(self, query: str) -> str:
        if self._directory is None:
            return "通讯录服务不可用"
        return self._directory.lookup_contact(query, viewer_role="requester")

    def _asset_lookup(self, query: str) -> str:
        if self._directory is None:
            return "资产台账服务不可用"
        return self._directory.lookup_asset(query)

    def _ticket_stats(self, group_by: str) -> str:
        field_map = {"status": "status", "queue": "queue", "category": "category", "priority": "priority"}
        col = field_map.get(group_by)
        if col is None:
            return f"不支持的统计维度：{group_by}（可选 status/queue/category/priority）"
        rows = self._tickets.stats_grouped(col)
        total = sum(rows.values())
        detail = "；".join(f"{k or '未设置'}={v}" for k, v in sorted(rows.items()))
        return f"工单统计（按{group_by}，共 {total} 单）：{detail}"

    def _allowed_actions(self, ticket_id: str, actor_role: str) -> str:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return "工单不存在"
        actions: list[str] = []
        if actor_role in ("operator", UserRole.OPERATOR.value):
            if ticket.status == TicketStatus.OPEN:
                actions.append("claim")
            elif ticket.status == TicketStatus.IN_PROGRESS:
                actions.append("resolve")
                actions.append("escalate")
                actions.append("force_close(需原因+审批)")
            elif ticket.status == TicketStatus.RESOLVED:
                actions.append("escalate")
        elif actor_role in ("requester",):
            if ticket.status == TicketStatus.RESOLVED:
                actions.append("confirm")
                actions.append("reject_resolution")
        return f"当前允许动作：{', '.join(actions) or '无'}"
