"""Idempotency store: dedupe inbound messages by channel message id."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


class IdempotencyStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.isolation_level = None

    def is_processed(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row is not None

    def mark_processed(self, key: str, trace_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_messages (idempotency_key, trace_id, processed_at) "
                "VALUES (?, ?, ?)",
                (key, trace_id, datetime.now(timezone.utc).isoformat()),
            )

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]