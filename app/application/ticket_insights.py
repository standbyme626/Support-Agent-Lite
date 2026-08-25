"""B-batch: similar-ticket recall + case timeline tool.

similar_tickets: embed ticket summaries into a local vector index so a
new problem can find historical cases (the "history value" payoff).
case_timeline: chronological audit-event view of one ticket.

Both read-only; the similarity index degrades to "unavailable" when no
embedding backend/index exists — never breaks the agent loop.
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.infrastructure.vector_store import NumpyVectorStore


class TicketSimilarityIndex:
    """Vector index over ticket summaries (default dir runtime/ticket_index)."""

    def __init__(
        self,
        index_dir: str | Path = "runtime/ticket_index",
        embedding=None,
    ) -> None:
        self._dir = Path(index_dir)
        self._embedding = embedding
        self._store: NumpyVectorStore | None = None
        self._loaded = False

    def _ensure_loaded(self) -> bool:
        if not self._loaded:
            store = NumpyVectorStore(self._dir)
            if store.load():
                self._store = store
            self._loaded = True
        return self._store is not None and self._embedding is not None

    @property
    def available(self) -> bool:
        return self._ensure_loaded()

    def build(self, records: list[dict]) -> int:
        """records: [{ticket_id, text, status, category}]. Returns count."""
        if not records or self._embedding is None:
            return 0
        vectors = self._embedding.embed([r["text"] for r in records])
        store = NumpyVectorStore(self._dir)
        docs = [
            {
                "doc_id": r["ticket_id"],
                "text": r["text"],
                "title": r.get("status", ""),
                "category": r.get("category", ""),
                "source_type": "ticket",
            }
            for r in records
        ]
        store.build(docs, vectors)
        store.save()
        self._store = store
        self._loaded = True
        return len(records)

    def search(self, query: str, top_k: int = 3) -> list[tuple[str, float]]:
        if not self._ensure_loaded():
            return []
        try:
            qvec = self._embedding.embed([query])[0]
            return [(str(tid), float(score)) for tid, score in self._store.search(qvec, top_k=top_k)]
        except Exception as exc:  # noqa: BLE001 - degrade silently
            print(f"[ticket-index] search degraded: {exc!r}", file=sys.stderr)
            return []


def format_case_timeline(store, ticket_id: str) -> str:
    """Chronological audit view for the case_timeline tool (read-only)."""
    ticket = store.get(ticket_id)
    if ticket is None:
        return f"工单不存在:{ticket_id}"
    lines = [
        f"工单 {ticket.id} [{ticket.status.value}] {ticket.title}"
        f"(优先级={ticket.priority or '-'} 队列={ticket.queue or '-'} 处理人={ticket.assignee_user_id or '-'})"
    ]
    events = store.events(ticket_id)
    if not events:
        lines.append("（无审计事件）")
    for e in events:
        actor = e.actor_user_id or "-"
        payload_note = ""
        if isinstance(e.payload, dict) and e.payload:
            payload_note = " " + json_compact(e.payload)
        lines.append(f"{e.created_at.isoformat(timespec='seconds')} {e.event_type.value} by {actor}{payload_note}")
    return "\n".join(lines)


def json_compact(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
