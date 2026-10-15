"""Robot integration — address name, history provenance, history cursor.

Additive and idempotent:
  person.call_as            TEXT NULL  how a voice client should address them
  upload.source_kind        TEXT NULL  kiosk | node_event | node_central
  upload.source_node_id     TEXT NULL  the reporting node ("kiosk" for kiosk)
  facedetection.seq         INTEGER NULL + index  write-order cursor
  historysequence           AUTOINCREMENT counter that issues seq

Existing rows keep NULL in every new column: their origin and write order
were never recorded, so they are reported as unknown and are not part of the
incremental cursor. Nothing is backfilled or guessed.

Numbered 012 because 011 is used by an unrelated Dummy-only migration; the
runner applies by id and does not require consecutive numbers.

ROLLBACK NOTE: as with 004-010, rollback means reverting the CODE that reads
these columns, never dropping them.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "person", "call_as", "VARCHAR")
    add_column_if_missing(conn, "upload", "source_kind", "VARCHAR")
    add_column_if_missing(conn, "upload", "source_node_id", "VARCHAR")
    add_column_if_missing(conn, "facedetection", "seq", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_facedetection_seq ON facedetection (seq)")
    # Normally already created by SQLModel.metadata.create_all() at startup;
    # repeated here so the migration is complete on its own.
    conn.execute("CREATE TABLE IF NOT EXISTS historysequence (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT)")
