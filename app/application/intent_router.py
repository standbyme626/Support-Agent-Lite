"""IntentRouter: deterministic intent pre-routing.

Pure keyword rules, no randomness: the same message always routes the
same way. V2.1 removed the LLM fallback (`llm_classify_fn`) — semantic
understanding is owned by the SupportAgent, and running two competing
LLM routing layers would create contradictory intent signals. Obvious
deterministic routing stays here; everything below threshold routes to
"other" and the agent/continuation logic handles it.

Intents:

    faq             knowledge question -> RAG evidence -> agent
    support         issue / repair request -> TicketResolver -> Ticket
    progress_query  status inquiry about existing tickets
    other           cannot be classified by rules
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

VALID_INTENTS = frozenset({"faq", "support", "progress_query", "other", "chitchat"})

# Chitchat: greetings / identity / thanks / farewell / help. Matched as
# whole-phrase containment on SHORT messages only, so real requests that
# happen to contain a polite word are never hijacked.
_CHITCHAT_PHRASES: tuple[str, ...] = (
    "你好", "您好", "在吗", "你是谁", "你叫什么", "你是机器人",
    "是不是机器人", "你是ai吗", "你能做什么", "你会什么",
    "谢谢", "多谢", "感谢", "再见", "拜拜", "辛苦了",
    "测试", "test", "testing", "试试", "在么", "在不在",
)
_CHITCHAT_EXACT: frozenset[str] = frozenset({
    "help", "/help", "帮助", "怎么用", "怎么使用", "使用说明",
})
_CHITCHAT_MAX_LEN = 15


@dataclass(frozen=True)
class IntentDecision:
    intent: str
    confidence: float
    is_low_confidence: bool
    reason: str


class IntentRouter:
    """Deterministic keyword intent router (no LLM fallback, V2.1)."""

    _intent_keywords: ClassVar[dict[str, frozenset[str]]] = {
        "faq": frozenset({
            "怎么", "如何", "怎么办", "是什么", "流程", "步骤", "指南",
            "说明", "咨询", "申请", "查询", "help",
        }),
        "support": frozenset({
            "坏了", "故障", "报修", "维修", "异常", "不能用", "无法使用",
            "开不了", "断网", "蓝屏", "死机", "连不上", "打不开", "黑屏",
            "花屏", "报错", "卡住", "中断", "失灵", "无法访问", "脱机",
            "不亮", "不出纸", "不能打印",
        }),
        "progress_query": frozenset({
            "进度", "怎么样了", "处理了吗", "好了吗", "进展", "谁在跟进",
            "什么时候能", "有结果吗", "结果如何", "跟进",
        }),
        "other": frozenset(),
    }

    # Tie-break weights (higher wins among equal scores): support > faq,
    # so a repair request phrased as a question still routes to support.
    _intent_weights: ClassVar[dict[str, float]] = {
        "progress_query": 0.10,
        "support": 0.08,
        "faq": 0.05,
        "other": 0.0,
    }

    _threshold = 0.58

    def __init__(self, threshold: float = 0.58) -> None:
        self._threshold = threshold

    @staticmethod
    def _has_business_signal(text: str) -> bool:
        """Any strong business keyword overrides chitchat even in short text."""
        for keywords in (
            IntentRouter._intent_keywords["support"],
            IntentRouter._intent_keywords["progress_query"],
            IntentRouter._intent_keywords["faq"],
        ):
            if any(k in text for k in keywords):
                return True
        return False

    def route(self, message: str) -> IntentDecision:
        normalized = message.strip().lower()
        if not normalized:
            return IntentDecision("other", 0.0, True, "empty-message")

        # Chitchat first: short social messages must never fall through to
        # the handoff-ticket path (B-fix: 「你好你是谁」不建单).
        if normalized in _CHITCHAT_EXACT or (
            len(normalized) <= _CHITCHAT_MAX_LEN
            and any(p in normalized for p in _CHITCHAT_PHRASES)
            and not self._has_business_signal(normalized)
        ):
            return IntentDecision("chitchat", 1.0, False, "chitchat-match")

        scored = [
            (intent, self._score(normalized, keywords))
            for intent, keywords in self._intent_keywords.items()
            if intent != "other"
        ]
        scored.sort(
            key=lambda item: (item[1], self._intent_weights.get(item[0], 0.0)),
            reverse=True,
        )
        best_intent, best_score = scored[0] if scored else ("other", 0.0)

        confidence = round(min(1.0, best_score), 3)
        if confidence < self._threshold:
            return IntentDecision("other", confidence, True, f"below-threshold:{self._threshold}")

        return IntentDecision(best_intent, confidence, False, "keyword-match")

    @staticmethod
    def _score(message: str, keywords: frozenset[str]) -> float:
        if not keywords:
            return 0.0
        matched = [word for word in keywords if word in message]
        # Drop keywords subsumed by a longer matched keyword (怎么 inside 怎么办):
        # one signal, not two.
        matched = [w for w in matched if not any(w in other and other != w for other in matched)]
        if not matched:
            return 0.0
        return min(1.0, 0.65 + 0.2 * (len(matched) - 1))
