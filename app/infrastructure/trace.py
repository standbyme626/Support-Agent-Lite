"""TraceLogger: stage-by-stage record for one trace_id.

Phase 7: one trace_id covers channel / identity / intent / retrieval /
ticket / agent / approval / memory / reply, so a single message's full
journey is inspectable via `GET /traces/{trace_id}`.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TraceEvent:
    trace_id: str
    stage: str
    payload: dict[str, Any]
    created_at: str


class TraceLogger:
    """Appends immutable stage events per trace_id (sqlite-backed)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def event(self, trace_id: str, stage: str, payload: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "INSERT INTO trace_events (trace_id, stage, payload, created_at) VALUES (?, ?, ?, ?)",
            (trace_id, stage, json.dumps(payload or {}, ensure_ascii=False), _ts()),
        )

    def get(self, trace_id: str) -> list[TraceEvent]:
        rows = self._conn.execute(
            "SELECT * FROM trace_events WHERE trace_id = ? ORDER BY id", (trace_id,)
        ).fetchall()
        return [
            TraceEvent(
                trace_id=r["trace_id"],
                stage=r["stage"],
                payload=json.loads(r["payload"]) if r["payload"] else {},
                created_at=r["created_at"],
            )
            for r in rows
        ]
