"""Optional per-person title and spoken language for voice clients.

Additive and idempotent:
  person.title     TEXT NULL  admin-entered title/position ("Dr.", "อาจารย์")
  person.language  TEXT NULL  BCP 47 language tag the robot should speak ("th")

Existing rows keep NULL: nothing is backfilled or guessed from a name, a
photo or registration data.

ROLLBACK NOTE: as with 004-013, rollback means reverting the CODE that reads
these columns, never dropping them.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "person", "title", "VARCHAR")
    add_column_if_missing(conn, "person", "language", "VARCHAR")
