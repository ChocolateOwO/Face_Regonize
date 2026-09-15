"""Adaptive identity (Phase F1) — the EventIdentitySample table.

Deliberately a no-op ALTER. The only schema this phase adds is one brand-new
table, and `create_all()` runs before migrations (see
app/migrations/__init__.py), so the table already exists by the time this is
reached — the same reason 005's PhotoBatchIngestionIssue and 006's two review
tables needed no ALTER either.

It is still registered as 007 so the migration numbering matches the phase it
belongs to, and so there is a recorded, ordered place to hang a backfill if
one is ever needed (for example, seeding enrollment-derived samples).

ROLLBACK NOTE: as with 004-006, rollback means reverting the CODE that reads
the table, never dropping it. Nothing outside Phase F reads
EventIdentitySample, so its mere presence is inert.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    return None
