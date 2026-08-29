"""CommandParser: channel text -> Domain Action (shared operator/approval).

Slash commands are only Action Input Adapters. Explicit ticket ids are
required in shared conversations — no implicit "last ticket" guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_TICKET_RE = re.compile(r"T\d{4,}")

CLAIM_KEYWORDS = ("认领", "接手", "处理中")
RESOLVE_KEYWORDS = ("解决", "完成", "处理好了", "已修复", "已处理", "搞定")
# Confirmation needs STRONG intent: "好了"/"可以了"/"修好了" were removed —
# they substring-match progress questions ("处理好了吗"/"修好了吗") and could
# auto-close a RESOLVED ticket that the user was only asking about.
CONFIRM_KEYWORDS = ("确认", "已恢复", "恢复了", "没问题了")
# "不好" removed: matches everyday moods ("心情不好"), not rejection of a
# resolution. Real rejections name the unresolved state.
REJECT_RESOLUTION_KEYWORDS = ("还没好", "没有好", "还是不行", "没解决", "未解决", "还有问题")
ESCALATE_KEYWORDS = ("升级", "上报", "加急")
FORCE_CLOSE_KEYWORDS = ("强制关闭", "force-close", "强制关闭")
APPROVE_KEYWORDS = ("同意", "批准", "approve")
REJECT_KEYWORDS = ("拒绝", "驳回", "reject")


class ActionName(str, Enum):
    CLAIM = "claim"
    RESOLVE = "resolve"
    REQUESTER_CONFIRM = "requester_confirm"
    REJECT_RESOLUTION = "reject_resolution"
    ESCALATE = "escalate"
    FORCE_CLOSE = "force_close"
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class ParsedCommand:
    action: ActionName
    ticket_id: str | None = None
    approval_id: str | None = None
    note: str | None = None
    reason: str | None = None
    raw: str = ""


class CommandParser:
    """Deterministic, keyword-first parsing. `parse` returns None when the
    text does not look like an action (then the caller treats it as chat)."""

    def parse_operator(self, text: str) -> ParsedCommand | None:
        stripped = text.strip()
        lowered = stripped.lower()
        ticket_id = self._ticket_id(stripped)

        if lowered.startswith("/claim") or any(k in text for k in CLAIM_KEYWORDS):
            if ticket_id is None:
                return None
            return ParsedCommand(ActionName.CLAIM, ticket_id=ticket_id, raw=stripped)

        if lowered.startswith("/resolve") or any(k in text for k in RESOLVE_KEYWORDS):
            if ticket_id is None:
                return None
            note = self._rest_after_ticket(stripped) or None
            return ParsedCommand(ActionName.RESOLVE, ticket_id=ticket_id, note=note, raw=stripped)

        if lowered.startswith("/force-close") or any(k in text for k in FORCE_CLOSE_KEYWORDS):
            if ticket_id is None:
                return None
            reason = self._rest_after_ticket(stripped) or None
            return ParsedCommand(ActionName.FORCE_CLOSE, ticket_id=ticket_id, reason=reason, raw=stripped)

        if lowered.startswith("/escalate") or any(k in text for k in ESCALATE_KEYWORDS):
            if ticket_id is None:
                return None
            reason = self._rest_after_ticket(stripped) or None
            return ParsedCommand(ActionName.ESCALATE, ticket_id=ticket_id, reason=reason, raw=stripped)

        return None

    def parse_approver(self, text: str) -> ParsedCommand | None:
        stripped = text.strip()
        lowered = stripped.lower()
        approval_id = self._approval_id(stripped)
        if approval_id is None:
            return None
        if lowered.startswith("/approve") or lowered.startswith("approve") or any(k in text for k in APPROVE_KEYWORDS):
            return ParsedCommand(ActionName.APPROVE, approval_id=approval_id, raw=stripped)
        if lowered.startswith("/reject") or lowered.startswith("reject") or any(k in text for k in REJECT_KEYWORDS):
            reason = self._rest_after_approval(stripped) or None
            return ParsedCommand(ActionName.REJECT, approval_id=approval_id, reason=reason, raw=stripped)
        return None

    def parse_requester_confirmation(self, text: str) -> tuple[str | None, str | None] | None:
        """(ticket_id | None, "confirm"|"reject"|None) when the text clearly
        confirms or rejects a resolution. ticket_id may be None (falls back
        to the caller's candidate ticket)."""
        ticket_id = self._ticket_id(text)
        if any(k in text for k in CONFIRM_KEYWORDS):
            return ticket_id, "confirm"
        if any(k in text for k in REJECT_RESOLUTION_KEYWORDS):
            return ticket_id, "reject"
        return None

    @staticmethod
    def _ticket_id(text: str) -> str | None:
        match = _TICKET_RE.search(text)
        return match.group(0) if match else None

    @staticmethod
    def _approval_id(text: str) -> str | None:
        match = re.search(r"(apr_[a-f0-9]+)", text.lower())
        return match.group(1) if match else None

    @staticmethod
    def _rest_after_ticket(text: str) -> str | None:
        match = _TICKET_RE.search(text)
        if not match:
            return None
        rest = text[match.end():].strip().lstrip("：:,").strip()
        return rest or None

    @staticmethod
    def _rest_after_approval(text: str) -> str | None:
        match = re.search(r"(apr_[a-f0-9]+)", text.lower())
        if not match:
            return None
        rest = text[match.end():].strip().lstrip("：:,").strip()
        return rest or None
