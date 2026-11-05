"""Person tags for clients such as the robot hub (F1).

Additive and idempotent:
  person.tags  JSON NOT NULL DEFAULT '[]'  short lowercase labels, e.g. ["vip","staff"]

Every existing person gets [] (no tags). Nothing is inferred.

ROLLBACK NOTE: as with 004-014, rollback means reverting the CODE that reads
this column, never dropping it; older code ignores it.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "person", "tags", "JSON NOT NULL DEFAULT '[]'")
