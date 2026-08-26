"""SessionCompactor: rolling summary over session history (pi compaction 同款).

pi 的 CompactionEntry 语义(参考 reference/pi/packages/agent/src/harness/
compaction + docs/session-format.md):
    超过阈值 → 把较早消息压缩为一条 summary,记录 first_kept_message_id
    (切点永远落在 user 消息上,不拆轮次);之后的近期消息原文保留。
    下次压缩以上一次的 first_kept_message_id 为增量起点,summary 迭代更新。

本项目适配:
- 触发:每条 assistant 回复落库后检查(workflow._record_reply),失败静默
  降级——压缩器绝不能影响主流程(Harness 不因增强而破);
- 摘要生成:默认确定性抽取式(逐轮用户诉求+末轮处理进展),绝不编造;
  可注入 LLM summarizer(与 MemoryExtractor 的"确定性为契约"立场一致);
- 存储:session_compactions 追加式,上下文只读最新条目;
- 无 LLM 时全链路离线可跑(测试缺省)。
"""
from __future__ import annotations

from typing import Callable

from app.domain.memory import SessionCompaction
from app.infrastructure.repositories import MessageRepository, SessionCompactionRepository

# 未压缩消息超过该数即触发压缩(pi: contextWindow - reserveTokens 的行数版)
COMPACTION_THRESHOLD = 12
# 压缩后原文保留的近期条数(= ContextBuilder._RECENT_LIMIT,窗口对齐)
KEEP_RECENT = 6


class SessionCompactor:
    """Builds one rolling-summary entry when a session's tail grows too long."""

    def __init__(
        self,
        messages: MessageRepository,
        compactions: SessionCompactionRepository,
        *,
        summarizer: Callable[[str], str] | None = None,
        threshold: int = COMPACTION_THRESHOLD,
        keep_recent: int = KEEP_RECENT,
    ) -> None:
        self._messages = messages
        self._compactions = compactions
        self._summarizer = summarizer
        self._threshold = threshold
        self._keep_recent = keep_recent

    def maybe_compact(self, session_id: str) -> SessionCompaction | None:
        """Compact the uncompacted tail if it exceeds the threshold.

        Returns the new entry, or None when not applicable. Raises nothing
        by contract of the caller (workflow swallows exceptions).
        """
        latest = self._compactions.latest_for(session_id)
        candidates = self._messages.list_after(
            session_id, after_id=latest.first_kept_message_id if latest else None
        )
        if len(candidates) <= self._threshold:
            return None

        # 切点:保留最近 keep_recent 条,且必须从 user 消息开始(不拆轮次)。
        cut = len(candidates) - self._keep_recent
        while cut > 0 and candidates[cut].role != "user":
            cut -= 1
        if cut <= 0:
            return None  # 历史不足以安全切割

        compacted = candidates[:cut]
        first_kept = candidates[cut]
        previous_summary = latest.summary if latest else ""
        summary, summarizer_kind = self._summarize(compacted, previous_summary)

        from uuid import uuid4

        entry = SessionCompaction(
            id=uuid4().hex[:12],
            session_id=session_id,
            summary=summary,
            first_kept_message_id=first_kept.id,
            messages_compacted=len(compacted),
            chars_before=sum(len(m.text) for m in compacted),
            summarizer=summarizer_kind,
        )
        return self._compactions.add(entry)

    def _summarize(self, compacted: list, previous_summary: str) -> tuple[str, str]:
        conversation = "\n".join(f"{m.role}: {m.text}" for m in compacted)
        if self._summarizer is not None:
            try:
                text = self._summarizer(conversation).strip()
                if text:
                    return text, "llm"
            except Exception:  # noqa: BLE001 - LLM 摘要失败降级确定性抽取
                pass
        return _deterministic_summary(compacted, previous_summary), "deterministic"


def _deterministic_summary(compacted: list, previous_summary: str) -> str:
    """Extractive fallback: 只重组原文,不引入新事实。

    用户轮保留原句(截断),助手轮只记最后一轮的处理进展——早期结论可能
    已被推翻,末轮才是当前状态。
    """
    lines: list[str] = []
    if previous_summary:
        lines.append(previous_summary)
    turn = 0
    last_assistant = ""
    for message in compacted:
        if message.role == "user":
            turn += 1
            lines.append(f"第{turn}轮诉求：{message.text[:80]}")
        else:
            last_assistant = message.text
    if last_assistant:
        lines.append(f"最近处理进展：{last_assistant[:120]}")
    return "\n".join(lines)
