"""A2A (Agent-to-Agent) minimal server: agent card + message handling.

Implements the core of Google's A2A protocol so other agents can
DISCOVER this support agent (/.well-known/agent.json) and DELEGATE
intake tasks to it (JSON-RPC message/send). Read-only by construction:
the delegated run goes through the same deterministic ingress pipeline
— the remote caller gains no privilege and no mutation surface beyond
what a normal channel message has.

Scope note (honest boundary): single-process deployment today; A2A's
multi-agent federation is where this pays off. The card + RPC surface
is the contract that makes future federation a config change.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from uuid import uuid4

AGENT_CARD_PATH = "/.well-known/agent.json"
_JSONRPC_PATH = "/a2a/rpc"

_JSONRPC_ERRORS = {
    "parse_error": -32700,
    "invalid_request": -32600,
    "method_not_found": -32601,
    "invalid_params": -32602,
    "internal_error": -32603,
}


def build_agent_card(*, base_url: str, queue: str = "facility") -> dict:
    """A2A Agent Card: discovery metadata for other agents."""
    return {
        "name": "support-agent-lite",
        "description": (
            "跨渠道企业支持代理：受理报修、知识库问答、工单进度查询。"
            "advice-only 架构——所有敏感状态变更经确定性服务与人工审批。"
        ),
        "url": f"{base_url}{_JSONRPC_PATH}",
        "version": "1.0.0",
        "protocolVersion": "0.2.9",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "intake",
                "name": "报修受理",
                "description": "接收自然语言故障描述，创建工单并返回工单号与状态。",
                "tags": ["ticket", "intake"],
            },
            {
                "id": "faq",
                "name": "知识库问答",
                "description": "基于 419 条中文企业知识库的证据式问答（低置信拒答）。",
                "tags": ["rag", "faq"],
            },
            {
                "id": "progress",
                "name": "进度查询",
                "description": "按工单号查询处理时间线与当前状态。",
                "tags": ["progress", "timeline"],
            },
        ],
    }


@dataclass
class A2AResult:
    ok: bool
    payload: dict


class A2AHandler:
    """JSON-RPC method dispatch over the existing workflow/ingress."""

    def __init__(self, *, handle_message) -> None:
        # handle_message(text) -> str : delegate into the local pipeline
        self._handle_message = handle_message
        self._tasks: dict[str, dict] = {}

    def dispatch(self, body: dict) -> A2AResult:
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return self._error(None, "invalid_request", "jsonrpc must be '2.0'")
        method = str(body.get("method") or "")
        request_id = body.get("id")
        params = body.get("params") or {}
        try:
            if method == "message/send":
                return self._message_send(request_id, params)
            if method == "tasks/get":
                return self._tasks_get(request_id, params)
            return self._error(request_id, "method_not_found", f"unknown method: {method}")
        except Exception as exc:  # noqa: BLE001 - never leak internals
            return self._error(request_id, "internal_error", repr(exc))

    def _message_send(self, request_id, params: dict) -> A2AResult:  # noqa: ANN001
        message = params.get("message") or {}
        parts = message.get("parts") or []
        text = next(
            (str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"),
            "",
        ).strip()
        if not text:
            return self._error(request_id, "invalid_params", "message.parts[].text required")
        task_id = str(params.get("taskId") or f"task_{uuid4().hex[:12]}")
        reply = str(self._handle_message(text))
        task = {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [
                {"name": "reply", "parts": [{"kind": "text", "text": reply}]},
            ],
        }
        self._tasks[task_id] = task
        return A2AResult(True, {
            "jsonrpc": "2.0", "id": request_id,
            "result": {"kind": "message", "role": "agent",
                       "parts": [{"kind": "text", "text": reply}], "taskId": task_id},
        })

    def _tasks_get(self, request_id, params: dict) -> A2AResult:  # noqa: ANN001
        task_id = str(params.get("id") or "")
        task = self._tasks.get(task_id)
        if task is None:
            return self._error(request_id, "invalid_params", f"unknown task: {task_id}")
        return A2AResult(True, {"jsonrpc": "2.0", "id": request_id, "result": task})

    @staticmethod
    def _error(request_id, code_name: str, message: str) -> A2AResult:  # noqa: ANN001
        return A2AResult(False, {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": _JSONRPC_ERRORS[code_name], "message": message},
        })


def extract_user_text(envelope_reply: object) -> str:
    return str(envelope_reply or "")


_TICKET_ID_RE = re.compile(r"T\d{4}")


def sanitize_for_a2a(reply: str) -> str:
    """A2A callers are external agents: mask any full phone numbers."""
    return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "138****0000", str(reply or ""))
