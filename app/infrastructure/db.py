"""SQLite connection management, migrations and thread-safe serialization.

V2 runs one process and (by design) one SQLite database. All services
share a single connection; a `SerializedConnection` wrapper serializes
every statement so concurrent webhooks/claims cannot interleave inside
an open transaction. The nested-safe `txn()` helper (repositories) holds
the same lock across a whole transaction.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "storage" / "migrations"


class SerializedCursor:
    """Cursor wrapper: row fetching stays inside the connection lock.

    sqlite3 cursors step lazily — `execute()` only fetches the first row,
    and `fetchall()`/`fetchone()`/iteration step the rest. If those later
    steps ran OUTSIDE the serialization lock, another thread's transaction
    could interleave with an in-flight read statement on the shared
    connection (torn rows / 'cannot start a transaction within a
    transaction'). All data access therefore holds the lock.
    """

    def __init__(self, conn: "SerializedConnection", cursor: sqlite3.Cursor) -> None:
        self._conn = conn
        self._cursor = cursor

    def fetchall(self):
        with self._conn._txn_lock:  # noqa: SLF001
            return self._cursor.fetchall()

    def fetchone(self):
        with self._conn._txn_lock:  # noqa: SLF001
            return self._cursor.fetchone()

    def __iter__(self):
        with self._conn._txn_lock:  # noqa: SLF001
            return iter(self._cursor.fetchall())

    def close(self) -> None:
        with self._conn._txn_lock:  # noqa: SLF001
            self._cursor.close()

    def __enter__(self) -> "SerializedCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class SerializedConnection:
    """sqlite3.Connection wrapper: every statement passes one global lock."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._txn_lock = lock

    def execute(self, sql: str, parameters: Any = ()) -> SerializedCursor:
        with self._txn_lock:
            return SerializedCursor(self, self._conn.execute(sql, parameters))

    def executemany(self, sql: str, seq_of_parameters: Any) -> sqlite3.Cursor:
        with self._txn_lock:
            return self._conn.executemany(sql, seq_of_parameters)

    def executescript(self, script: str) -> sqlite3.Cursor:
        with self._txn_lock:
            return self._conn.executescript(script)

    def commit(self) -> None:
        with self._txn_lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._txn_lock:
            self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> "SerializedConnection":
        # `with conn:` would commit on exit and break nested transactions.
        # Use `txn(conn)` instead.
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


def connect(db_path: str | Path) -> SerializedConnection:
    """Open a serialized sqlite connection with foreign keys enabled.

    Use `txn(conn)` for multi-statement atomic units (ticket state +
    TicketEvent + notification outbox must commit together).
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.isolation_level = None  # autocommit per statement; txn() is explicit
    return SerializedConnection(conn, threading.RLock())


def apply_migrations(conn: SerializedConnection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply all *.up.sql migrations not yet recorded. Returns applied names."""
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    result: list[str] = []
    for up_file in sorted(migrations_dir.glob("*.up.sql")):
        if up_file.name in applied:
            continue
        conn.executescript(up_file.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
            (up_file.name,),
        )
        result.append(up_file.name)
    conn.commit()
    return result
