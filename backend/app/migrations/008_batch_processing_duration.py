"""Phase G4 — persist how long a batch's processing actually took.

`PhotoBatch` had no processing timestamps at all (only created_at,
retention_start_at and delete_at), and the live ETA is in-memory only, so a
refresh or a backend restart lost the figure entirely. Two nullable datetimes
make the final "Completed in ..." line backend-authoritative rather than a
browser timer.

Both are NULL for every batch that finished before this shipped — the UI
simply omits the line for those rather than inventing a number.

Idempotent by construction via add_column_if_missing.

ROLLBACK NOTE: as with 004-007, rollback means reverting the CODE that reads
these columns, never dropping them.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "photobatch", "processing_started_at", "DATETIME")
    add_column_if_missing(conn, "photobatch", "processing_finished_at", "DATETIME")
