"""Event Photo Processing dual-output columns (v2.0/v2.1).

These columns were added by hand on the development machine while the feature
was being built, before this migration runner existed. This script exists so
any OTHER installation reaches the same schema through an update instead of
manual SQL.

Idempotent by construction: every add is guarded, so running it on the machine
that was already patched by hand is a no-op.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing

PHOTOBATCH_COLUMNS = [
    ("storage_dir", "TEXT DEFAULT ''"),
    ("failed_photos", "INTEGER DEFAULT 0"),
    ("last_error", "TEXT"),
    ("drive_failed_photos", "INTEGER DEFAULT 0"),
    ("drive_error", "TEXT"),
]

PHOTOBATCHPHOTO_COLUMNS = [
    ("original_path", "TEXT DEFAULT ''"),
    ("media_path", "TEXT"),
    ("media_drive_file_id", "TEXT"),
    ("drive_upload_status", "TEXT DEFAULT 'pending'"),
    ("drive_error", "TEXT"),
]


def upgrade(conn: sqlite3.Connection) -> None:
    for column, ddl in PHOTOBATCH_COLUMNS:
        add_column_if_missing(conn, "photobatch", column, ddl)
    for column, ddl in PHOTOBATCHPHOTO_COLUMNS:
        add_column_if_missing(conn, "photobatchphoto", column, ddl)
