"""C7: read-only MCP server for the support platform.

Wraps existing application services as standard MCP tools so ANY MCP
client (Claude Desktop / Cursor / other agents) can query tickets and
the knowledge base without bespoke integration — the "solves
integration fragmentation" capability, demonstrated on real protocol.

Governance (AGENTS.md 2026-08-24): READ-ONLY capabilities only. There
is deliberately no claim/resolve/close/approve tool here; anything that
could mutate sensitive Ticket state stays behind the deterministic
services + HITL pipeline (Invariant 4), and the server instructions say
so on the wire.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

READ_ONLY_DECLARATION = "[read-only]"

_INSTRUCTIONS = (
    "Support Agent Lite read-only MCP server.\n"
    "Exposes ticket case views, constrained statistics and knowledge search.\n"
    "Every tool is " + READ_ONLY_DECLARATION + ": there is intentionally no "
    "claim/resolve/close/approve surface. Invariant 4: agents must never "
    "directly mutate sensitive Ticket state; mutations go through the "
    "deterministic services + human-in-the-loop approval pipeline."
)


def build_mcp_server(
    *,
    ops: Any = None,
    seed_dir: str | Path = "seed/faq",
    conn: Any = None,
) -> MCPServer:
    """Assemble the MCPServer over existing services.

    `ops` is an OpsServices bundle (from app.main.build_ops). Pass `conn`
    instead to have one built lazily (used by the stdio entrypoint).
    """
    from app.infrastructure.db import apply_migrations, connect

    if ops is None:
        if conn is None:
            db_path = Path("runtime/support_agent.db")
            conn = connect(str(db_path)) if db_path.exists() else connect(":memory:")
            apply_migrations(conn)
        from app.infrastructure.repositories import TicketStore
        from app.main import build_ops

        ops = build_ops(conn, TicketStore(conn))

    store: TicketStore = getattr(ops.tickets, "_store", ops.tickets)
    retriever = RetrieverProxy(Path(seed_dir))
    server = MCPServer(
        name="support-agent-lite",
        title="Support Agent Lite (read-only)",
        description="Read-only ticket/knowledge access for enterprise support.",
        instructions=_INSTRUCTIONS,
        version="1.0.0",
    )

    @server.tool()
    def get_case(ticket_id: str) -> str:
        """[read-only] Full case view: ticket state + audit events."""
        ticket = store.get(ticket_id)
        if ticket is None:
            return json.dumps({"error": f"ticket not found: {ticket_id}"}, ensure_ascii=False)
        events = [
            {"type": e.event_type.value, "actor": e.actor_user_id or "-", "trace": e.trace_id or "-"}
            for e in store.events(ticket_id)
        ]
        return json.dumps(
            {
                "ticket": {
                    "id": ticket.id,
                    "status": ticket.status.value,
                    "title": ticket.title,
                    "priority": ticket.priority,
                    "queue": ticket.queue,
                    "assignee_user_id": ticket.assignee_user_id,
                },
                "events": events,
            },
            ensure_ascii=False,
        )

    @server.tool()
    def ticket_stats(group_by: str) -> str:
        """[read-only] Constrained grouped counts: status|queue|category|priority."""
        try:
            rows = store.stats_grouped(group_by)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        return json.dumps(
            {"group_by": group_by, "rows": rows, "total": sum(rows.values())},
            ensure_ascii=False,
        )

    @server.tool()
    def search_knowledge(query: str, top_k: int = 3) -> str:
        """[read-only] Hybrid knowledge search over the zh KB corpus."""
        hits = retriever.search(query, top_k=max(1, min(top_k, 8)))
        return json.dumps(
            {
                "hits": [
                    {
                        "doc_id": h.document.doc_id,
                        "title": h.document.title,
                        "score": round(h.score, 4),
                        "excerpt": h.document.content[:160],
                    }
                    for h in hits
                ]
            },
            ensure_ascii=False,
        )

    @server.resource("kb://catalog")
    def kb_catalog() -> str:
        """Knowledge base catalog (id/title/category/source_type)."""
        docs = [
            {
                "doc_id": d.doc_id,
                "title": d.title,
                "category": getattr(d, "category", ""),
                "source_type": d.source_type,
            }
            for d in retriever.documents
        ]
        return json.dumps({"total": len(docs), "documents": docs}, ensure_ascii=False)

    return server


class RetrieverProxy:
    """Lazy Retriever wrapper: corpus loads on first use, not at import."""

    def __init__(self, seed_dir: Path) -> None:
        self._seed_dir = seed_dir
        self._inner: Retriever | None = None

    def _get(self):
        from app.application.retriever import Retriever

        if self._inner is None:
            self._inner = Retriever(self._seed_dir)
        return self._inner

    def search(self, query: str, top_k: int = 3):
        return self._get().search(query, top_k=top_k)

    @property
    def documents(self):
        return self._get().documents


def main() -> int:
    """stdio entrypoint (Claude Desktop / inspector compatible)."""
    import asyncio

    server = build_mcp_server()
    asyncio.run(server.run_stdio_async())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
