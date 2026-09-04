"""Event activities, and the attendance link to them.

The `activity` table itself is created by SQLModel's create_all(), which runs
before migrations and does add missing TABLES. What it never does is add a
COLUMN to a table that already exists, which is why the attendance link needs
this script.

Idempotent by construction: the add is guarded, so this is a no-op on a
database that already has the column.

Deliberately does NOT backfill. Attendance rows written before activities
existed keep activity_id NULL, which reads correctly as "checked in, no
activity recorded" rather than inventing an activity they never belonged to.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "attendance", "activity_id", "TEXT")
