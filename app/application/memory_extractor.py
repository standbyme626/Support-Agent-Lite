"""MemoryExtractor: CLOSED ticket -> stable facts + final summary.

AC-09. Deterministic rules only: facts are derived exclusively from
ticket fields and events, never invented, never LLM-polished (the
declared-but-unused `llm` parameter was removed in V2.1 — a credible,
deterministic closed-case extraction is the contract; optional LLM
polish is not claimed). Extraction happens AFTER a ticket reaches
CLOSED; open tickets must not produce memory (nothing is stable yet).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.memory import Memory, MemoryKind
from app.domain.ticket import Ticket, TicketEvent, TicketStatus

_CATEGORY_LABELS: dict[str, tuple[str, ...]] = {
    "设备问题": ("空调", "电脑", "打印机", "显示器", "硬件", "设备", "开机", "蓝屏"),
    "网络问题": ("网络", "wifi", "vpn", "断网", "共享盘", "连不上"),
    "账号问题": ("账号", "密码", "邮箱", "登录"),
    "软件问题": ("软件", "系统", "报错", "安装"),
    "财务问题": ("发票", "报销", "退款", "费用"),
    "人事问题": ("年假", "请假", "考勤", "打卡"),
}


@dataclass
class ExtractionResult:
    summary: str
    category: str
    memories: list[Memory]


class MemoryExtractor:
    """Extracts stable facts from a closed ticket and its history."""

    def extract(
        self,
        ticket: Ticket,
        events: list[TicketEvent] | None = None,
        messages: list[str] | None = None,
    ) -> ExtractionResult:
        if ticket.status != TicketStatus.CLOSED:
            raise ValueError(f"memory extraction requires CLOSED ticket, got {ticket.status.value}")
        events = events or []
        messages = messages or []

        category = self._categorize(ticket.title)
        memories: list[Memory] = [
            self._memory(ticket, MemoryKind.STABLE_FACT, self._issue_fact(ticket, category), 0.9 if category else 0.7)
        ]
        resolution_fact = self._resolution_fact(ticket, events, messages)
        if resolution_fact is not None:
            memories.append(self._memory(ticket, MemoryKind.STABLE_FACT, resolution_fact, 0.85))
        summary = self._summary(ticket, category)
        memories.append(self._memory(ticket, MemoryKind.SUMMARY, summary, 0.95))
        return ExtractionResult(summary=summary, category=category, memories=memories)

    # --- deterministic rules ---

    @staticmethod
    def _memory(ticket: Ticket, kind: MemoryKind, fact: str, confidence: float) -> Memory:
        from uuid import uuid4

        return Memory(
            id=uuid4().hex[:12],
            user_id=ticket.user_id,
            ticket_id=ticket.id,
            kind=kind,
            fact=fact,
            confidence=confidence,
        )

    @staticmethod
    def _categorize(text: str) -> str:
        lowered = text.lower()
        for label, terms in _CATEGORY_LABELS.items():
            if any(term in lowered for term in terms):
                return label
        return ""

    def _issue_fact(self, ticket: Ticket, category: str) -> str:
        return f"{category}：{ticket.title}" if category else f"工单问题：{ticket.title}"

    @staticmethod
    def _resolution_fact(
        ticket: Ticket, events: list[TicketEvent], messages: list[str]
    ) -> str | None:
        for event in reversed(events):
            if event.event_type.value in ("resolved", "closed") and event.payload:
                note = event.payload.get("note") or event.payload.get("resolution")
                if note:
                    return f"处理结果：{note}"
        for message in reversed(messages):
            if message:
                return f"处理结果：{message[:80]}"
        return None

    def _summary(self, ticket: Ticket, category: str) -> str:
        return f"工单 {ticket.id}：{ticket.title} 已处理完成。"
