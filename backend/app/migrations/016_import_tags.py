"""Tags column in Sheet/Excel imports (robot hub F2).

Additive and idempotent:
  importrow.tags        JSON NULL      valid tags read from the row; NULL = no Tags column
  importrow.tag_errors  VARCHAR NULL   invalid values from the row, reported and skipped
  importjob.tags_mode   VARCHAR NOT NULL DEFAULT 'merge'   merge | replace for existing people

Existing import jobs and rows get NULL / 'merge'; nothing is re-run.

ROLLBACK NOTE: as with 004-015, rollback means reverting the CODE, never
dropping the columns.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "importrow", "tags", "JSON")
    add_column_if_missing(conn, "importrow", "tag_errors", "VARCHAR")
    add_column_if_missing(conn, "importjob", "tags_mode", "VARCHAR NOT NULL DEFAULT 'merge'")
