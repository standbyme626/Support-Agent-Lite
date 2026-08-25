"""Build the similar-ticket vector index from existing tickets.

    .venv/bin/python scripts/build_ticket_index.py

Indexes every ticket's title+summary+category so the similar_tickets
tool can recall historical cases. Requires SILICONFLOW_API_KEY.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infrastructure.llm import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

from app.application.ticket_insights import TicketSimilarityIndex  # noqa: E402
from app.infrastructure.db import apply_migrations, connect  # noqa: E402
from app.infrastructure.repositories import TicketStore  # noqa: E402
from app.infrastructure.vector_store import SiliconFlowEmbedding  # noqa: E402


def main() -> int:
    db_path = ROOT / "runtime" / "support_agent.db"
    if not db_path.exists():
        print("no runtime database found", file=sys.stderr)
        return 1
    conn = connect(str(db_path))
    apply_migrations(conn)
    store = TicketStore(conn)
    rows = conn.execute(
        "SELECT id, title, summary, category, status FROM tickets ORDER BY created_at"
    ).fetchall()
    records = [
        {
            "ticket_id": r["id"],
            "text": f"{r['title']} {r['summary'] or ''} {r['category'] or ''}".strip(),
            "status": r["status"],
            "category": r["category"] or "",
        }
        for r in rows
    ]
    index = TicketSimilarityIndex(
        index_dir=ROOT / "runtime" / "ticket_index",
        embedding=SiliconFlowEmbedding(),
    )
    count = index.build(records)
    print(f"indexed {count} tickets → runtime/ticket_index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
