"""Phase F1 — identity-evaluation schema, created explicitly.

The F1 tables now live on a separate SQLModel metadata
(app/models/identity_models.py) so `create_all()` never creates them; this
migration is the only thing that does.

It is written to be correct on every database shape that exists:
  * fresh database — creates both tables and every index;
  * Dummy, where 007 already ran and `create_all()` had created
    `eventidentitysample` (0 rows) under the old registration — the table is
    left as is (IF NOT EXISTS), the two new provenance columns are added in
    place, nothing is dropped or rewritten;
  * production (001–006 + 008, no 007) — 007 runs first as the no-op it has
    always been, then this creates everything. 007 is deliberately left
    byte-identical: Dummy already records it as applied, and rewriting an
    applied migration would make the two databases disagree about what it did.

ROLLBACK NOTE: as with 004–009, rollback means reverting the code that reads
these tables, never dropping them.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing

_SAMPLE_DDL = """
CREATE TABLE IF NOT EXISTS eventidentitysample (
    id VARCHAR NOT NULL,
    person_id VARCHAR NOT NULL,
    batch_id VARCHAR,
    source VARCHAR NOT NULL,
    trust VARCHAR NOT NULL,
    embedding BLOB NOT NULL,
    pose_bucket VARCHAR NOT NULL,
    quality_score FLOAT NOT NULL,
    face_pixel_area INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    source_candidate_id VARCHAR,
    extractor_version VARCHAR,
    PRIMARY KEY (id),
    FOREIGN KEY(person_id) REFERENCES person (id),
    FOREIGN KEY(batch_id) REFERENCES photobatch (id)
)"""

_CANDIDATE_DDL = """
CREATE TABLE IF NOT EXISTS eventidentitycandidate (
    id VARCHAR NOT NULL,
    batch_id VARCHAR NOT NULL,
    photo_id VARCHAR NOT NULL,
    face_id VARCHAR,
    face_index INTEGER NOT NULL,
    predicted_person_id VARCHAR,
    label_person_id VARCHAR,
    label_status VARCHAR NOT NULL,
    top1_person_id VARCHAR,
    top1_score FLOAT NOT NULL,
    top2_person_id VARCHAR,
    top2_score FLOAT NOT NULL,
    margin FLOAT NOT NULL,
    capture_bucket VARCHAR NOT NULL,
    selection_key VARCHAR NOT NULL,
    face_area_px INTEGER NOT NULL,
    det_score FLOAT NOT NULL,
    sharpness FLOAT NOT NULL,
    pose VARCHAR,
    bbox VARCHAR NOT NULL,
    image_w INTEGER NOT NULL,
    image_h INTEGER NOT NULL,
    extractor_version VARCHAR NOT NULL,
    recognition_threshold FLOAT NOT NULL,
    embedding BLOB NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_eventidentitycandidate_face_version UNIQUE (photo_id, face_index, extractor_version)
)"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_eventidentitysample_person_id ON eventidentitysample (person_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventidentitysample_batch_id ON eventidentitysample (batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventidentitysample_source_candidate_id ON eventidentitysample (source_candidate_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventidentitycandidate_batch_id ON eventidentitycandidate (batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventidentitycandidate_photo_id ON eventidentitycandidate (photo_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventidentitycandidate_face_id ON eventidentitycandidate (face_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventidentitycandidate_predicted_person_id ON eventidentitycandidate (predicted_person_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventidentitycandidate_label_person_id ON eventidentitycandidate (label_person_id)",
    "CREATE INDEX IF NOT EXISTS ix_eventidentitycandidate_reservoir "
    "ON eventidentitycandidate (batch_id, capture_bucket, extractor_version, selection_key)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_SAMPLE_DDL)
    # Dummy's pre-existing table (created by create_all under 007) lacks these.
    add_column_if_missing(conn, "eventidentitysample", "source_candidate_id", "VARCHAR")
    add_column_if_missing(conn, "eventidentitysample", "extractor_version", "VARCHAR")
    conn.execute(_CANDIDATE_DDL)
    for ddl in _INDEXES:
        conn.execute(ddl)
