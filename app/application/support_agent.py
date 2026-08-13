"""SupportAgent: produces an analysis for a resolved ticket context.

Invariant #4: the agent NEVER mutates ticket state. It only outputs
advice — summary, category, priority suggestion, recommended action and
a reply draft. State changes are made by the workflow via TicketService.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar

from app.application.context_builder import AgentContext
from app.infrastructure.llm import LLMClient

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "account": ("账号", "密码", "登录", "邮箱"),
    "network": ("网络", "wifi", "vpn", "断网", "共享盘", "无法访问"),
    "device": ("空调", "设备", "电脑", "打印机", "显示器", "硬件", "开机", "蓝屏"),
    "software": ("软件", "系统", "报错", "安装"),
    "billing": ("发票", "报销", "退款", "费用", "扣费"),
    "hr": ("年假", "请假", "考勤", "打卡"),
}

_URGENT_KEYWORDS = ("紧急", "马上", "立刻", "影响工作", "无法工作", "中断", "停用", "不可用")

_CATEGORY_TO_ACTION = {
    "account": "credential_reset",
    "network": "network_triage",
    "device": "dispatch_repair",
    "software": "software_support",
    "billing": "finance_review",
    "hr": "hr_review",
}


@dataclass(frozen=True)
class AgentAnalysis:
    summary: str
    category: str
    priority_suggestion: str  # "high" | "normal" | "low"
    recommended_action: str
    reply_draft: str


class SupportAgent:
    """Analyzes agent context. Advice only — no state mutation (invariant #4)."""

    _priority_choices: ClassVar[tuple[str, ...]] = ("high", "normal", "low")

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    def analyze(self, context: AgentContext) -> AgentAnalysis:
        category = self._categorize(context.latest_user_text)
        priority = self._prioritize(context.latest_user_text)
        recommended = _CATEGORY_TO_ACTION.get(category, "assign_operator")
        summary, reply = self._generate(context)
        return AgentAnalysis(
            summary=summary,
            category=category,
            priority_suggestion=priority,
            recommended_action=recommended,
            reply_draft=reply,
        )

    # --- deterministic rules ---

    def _categorize(self, text: str) -> str:
        lowered = text.lower()
        for category, keywords in _CATEGORY_KEYWORDS.items():
            if any(k in lowered for k in keywords):
                return category
        return "general"

    def _prioritize(self, text: str) -> str:
        if any(word in text for word in _URGENT_KEYWORDS):
            return "high"
        return "normal"

    # --- summary + reply (LLM polish with deterministic fallback) ---

    def _generate(self, context: AgentContext) -> tuple[str, str]:
        fallback_summary = self._rule_summary(context)
        fallback_reply = self._rule_reply(context)
        if self._llm is None:
            return fallback_summary, fallback_reply
        try:
            system = (
                "你是企业技术支持助手。根据给定上下文生成 JSON 输出，只包含两个字段："
                '"summary"（一句话工单摘要）和 "reply_draft"（给用户的中文回复草稿，语气友好）。'
                "不要修改工单状态，不要编造事实。只输出 JSON。"
            )
            user = f"上下文：\n{context.ticket_summary}\n用户最新消息：{context.latest_user_text}"
            raw = self._llm.complete(system=system, user=user)
            parsed = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            summary = str(parsed.get("summary") or fallback_summary).strip()
            reply = str(parsed.get("reply_draft") or fallback_reply).strip()
            return summary, reply
        except Exception:
            return fallback_summary, fallback_reply

    @staticmethod
    def _rule_summary(context: AgentContext) -> str:
        parts: list[str] = []
        if context.recalled_memories:
            facts = [m.fact for m in context.recalled_memories]
            parts.append(f"参考历史记忆：{'；'.join(facts)}")
        if context.ticket is not None:
            ticket = context.ticket
            parts.append(
                f"用户反馈「{context.latest_user_text}」，关联工单 {ticket.id}（{ticket.status.value}）："
                f"{ticket.title}。"
            )
        else:
            parts.append(f"用户消息：{context.latest_user_text}。")
        return " ".join(parts)

    @staticmethod
    def _rule_reply(context: AgentContext) -> str:
        ticket = context.ticket
        if ticket is None:
            return "已收到您的反馈，我们会尽快为您处理。"
        return f"工单 {ticket.id} 已记录：{ticket.title}。当前状态：{ticket.status.value}，我们会持续跟进。"
