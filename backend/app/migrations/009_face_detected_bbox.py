"""Phase D1 — preserve the detector's original box when a face box is edited.

`PhotoBatchFace.detected_bbox` is NULL until a face's box is first edited, at
which point it is set, once and immutably, to the box the detector produced.
For a face whose privacy decision requires a mask, every later edit must still
contain that region (enlarge / extend only), so repeated edits can never shrink
a declined participant's mask below what the detector found.

Nullable and additive: a NULL simply means "never edited — `bbox` is the
detector output". Idempotent by construction via add_column_if_missing.

ROLLBACK NOTE: as with 004-008, rollback means reverting the CODE that reads
the column, never dropping it.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "photobatchface", "detected_bbox", "TEXT")
