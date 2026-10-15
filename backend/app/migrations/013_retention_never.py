"""Event Photo retention "Never" - a real persisted state.

  photobatch.retention_policy  VARCHAR NOT NULL DEFAULT 'days'   ("days" | "never")
  photobatch.delete_at         becomes NULLable (NULL = no deadline)

Every existing batch keeps policy "days" and the exact deadline it already
had: this migration changes no batch's retention. Converting particular
batches to Never is a separate, explicit action (the per-batch Change
Retention control, or scripts/set_retention_never.py for an offline
database).

Why delete_at must become nullable rather than hold a far-future date: Never
has to mean "there is no deadline", so that nothing which compares dates -
including an older copy of this code after a rollback, whose cleanup is
simply `delete_at <= now` - can ever expire such a batch. NULL is never
<= now.

SQLite cannot drop NOT NULL in place, so the table is rebuilt the documented
way (create new, copy, drop, rename) inside one transaction. The new table's
definition is the EXISTING definition with only that one constraint removed,
so it carries whatever columns and defaults this particular database has;
rows are copied column-for-column and counted before the old table goes.

Idempotent: each step checks whether it is already done.

ROLLBACK NOTE: as with 004-012, rollback means reverting the code, never the
schema. Older code tolerates both changes (it ignores the extra column, and a
NULL deadline is simply never selected by its cleanup).
"""
from __future__ import annotations

import re
import sqlite3

from app.migrations.runner import add_column_if_missing, table_exists

_TABLE = "photobatch"
_TEMP = "photobatch__013_rebuild"
_NOT_NULL_DEADLINE = re.compile(r"(\bdelete_at\s+DATETIME)\s+NOT\s+NULL", re.IGNORECASE)


def _deadline_is_not_null(conn: sqlite3.Connection) -> bool:
    for row in conn.execute(f"PRAGMA table_info({_TABLE})"):
        if row[1] == "delete_at":
            return bool(row[3])
    raise RuntimeError("photobatch.delete_at is missing")


def upgrade(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, _TABLE):
        return
    add_column_if_missing(conn, _TABLE, "retention_policy", "VARCHAR NOT NULL DEFAULT 'days'")
    conn.commit()
    if not _deadline_is_not_null(conn):
        return

    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone()[0]
    new_ddl, replaced = _NOT_NULL_DEADLINE.subn(r"\1", ddl)
    if replaced != 1:
        raise RuntimeError("Could not locate exactly one 'delete_at DATETIME NOT NULL' in the photobatch definition.")
    new_ddl, renamed = re.subn(r"^\s*CREATE\s+TABLE\s+\"?photobatch\"?", f'CREATE TABLE "{_TEMP}"', new_ddl,
                               count=1, flags=re.IGNORECASE)
    if renamed != 1:
        raise RuntimeError("Unexpected photobatch CREATE TABLE statement.")
    # Explicit indexes/triggers are dropped with the old table; recreate them
    # verbatim afterwards. (Auto-indexes have sql NULL and come back by themselves.)
    dependents = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (_TABLE,))]
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Refusing to rebuild photobatch with foreign-key enforcement on.")

    before = conn.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{_TEMP}"')  # leftover of an interrupted attempt; never the live table
        conn.execute(new_ddl)
        conn.execute(f'INSERT INTO "{_TEMP}" SELECT * FROM {_TABLE}')
        copied = conn.execute(f'SELECT count(*) FROM "{_TEMP}"').fetchone()[0]
        if copied != before:
            raise RuntimeError(f"photobatch rebuild copied {copied} of {before} rows")
        conn.execute(f"DROP TABLE {_TABLE}")
        conn.execute(f'ALTER TABLE "{_TEMP}" RENAME TO {_TABLE}')
        for sql in dependents:
            conn.execute(sql)
        if conn.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0] != before:
            raise RuntimeError("photobatch row count changed during rebuild")
        if _deadline_is_not_null(conn):
            raise RuntimeError("photobatch.delete_at is still NOT NULL after rebuild")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
