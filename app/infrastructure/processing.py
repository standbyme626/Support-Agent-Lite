"""Durable inbound processing lifecycle (V2.1 two-phase ingress).

The claim table (`processed_messages`) proves a message was seen; this
store tracks HOW FAR processing got, so a crash between phase A (domain
committed) and phase B (agent decision applied) can be resumed by a
later duplicate delivery without re-running deterministic effects.

State machine (CAS-guarded updates, exactly-once phase B):

    RECEIVED  -> AGENT_PENDING -> AGENT_COMPLETED -> COMPLETED
                                    (phase B in progress, same txn)
    AGENT_PENDING -> FAILED_RETRYABLE   (phase B failed, rollback)
    AGENT_PENDING/FAILED_RETRYABLE -> resume on duplicate delivery
    COMPLETED  -> duplicate deliveries are no-ops
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class ProcessingState(str, Enum):
    RECEIVED = "RECEIVED"
    AGENT_PENDING = "AGENT_PENDING"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"

    # states a duplicate delivery may resume from
    RESUMABLE = frozenset({AGENT_PENDING, FAILED_RETRYABLE})
    # states where a duplicate delivery must NOT re-run anything
    FINAL = frozenset({COMPLETED, AGENT_COMPLETED})


@dataclass(slots=True)
class ProcessingRecord:
    idempotency_key: str
    trace_id: str
    channel: str
    state: ProcessingState
    kind: str | None = None
    ticket_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    conversation_channel: str | None = None
    conversation_id: str | None = None
    intent: str | None = None
    reply: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class InboundProcessingStore:
    """SQLite-backed processing lifecycle. All methods must be called
    inside the caller's `txn(conn)` so claim + state + business effects
    commit atomically."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def claim(
        self,
        key: str,
        *,
        trace_id: str,
        channel: str,
        user_id: str,
        session_id: str,
        conversation_channel: str | None,
        conversation_id: str | None,
    ) -> None:
        now = _ts()
        self._conn.execute(
            "INSERT OR REPLACE INTO inbound_processing "
            "(idempotency_key, trace_id, channel, state, user_id, session_id, "
            " conversation_channel, conversation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, trace_id, channel, ProcessingState.RECEIVED.value, user_id, session_id,
             conversation_channel, conversation_id, now, now),
        )

    def update(
        self,
        key: str,
        *,
        state: ProcessingState | None = None,
        kind: str | None = None,
        ticket_id: str | None = None,
        intent: str | None = None,
        reply: str | None = None,
        error: str | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[object] = []
        if state is not None:
            fields.append("state = ?")
            values.append(state.value)
        if kind is not None:
            fields.append("kind = ?")
            values.append(kind)
        if ticket_id is not None:
            fields.append("ticket_id = ?")
            values.append(ticket_id)
        if intent is not None:
            fields.append("intent = ?")
            values.append(intent)
        if reply is not None:
            fields.append("reply = ?")
            values.append(reply)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if not fields:
            return
        fields.append("updated_at = ?")
        values.append(_ts())
        values.append(key)
        self._conn.execute(
            f"UPDATE inbound_processing SET {', '.join(fields)} WHERE idempotency_key = ?",
            values,
        )

    def get(self, key: str) -> ProcessingRecord | None:
        row = self._conn.execute(
            "SELECT * FROM inbound_processing WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return self._row(row) if row else None

    def advance(self, key: str, expected: ProcessingState, target: ProcessingState) -> bool:
        """CAS state transition: exactly one concurrent worker wins."""
        cursor = self._conn.execute(
            "UPDATE inbound_processing SET state = ?, updated_at = ? "
            "WHERE idempotency_key = ? AND state = ?",
            (target.value, _ts(), key, expected.value),
        )
        return cursor.rowcount == 1

    def is_final(self, key: str) -> bool:
        record = self.get(key)
        return record is not None and record.state in ProcessingState.FINAL

    @staticmethod
    def _row(row: sqlite3.Row) -> ProcessingRecord:
        return ProcessingRecord(
            idempotency_key=row["idempotency_key"],
            trace_id=row["trace_id"],
            channel=row["channel"],
            state=ProcessingState(row["state"]),
            kind=row["kind"],
            ticket_id=row["ticket_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            conversation_channel=row["conversation_channel"],
            conversation_id=row["conversation_id"],
            intent=row["intent"],
            reply=row["reply"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
