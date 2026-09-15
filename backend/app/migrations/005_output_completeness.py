"""Output Completeness Invariant — one new counter column on photobatch.

`rejected_photos` distinguishes permanent, pre-acceptance validation
failures (never retried) from `failed_photos` (post-acceptance, retriable
via retry-unresolved). The per-item detail table this phase also needs,
PhotoBatchIngestionIssue, is a brand-new table — create_all() (which runs
before migrations, see app/migrations/__init__.py) creates it automatically,
so it needs no ALTER-based migration of its own.

Idempotent by construction via add_column_if_missing.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "photobatch", "rejected_photos", "INTEGER NOT NULL DEFAULT 0")
