"""SQLite connection management and migration runner."""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "storage" / "migrations"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a sqlite connection with foreign keys enabled.

    Use `conn.isolation_level = None` + explicit BEGIN so we control
    transactions precisely for ticket+event atomic writes.
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
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
