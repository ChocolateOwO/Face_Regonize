"""Carry the sheet's PDPA answer through the import.

`importrow.consent` holds "consented" / "declined" / NULL for each row, read
from the registration form's consent column during a Google Sheet sync and
turned into a ConsentRecord once the participant is actually created.

Guarded and idempotent, like every migration here.

Deliberately no backfill: rows imported before this existed have no recorded
answer, and NULL says exactly that. Inventing "consented" for them would be
fabricating the very evidence the PDPA record exists to provide.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "importrow", "consent", "TEXT")
