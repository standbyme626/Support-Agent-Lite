"""Idempotency store: dedupe inbound messages by channel message id."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.infrastructure.repositories import txn


class IdempotencyStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.isolation_level = None

    def claim(self, key: str, trace_id: str) -> bool:
        """Atomically claim an idempotency key.

        INSERT OR IGNORE: two concurrent requests with the same key ->
        exactly one claims (rowcount == 1). Callers run this inside the
        business transaction so a business failure rolls the claim back
        with it (message stays retryable).
        """
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO processed_messages (idempotency_key, trace_id, processed_at) "
            "VALUES (?, ?, ?)",
            (key, trace_id, datetime.now(timezone.utc).isoformat()),
        )
        return cursor.rowcount == 1

    def is_processed(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row is not None

    def mark_processed(self, key: str, trace_id: str) -> None:
        """Legacy non-atomic mark (kept for API compatibility)."""
        with txn(self._conn):
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_messages (idempotency_key, trace_id, processed_at) "
                "VALUES (?, ?, ?)",
                (key, trace_id, datetime.now(timezone.utc).isoformat()),
            )

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]