"""IntentRouter: intent classification for inbound messages.

Rules first, optional LLM fallback for low-confidence cases.

Intents (Phase 4 contract):

    faq             knowledge question -> RAG answer, NO ticket
    support         issue / repair request -> TicketResolver -> Ticket
    progress_query  status inquiry about existing tickets
    other           cannot be classified by rules

ADAPT from legacy `reference/core/intent_router.py`. Pure rules, no
randomness: the same message always routes the same way.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, ClassVar

VALID_INTENTS = frozenset({"faq", "support", "progress_query", "other"})


@dataclass(frozen=True)
class IntentDecision:
    intent: str
    confidence: float
    is_low_confidence: bool
    reason: str


class IntentRouter:
    """Hybrid intent router: keyword rules first, LLM fallback below threshold."""

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

    def __init__(
        self,
        threshold: float = 0.58,
        llm_classify_fn: Callable[[str], tuple[str, float]] | None = None,
    ) -> None:
        self._threshold = threshold
        self._llm_classify_fn = llm_classify_fn

    def route(self, message: str) -> IntentDecision:
        normalized = message.strip().lower()
        if not normalized:
            return IntentDecision("other", 0.0, True, "empty-message")

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
            if self._llm_classify_fn is not None:
                llm_intent, llm_conf = self._llm_classify_fn(message)
                if llm_intent in VALID_INTENTS and llm_conf >= self._threshold:
                    return IntentDecision(llm_intent, round(llm_conf, 3), False, "llm-classify")
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
