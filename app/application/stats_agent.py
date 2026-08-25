"""C10: stats sub-agent (问数).

A single-responsibility read-only sub-agent: it turns a natural-language
statistical question into a constrained stat spec, executes it against
the tickets table, and formats a grounded answer. It owns no mutable
state, shares nothing with the main conversation agent, and can never
write (invariant #4). The main agent reaches it through the bounded
`ask_stats` tool; every failure degrades to deterministic rule parsing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.infrastructure.repositories import TicketStore

_GROUP_BY = {"status", "queue", "category", "priority"}

_STATUS_WORDS = {
    "待处理": "OPEN", "未处理": "OPEN", "新工单": "OPEN", "open": "OPEN",
    "处理中": "IN_PROGRESS", "进行中": "IN_PROGRESS",
    "已解决": "RESOLVED", "解决": "RESOLVED",
    "已关闭": "CLOSED", "关闭": "CLOSED",
}

_CATEGORY_WORDS = {
    "网络": "network", "账号": "account", "密码": "account", "邮箱": "account",
    "设备": "device", "硬件": "device", "电脑": "device", "打印机": "device",
    "软件": "software", "系统": "software",
    "发票": "billing", "报销": "billing", "费用": "billing",
    "年假": "hr", "请假": "hr", "考勤": "hr",
}


@dataclass
class StatsAnswer:
    text: str
    spec: dict
    rows: dict = field(default_factory=dict)
    fallback_used: bool = False


def parse_time_window(preset: str) -> tuple[datetime | None, datetime | None]:
    now = datetime.now()
    if preset == "last_month":
        start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
        return start, now.replace(day=1)
    if preset == "this_month":
        return now.replace(day=1), None
    if preset == "last_7d":
        return now - timedelta(days=7), None
    if preset == "this_week":
        return now - timedelta(days=now.weekday()), None
    return None, None  # all


def _clean(spec: dict) -> dict:
    out = {
        "metric": str(spec.get("metric") or "count"),
        "group_by": str(spec.get("group_by") or "none"),
        "status": spec.get("status") or None,
        "queue": spec.get("queue") or None,
        "category": spec.get("category") or None,
        "priority": spec.get("priority") or None,
        "time": spec.get("time") or {"preset": "all"},
    }
    if out["group_by"] not in _GROUP_BY:
        out["group_by"] = "none"
    if out["status"] is not None and out["status"].upper() not in ("OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"):
        out["status"] = None
    else:
        out["status"] = out["status"].upper() if out["status"] else None
    preset = str(out["time"].get("preset") or "all")
    if preset not in ("last_month", "this_month", "last_7d", "this_week", "all"):
        preset = "all"
    out["time"] = {"preset": preset}
    return out


def deterministic_spec(question: str) -> dict:
    """Rule-based stat spec extraction — the no-LLM path and the floor."""
    q = (question or "").lower()
    group_by = "none"
    for word, col in (("状态", "status"), ("队列", "queue"), ("类别", "category"),
                      ("分类", "category"), ("优先级", "priority")):
        if f"按{word}" in q or f"按{word}统计" in question:
            group_by = col
            break
    status = next((en for zh, en in _STATUS_WORDS.items() if zh in q), None)
    category = next((cat for word, cat in _CATEGORY_WORDS.items() if word in q), None)
    time_preset = "all"
    if "上个月" in q or "上月" in q:
        time_preset = "last_month"
    elif "本月" in q or "这个月" in q:
        time_preset = "this_month"
    elif "本周" in q or "这周" in q:
        time_preset = "this_week"
    elif re.search(r"最近[一二两三四五六七0-9]+天|最近7天", q):
        time_preset = "last_7d"
    return _clean({
        "metric": "count", "group_by": group_by,
        "status": status, "category": category,
        "time": {"preset": time_preset},
    })


_SPEC_INSTRUCTIONS = (
    "把用户的统计问题转换为 JSON 规格，字段：\n"
    '{"metric":"count","group_by":"none|status|queue|category|priority",'
    '"status":null|"OPEN"|"IN_PROGRESS"|"RESOLVED"|"CLOSED",'
    '"queue":null|"队列名","category":null|"network|account|device|software|billing|hr",'
    '"priority":null,"time":{"preset":"all|this_week|this_month|last_7d|last_month"}}\n'
    "只输出 JSON。"
)


class StatsAgent:
    """问数子代理：NL -> 受限统计规格 -> 只读执行 -> 口径明确的答案."""

    def __init__(self, llm=None, tickets: TicketStore | None = None) -> None:  # noqa: ANN001
        self._llm = llm
        self._tickets = tickets

    def run(self, question: str) -> StatsAnswer:
        spec, fallback_used = self._spec_for(question)
        since, until = parse_time_window(spec["time"]["preset"])
        rows: dict[str, int] = {}
        if self._tickets is not None:
            column = spec["group_by"] if spec["group_by"] != "none" else "status"
            rows = self._tickets.stats_filtered(
                column,
                status=spec["status"],
                queue=spec["queue"],
                category=spec["category"],
                priority=spec["priority"],
                since=since.isoformat() if since else None,
                until=until.isoformat() if until else None,
            )
        total = sum(rows.values())
        parts = [f"统计口径：{self._describe(spec)}"]
        if spec["group_by"] != "none" and rows:
            detail = "；".join(f"{k or '未设置'}={v}" for k, v in sorted(rows.items()))
            parts.append(f"共 {total} 单（{detail}）")
        elif rows:
            parts.append(f"{total} 单")
        else:
            parts.append("共 0 单" if rows is not None else "暂无数据")
        return StatsAnswer(text="。".join(parts), spec=spec, rows=rows, fallback_used=fallback_used)

    def _spec_for(self, question: str) -> tuple[dict, bool]:
        fallback = deterministic_spec(question)
        if self._llm is None:
            return fallback, True
        try:
            raw = self._llm.complete(system=_SPEC_INSTRUCTIONS, user=question)
            parsed = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            spec = _clean(parsed)
            # LLM spec must stay within the rule-extractor's expressiveness;
            # when the rules found a signal the LLM missed, trust the rules.
            if fallback["status"] and not spec["status"]:
                spec["status"] = fallback["status"]
            if fallback["category"] and not spec["category"]:
                spec["category"] = fallback["category"]
            if fallback["time"]["preset"] != "all" and spec["time"]["preset"] == "all":
                spec["time"] = fallback["time"]
            if fallback["group_by"] != "none" and spec["group_by"] == "none":
                spec["group_by"] = fallback["group_by"]
            return spec, False
        except Exception:  # noqa: BLE001 - degrade to deterministic parsing
            return fallback, True

    @staticmethod
    def _describe(spec: dict) -> str:
        bits = ["全部时间" if spec["time"]["preset"] == "all" else spec["time"]["preset"]]
        if spec["status"]:
            bits.append(f"状态={spec['status']}")
        if spec["category"]:
            bits.append(f"类别={spec['category']}")
        if spec["queue"]:
            bits.append(f"队列={spec['queue']}")
        if spec["group_by"] != "none":
            bits.append(f"按{spec['group_by']}分组")
        return "、".join(bits)
