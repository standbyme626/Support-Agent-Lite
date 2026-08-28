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
_TICKET_REF_RE = __import__("re").compile(r"T\d{4,}")

# 业务保底护栏词(E2E 实测修复 1A):语义层低分/异常降级时,命中任一词即
# 保守走 support 建单——错建单有 HITL 兜底,漏单没有。
SUPPORT_GUARD_TERMS: frozenset[str] = frozenset({
    "卡纸", "黑屏", "蓝屏", "死机", "断网", "报错", "故障", "坏了", "失灵",
    "不能用", "用不了", "开不了", "打不开", "进不去", "连不上", "闪退",
    "没反应", "不亮", "不出纸", "不能打印", "登不上", "上不去", "冻结",
    "锁定", "卡住", "中断", "脱机", "白屏", "花屏", "没声音", "听不到",
    "门禁", "投影仪", "打印机", "显示器", "键盘", "鼠标", "工位",
    "会议室", "打卡", "摄像头", "麦克风", "耳机", "工号",
})


def has_support_guard_signal(text: str) -> bool:
    return any(t in text for t in SUPPORT_GUARD_TERMS)


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
            "更改", "重置", "更换", "办理", "开通", "注销", "找回",
        }),
        "support": frozenset({
            "坏了", "故障", "报修", "维修", "异常", "不能用", "无法使用",
            "开不了", "断网", "蓝屏", "死机", "连不上", "打不开", "黑屏",
            "花屏", "报错", "卡住", "中断", "失灵", "无法访问", "脱机",
            "不亮", "不出纸", "不能打印", "卡纸",
            "冻结", "锁定", "无法", "失效", "被阻止", "被锁", "登录不上",
            "扣款失败", "支付失败", "重启", "用不了", "进不去", "没反应",
            "闪退", "登不上", "上不去",
        }),
        "progress_query": frozenset({
            "进度", "怎么样了", "处理了吗", "好了吗", "进展", "谁在跟进",
            "什么时候能", "有结果吗", "结果如何", "跟进", "哪一步", "到哪了",
            "啥情况", "什么情况",
            "状态", "订单", "包裹", "送达", "物流", "发货", "到货",
            "什么时候", "到哪",
            "还没修好", "修好了吗", "修好没", "弄好了吗", "处理好了吗",
            "催一下", "麻烦催", "催催", "搞定没", "啥时候能好", "进展如何",
            "处理得怎么样", "弄好没",
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

        # Explicit ticket reference always routes to progress first —
        # asking about T0001 must never degrade into chitchat.
        if _TICKET_REF_RE.search(normalized.upper()):
            return IntentDecision("progress_query", 0.9, False, "ticket-id-reference")

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
