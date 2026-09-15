"""Phase F1 — identity-evaluation tables, on their OWN SQLModel metadata.

`init_db()` calls `SQLModel.metadata.create_all()` before running migrations,
so any table registered on the default metadata appears on every database the
moment its code ships — including production — with no migration and no
registry entry. These tables must only ever come from an explicit, recorded
migration (010), so they are registered on a separate metadata that
`create_all()` never sees.

Consequences, all deliberate and tested (tests/test_identity_metadata.py):
  * no ORM `foreign_key=` / relationships across the metadata boundary —
    SQLAlchemy resolves FK targets inside one MetaData only. Joins with
    Person/PhotoBatch are written explicitly, and lifecycle (delete with the
    source batch, null the person on participant deletion) is explicit code,
    which is also what SQLite requires here: the app runs with
    PRAGMA foreign_keys off, so a declared FK would never be enforced anyway;
  * the migration DDL is the schema of record; a test pins that it matches
    these models column for column.

Nothing here is trusted evidence or authoritative matching. Candidates are
evaluation data only; promotion to a trusted sample is hard-disabled
(identity_candidate_service.promotion_enabled).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, MetaData, UniqueConstraint
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel

from app.models.models import new_id

identity_metadata = MetaData()
identity_registry = registry(metadata=identity_metadata)


class IdentityModel(SQLModel, registry=identity_registry):
    """Base for tables that create_all() must never create."""


class EventIdentitySample(IdentityModel, table=True):
    """A TRUSTED extra reference face for Event Photo matching.

    The only source is `event_harvested` — a candidate that passed a calibrated
    gate. Enrollment embeddings are read from Person and never duplicated here.
    No row is written until calibration exists: promotion is hard-disabled.
    """

    __tablename__ = "eventidentitysample"

    id: str = Field(default_factory=new_id, primary_key=True)
    person_id: str = Field(index=True)
    batch_id: Optional[str] = Field(default=None, index=True)
    source: str  # event_harvested
    trust: str = Field(default="trusted")
    embedding: bytes  # float32[512], L2-normalised, numpy.tobytes() — same as Person.embedding
    pose_bucket: str = Field(default="unknown")
    quality_score: float = Field(default=0.0)
    face_pixel_area: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    # Provenance (migration 010): the candidate it was promoted from, and the
    # extractor that produced the embedding.
    source_candidate_id: Optional[str] = Field(default=None, index=True)
    extractor_version: Optional[str] = Field(default=None)


class EventIdentityCandidate(IdentityModel, table=True):
    """One Event-photo face captured for OFFLINE evaluation/calibration.

    `predicted_person_id` is what the model said; `label_person_id` /
    `label_status` are ground truth and are only ever set by a future offline
    label import — never by the pipeline. The embedding is never serialised by
    any API; only the server-side evaluation CLI reads it.
    """

    __tablename__ = "eventidentitycandidate"
    __table_args__ = (
        UniqueConstraint("photo_id", "face_index", "extractor_version",
                         name="uq_eventidentitycandidate_face_version"),
        Index("ix_eventidentitycandidate_reservoir",
              "batch_id", "capture_bucket", "extractor_version", "selection_key"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    batch_id: str = Field(index=True)
    photo_id: str = Field(index=True)
    face_id: Optional[str] = Field(default=None, index=True)
    face_index: int  # detection order within the photo — stable across retries
    predicted_person_id: Optional[str] = Field(default=None, index=True)
    label_person_id: Optional[str] = Field(default=None, index=True)
    label_status: str = Field(default="unlabeled")  # unlabeled | labeled_person | labeled_unknown | excluded
    top1_person_id: Optional[str] = Field(default=None)
    top1_score: float = Field(default=-1.0)
    top2_person_id: Optional[str] = Field(default=None)
    top2_score: float = Field(default=-1.0)
    margin: float = Field(default=0.0)
    capture_bucket: str  # matched | ambiguous | low_score | no_match
    selection_key: str  # sha256 hex of stable ids — bottom-k reservoir key
    face_area_px: int = Field(default=0)
    det_score: float = Field(default=0.0)
    sharpness: float = Field(default=0.0)  # Laplacian variance of the grey face crop
    pose: Optional[str] = Field(default=None)
    bbox: str  # "x1,y1,x2,y2" in ORIGINAL-image pixels
    image_w: int = Field(default=0)
    image_h: int = Field(default=0)
    extractor_version: str
    recognition_threshold: float = Field(default=0.0)
    embedding: bytes
    created_at: datetime = Field(default_factory=datetime.now)
