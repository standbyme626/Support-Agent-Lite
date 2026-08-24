"""Deterministic fake LLMs for agent tests (never touch the real network).

- RecordingLLM: records every (system, user) call, returns scripted output.
- ScriptedLLM: returns a queue of scripted responses, one per call.
- BrokenLLM: raises (unavailable).
- TimeoutLLM: raises a timeout-shaped error.
- MalformedLLM: returns garbage (empty / plain text / bad JSON).
- SlowLLM: sleeps before returning (transaction-timing tests).
"""
from __future__ import annotations

import time
from typing import Callable


class RecordingLLM:
    """Captures prompts; returns a scripted reply for every call."""

    def __init__(self, reply: str = '{"summary": "s", "reply_draft": "r"}') -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []  # (system, user)

    @property
    def model(self) -> str:
        return "recording-test-model"

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:  # noqa: ARG002
        self.calls.append((system, user))
        return self.reply

    def last_prompt(self) -> str:
        return self.calls[-1][1] if self.calls else ""


class ScriptedLLM:
    """Returns one scripted response per call (FIFO); repeats the last."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    @property
    def model(self) -> str:
        return "scripted-test-model"

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:  # noqa: ARG002
        idx = min(self.calls, len(self._responses) - 1) if self._responses else 0
        self.calls += 1
        if not self._responses:
            raise RuntimeError("scripted LLM has no responses")
        return self._responses[idx]


class BrokenLLM:
    """LLM unavailable: raises on every call."""

    @property
    def model(self) -> str:
        return "broken-test-model"

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:  # noqa: ARG002
        raise RuntimeError("llm unavailable")


class TimeoutLLM:
    """LLM timeout: raises a timeout-shaped error."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or TimeoutError("llm timeout")

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:  # noqa: ARG002
        raise self._error


class MalformedLLM:
    """Returns garbage that cannot be parsed into a valid decision."""

    def __init__(self, payload: str = "这不是 JSON") -> None:
        self._payload = payload

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:  # noqa: ARG002
        return self._payload


class SlowLLM:
    """Sleeps `delay` seconds then returns a valid decision (timing tests)."""

    def __init__(self, delay: float = 0.5, reply: str | None = None) -> None:
        self._delay = delay
        self._reply = reply or VALID_DECISION
        self.started = 0.0

    @property
    def model(self) -> str:
        return "slow-test-model"

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:  # noqa: ARG002
        self.started = time.monotonic()
        time.sleep(self._delay)
        return self._reply


def make_decision(
    *,
    summary: str = "LLM摘要",
    category: str = "device",
    priority: str = "normal",
    action: str = "dispatch_repair",
    reply: str = "工单 T0001 已记录：空调问题。当前状态：OPEN，我们会持续跟进。",
    confidence: float = 0.9,
    missing: list[str] | None = None,
    needs_human: bool = False,
    needs_approval: bool = False,
    memory_refs: list[str] | None = None,
    knowledge_refs: list[str] | None = None,
    proposal: dict | None = None,
    rationale: str = "语义分析",
    tool_request: dict | None = None,
    understanding: str = "用户上报空调故障",
) -> str:
    """Build a valid decision JSON for ScriptedLLM/RecordingLLM."""
    import json

    payload: dict = {
        "understanding": understanding,
        "summary": summary,
        "category": category,
        "priority_suggestion": priority,
        "recommended_action": action,
        "missing_information": missing or [],
        "confidence": confidence,
        "needs_human": needs_human,
        "needs_approval": needs_approval,
        "reply_draft": reply,
        "memory_refs": memory_refs or [],
        "knowledge_refs": knowledge_refs or [],
        "action_proposal": proposal,
        "rationale": rationale,
        "tool_request": tool_request,
    }
    return json.dumps(payload, ensure_ascii=False)


VALID_DECISION = make_decision()


class ToolLLM(ScriptedLLM):
    """One tool request first, then a final decision (bounded-loop test)."""

    def __init__(self, tool_name: str, tool_args: dict, final: str) -> None:
        tool_json = make_decision(
            tool_request={"tool": tool_name, "args": tool_args}, summary="先用工具"
        )
        super().__init__([tool_json, final])
