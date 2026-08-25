"""L4 entity interception (C9 router security layer).

Detects precise-entity patterns (phone / national ID / employee id /
asset id) in user text. When any pattern hits, the workflow mounts ONLY
entity-lookup tools for the agent run and disables vector/knowledge
tools — precise identifiers must never flow into semantic retrieval
(both a correctness and a data-safety rule; see 升级计划 §7.2).
"""
from __future__ import annotations

import re

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_EMPLOYEE_ID_RE = re.compile(r"(?<![A-Za-z])E\d{4}(?!\d)")
_ASSET_ID_RE = re.compile(r"(?<![A-Za-z])AST-\d{4}(?!\d)")

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("phone", _PHONE_RE),
    ("id_card", _ID_CARD_RE),
    ("employee_id", _EMPLOYEE_ID_RE),
    ("asset_id", _ASSET_ID_RE),
)


def detect_entities(text: str) -> list[str]:
    """Return the kinds of precise entities found in `text` (deduplicated)."""
    if not text:
        return []
    kinds: list[str] = []
    for kind, pattern in PATTERNS:
        if pattern.search(text):
            kinds.append(kind)
    return kinds


GUARD_INSTRUCTION = (
    "【安全提示】检测到精确编号/个人信息：本轮仅允许使用实体查询工具"
    "（contact_lookup / asset_lookup），输出必须对手机号等敏感字段脱敏。"
)
