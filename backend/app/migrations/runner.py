"""Applies pending migration scripts, exactly once each.

Called from ONE place only: init_db() in app/database/db.py, at backend
startup. Nothing else runs migrations — not the updater, not any API route.
That single path is what makes update rollback predictable: if a migration
raises, startup fails, the updater's health check fails, and the standard
rollback restores both the code and the verified database backup.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import re
import sqlite3

from app.config import DATABASE_PATH

logger = logging.getLogger(__name__)

_SCRIPT_RE = re.compile(r"^(\d{3})_(.+)$")


def _discover() -> list[tuple[str, str, str]]:
    """Returns [(id, name, module_path)] sorted by id."""
    from app import migrations

    found: list[tuple[str, str, str]] = []
    for mod in pkgutil.iter_modules(migrations.__path__):
        match = _SCRIPT_RE.match(mod.name)
        if match:
            found.append((match.group(1), match.group(2), f"app.migrations.{mod.name}"))
    return sorted(found, key=lambda row: row[0])


def _applied_ids(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schemamigration'")
    if not cur.fetchone():
        return set()
    return {row[0] for row in conn.execute("SELECT id FROM schemamigration")}


def _record(conn: sqlite3.Connection, migration_id: str, name: str) -> None:
    from datetime import datetime

    conn.execute(
        "INSERT OR REPLACE INTO schemamigration (id, name, applied_at) VALUES (?, ?, ?)",
        (migration_id, name, datetime.now().isoformat(sep=" ")),
    )
    conn.commit()


def run_pending_migrations() -> list[str]:
    """Applies every not-yet-applied script in order. Returns the ids applied.

    Raises on the first failure — deliberately. A half-migrated database that
    the app keeps running on is far worse than a backend that refuses to
    start and triggers a rollback.
    """
    scripts = _discover()
    if not scripts:
        return []

    conn = sqlite3.connect(str(DATABASE_PATH))
    applied: list[str] = []
    try:
        done = _applied_ids(conn)
        for migration_id, name, module_path in scripts:
            if migration_id in done:
                continue
            logger.info("Applying migration %s_%s", migration_id, name)
            module = importlib.import_module(module_path)
            module.upgrade(conn)
            conn.commit()
            _record(conn, migration_id, name)
            applied.append(migration_id)
    finally:
        conn.close()

    if applied:
        logger.info("Applied %d migration(s): %s", len(applied), ", ".join(applied))
    return applied


# --- helpers for migration scripts -----------------------------------------

def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """Idempotent ALTER TABLE ADD COLUMN. Returns True if it added one."""
    if not table_exists(conn, table) or column_exists(conn, table, column):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True
