from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


def new_id() -> str:
    return uuid.uuid4().hex


class User(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(default="admin")
    created_at: datetime = Field(default_factory=datetime.now)


class Person(SQLModel, table=True):
    """A registered participant. One reference face image -> one embedding."""

    id: str = Field(default_factory=new_id, primary_key=True)
    participant_id: str = Field(unique=True, index=True)
    first_name: str
    last_name: str = Field(default="")
    email: Optional[str] = None
    image_path: str  # relative path under storage/people/
    image_source: str = Field(default="manual")  # manual | google_drive
    original_image_url: Optional[str] = None
    embedding: bytes  # float32[512] packed with numpy.tobytes()
    det_score: float = 0.0
    is_demo: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class Upload(SQLModel, table=True):
    """An event photo submitted for recognition."""

    id: str = Field(default_factory=new_id, primary_key=True)
    filename: str
    image_path: str  # relative path under storage/events/
    uploaded_at: datetime = Field(default_factory=datetime.now)
    uploaded_by: Optional[str] = Field(default=None, foreign_key="user.id")
    processing_status: str = Field(default="pending")  # pending | processing | completed | failed
    processing_duration_ms: Optional[float] = None
    threshold_used: float = 0.0
    faces_total: int = 0
    faces_matched: int = 0
    faces_unknown: int = 0


class FaceDetection(SQLModel, table=True):
    """One detected face within an upload, matched or not."""

    id: str = Field(default_factory=new_id, primary_key=True)
    upload_id: str = Field(foreign_key="upload.id", index=True)
    person_id: Optional[str] = Field(default=None, foreign_key="person.id", index=True)
    confidence: float  # similarity score (0-1) of the best candidate, whether matched or not
    bbox: str  # "x1,y1,x2,y2"
    detected_at: datetime = Field(default_factory=datetime.now)
    status: str = Field(default="unknown")  # matched | unknown


class Activity(SQLModel, table=True):
    """Something a participant can be checked in to during an event - "Food",
    "Gadget", "Registration". Which one the kiosk is currently checking people
    into is NOT stored here: it lives in the `current_activity_id` Setting, so
    exactly one can be current by construction rather than by keeping a bool
    mutually exclusive across rows.

    Archiving hides an activity from the kiosk picker but keeps its
    attendance history intact and still reportable."""

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    archived: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.now)


class Attendance(SQLModel, table=True):
    """One attendance record created whenever a face_detection matches a known person."""

    id: str = Field(default_factory=new_id, primary_key=True)
    person_id: str = Field(foreign_key="person.id", index=True)
    upload_id: str = Field(foreign_key="upload.id", index=True)
    face_detection_id: str = Field(foreign_key="facedetection.id")
    confidence: float
    detected_at: datetime = Field(default_factory=datetime.now)
    status: str = Field(default="detected")
    # Which activity this check-in belongs to. Nullable on purpose: rows
    # written before activities existed keep NULL, and a kiosk running with no
    # activity selected still records plain attendance exactly as before.
    activity_id: Optional[str] = Field(default=None, foreign_key="activity.id", index=True)


class ImportJob(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    filename: str
    file_type: str  # csv | xlsx | xls
    total_rows: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    status: str = Field(default="pending")  # pending | processing | completed | failed
    duplicate_strategy: str = Field(default="skip")  # skip | update
    imported_at: datetime = Field(default_factory=datetime.now)
    imported_by: Optional[str] = Field(default=None, foreign_key="user.id")
    current_stage: str = Field(default="")


class ImportRow(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    import_id: str = Field(foreign_key="importjob.id", index=True)
    row_number: int
    participant_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    image_url: Optional[str] = None
    status: str = Field(default="pending")  # ready | warning | error | imported | skipped
    error_message: Optional[str] = None
    # PDPA answer carried from the sheet, "consented" / "declined" / None.
    # None means the sheet had no usable answer, and no consent record is
    # invented for that person.
    consent: Optional[str] = None


class ConsentRecord(SQLModel, table=True):
    """One row per PDPA consent action for a participant. Append-only —
    never updated or overwritten, so a participant's full consent history is
    always intact. There is deliberately no separate "current status" column
    anywhere: a participant's current status is always whichever
    ConsentRecord row has the latest recorded_at, computed on read. This
    makes "current status out of sync with history" structurally impossible
    rather than something that has to be kept in sync by hand.

    `source` distinguishes a participant's own kiosk tap from an admin
    manually setting their status — a compliance record that can't tell
    those apart isn't trustworthy as evidence of actual consent."""

    id: str = Field(default_factory=new_id, primary_key=True)
    person_id: str = Field(foreign_key="person.id", index=True)
    choice: str  # "consented" | "declined"
    source: str = Field(default="kiosk")  # "kiosk" | "admin" | "registration"
    recorded_at: datetime = Field(default_factory=datetime.now)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class PhotoBatch(SQLModel, table=True):
    """One photographer photo-processing run ("the event" in the retention
    sense) — its own isolated storage directory and its own retention timer,
    completely separate from the kiosk's Upload/FaceDetection/Attendance
    tables so deleting an expired batch can never touch kiosk data, and
    kiosk activity can never touch a batch's data.

    Output goes to two independent places: storage_dir on local disk (which
    backs the in-app web preview) and, best-effort, the *_folder_id Drive
    folders created inside the photographer's own shared folder."""

    id: str = Field(default_factory=new_id, primary_key=True)
    label: str  # admin-facing name, e.g. the Drive folder name
    drive_folder_id: str  # the photographer's source folder
    storage_dir: str = Field(default="")  # relative path under storage/photo_batches/{id}/
    processed_folder_id: str = Field(default="")
    media_folder_id: str = Field(default="")
    ambience_folder_id: str = Field(default="")
    review_folder_id: str = Field(default="")
    # ready = local processing finished; every local result is final and
    # previewable. Google Drive upload to a manually-chosen destination is a
    # separate, explicit action from here (see drive_destination_service.py):
    # ready -> uploading -> completed, or -> upload_failed on error (local
    # output is untouched either way). "syncing_drive" is the OLD automatic-
    # mirror status, kept only for backward compatibility with batches
    # created before this flow existed.
    status: str = Field(default="pending")  # pending | processing | ready | uploading | upload_failed | syncing_drive | completed | failed
    current_stage: str = Field(default="")

    # Phase A workflow-axis columns (migration 004, restored verbatim from
    # the Main tree where they are already live in the shared production
    # DB). Additive, alongside `status` — not a replacement for it. No
    # current code reads/writes these yet; they exist so this worktree's
    # schema matches production and later phases (A1 onward) have them
    # available without a further migration.
    source_type: str = Field(default="drive")  # drive | local
    source_status: str = Field(default="PENDING")
    local_status: str = Field(default="CREATED")
    drive_status: str = Field(default="NOT_UPLOADED")
    workflow_version: int = Field(default=1)  # 1 = legacy sequential path (this worktree's only path today)

    total_photos: int = 0
    processed_photos: int = 0
    faces_detected: int = 0
    faces_recognized: int = 0
    faces_unknown: int = 0
    consented_faces: int = 0
    not_consented_faces: int = 0  # includes unrecognized faces — see blur rule
    blurred_faces: int = 0
    recognized_photos: int = 0  # photos containing >=1 identified participant (a photo count — not faces, not participants)
    ambience_photos: int = 0  # zero faces
    review_photos: int = 0  # LEGACY: written only by the removed Review workflow; always 0 for new batches, historical values kept
    failed_photos: int = 0  # unresolved: threw AFTER acceptance (past ORIGINAL/) — retriable via retry-unresolved
    rejected_photos: int = 0  # permanent: failed validation BEFORE reaching ORIGINAL/ — never retried, never blocks ready
    last_error: Optional[str] = None  # most recent per-photo failure message, for the Admin to see
    drive_failed_photos: int = 0  # processed fine locally, but the Drive upload failed/could not be verified
    drive_error: Optional[str] = None  # most recent Drive upload failure, shown separately from processing failures

    retention_days: int = 7  # 1-7, enforced at the API layer
    retention_start_at: datetime = Field(default_factory=datetime.now)
    delete_at: datetime = Field(default_factory=datetime.now)  # recomputed whenever retention_days changes

    # Phase G4 — how long processing actually took, persisted so the figure
    # survives a page refresh and a backend restart (the live ETA is
    # in-memory only). Both cover the MOST RECENT processing run: a retry
    # overwrites the start, so the reported duration is always the run the
    # admin just watched, never one inflated by idle time between attempts.
    processing_started_at: Optional[datetime] = None
    processing_finished_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=datetime.now)
    created_by: Optional[str] = Field(default=None, foreign_key="user.id")


class PhotoBatchPhoto(SQLModel, table=True):
    """One photo within a batch.

    Two INDEPENDENT outputs are tracked per photo:
      - local:  original_path / media_path, relative to STORAGE_PATH (same
                convention as Person.image_path / Upload.image_path). These
                back the in-app web preview and are always written first.
      - Drive:  media_drive_file_id + drive_upload_status, written second.

    Drive upload is deliberately allowed to fail without affecting the local
    result — the web preview must keep working when Drive is unavailable."""

    id: str = Field(default_factory=new_id, primary_key=True)
    batch_id: str = Field(foreign_key="photobatch.id", index=True)
    filename: str
    drive_file_id: str  # the SOURCE file in the photographer's folder
    original_path: str
    media_path: Optional[str] = None
    media_drive_file_id: Optional[str] = None  # verified MEDIA copy in the batch's Drive folder
    drive_upload_status: str = Field(default="pending")  # pending | uploaded | failed
    drive_error: Optional[str] = None
    classification: str = Field(default="pending")  # pending | ambience | sorted  ("review" appears only on legacy rows)
    faces_total: int = 0
    faces_matched: int = 0
    faces_unknown: int = 0
    processed_at: datetime = Field(default_factory=datetime.now)


class PhotoBatchIngestionIssue(SQLModel, table=True):
    """One row per source item rejected before it ever reached ORIGINAL/ —
    a corrupt/unreadable file, for example. Permanent and never retried
    (rejected_photos on PhotoBatch is the count; this is the per-item detail
    a bare counter can't carry — filename + reason, never overwritten the
    way a single last_error string would be)."""

    id: str = Field(default_factory=new_id, primary_key=True)
    batch_id: str = Field(foreign_key="photobatch.id", index=True)
    filename: str
    reason: str
    occurred_at: datetime = Field(default_factory=datetime.now)


class PhotoBatchParticipantFolder(SQLModel, table=True):
    """The Drive subfolder created for one participant within one batch's
    PROCESSED tree, and a running count of photos placed there — avoids
    re-deriving this from PhotoBatchFace joins on every page load."""

    id: str = Field(default_factory=new_id, primary_key=True)
    batch_id: str = Field(foreign_key="photobatch.id", index=True)
    person_id: str = Field(foreign_key="person.id", index=True)
    folder_id: str
    photo_count: int = 0


class PhotoBatchFace(SQLModel, table=True):
    """One detected face within a batch photo. consent_status_at_processing
    is a permanent snapshot — the whole point of "consent is final for
    processed photos" is that this never gets re-read or updated after the
    Media copy is generated, even if the participant's consent changes later."""

    id: str = Field(default_factory=new_id, primary_key=True)
    photo_id: str = Field(foreign_key="photobatchphoto.id", index=True)
    person_id: Optional[str] = Field(default=None, foreign_key="person.id", index=True)
    confidence: float
    bbox: str  # "x1,y1,x2,y2", original-image coordinates
    consent_status_at_processing: str = Field(default="unknown")  # consented | declined | unknown (no person match)
    blurred: bool = Field(default=False)
    # Phase E1 — persistent reviewer override, NOT client state:
    #   None  = no override, use the computed privacy decision
    #   True  = reviewer force-hid an otherwise-visible face
    #   False = reviewer explicitly confirmed this face visible
    # A reviewer can never set False on a face whose identity+consent computes
    # to mandatorily masked; that rule is enforced backend-side in Phase E2,
    # never by trusting this column alone.
    manual_mask: Optional[bool] = Field(default=None)

    # Phase D1 — the detector's original box, frozen on the FIRST geometry
    # edit (NULL = never edited, `bbox` is still the detector output). For a
    # face whose privacy decision requires a mask, every later edit must still
    # contain this region, so repeated edits can never shrink the mask below
    # what the detector found.
    detected_bbox: Optional[str] = Field(default=None)


class PhotoBatchReviewItem(SQLModel, table=True):
    """Phase E1 — the review QUEUE/workflow state for one photo.

    Distinct from the REVIEW output archive: archive membership is DERIVED
    from the mere existence of a row here (resolved or not), so resolving an
    item corrects the archived copy's content but never removes the photo
    from the archive. That is why no separate "ever required review" flag
    exists — it would be second state that could drift from this table.

    Only genuinely ambiguous cases belong here. An ordinary confident
    unknown face is visible, is NOT review work, and must not block ready.
    """

    id: str = Field(default_factory=new_id, primary_key=True)
    batch_id: str = Field(foreign_key="photobatch.id", index=True)
    photo_id: str = Field(foreign_key="photobatchphoto.id", index=True)
    # score_ambiguous | margin_ambiguous | tile_conflict | manual | artifact
    reason: str
    created_at: datetime = Field(default_factory=datetime.now)
    resolved_at: Optional[datetime] = Field(default=None)
    resolved_by: Optional[str] = Field(default=None, foreign_key="user.id")


class PhotoBatchReviewDecision(SQLModel, table=True):
    """Phase E1 — append-only audit log of every reviewer action.

    Append-only, matching ConsentRecord's existing philosophy: nothing here
    is ever updated or deleted. `outcome` distinguishes a decision that fully
    landed (DB state AND regenerated artifacts) from an attempt whose
    regeneration failed partway, so the audit trail can never claim an
    assignment that never reached the delivered files.

    consent_snapshot_at_decision locks the participant's effective consent at
    the moment of the decision, so a later regeneration of the same photo
    cannot silently produce a different privacy outcome because live consent
    changed in between.
    """

    id: str = Field(default_factory=new_id, primary_key=True)
    review_item_id: str = Field(foreign_key="photobatchreviewitem.id", index=True)
    photo_id: str = Field(foreign_key="photobatchphoto.id", index=True)
    face_id: Optional[str] = Field(default=None, foreign_key="photobatchface.id")
    action: str  # assign | confirm_unknown | hide | unhide | skip
    previous_person_id: Optional[str] = Field(default=None)
    new_person_id: Optional[str] = Field(default=None)
    previous_manual_mask: Optional[bool] = Field(default=None)
    new_manual_mask: Optional[bool] = Field(default=None)
    consent_snapshot_at_decision: Optional[str] = Field(default=None)
    reviewer_id: Optional[str] = Field(default=None, foreign_key="user.id")
    decided_at: datetime = Field(default_factory=datetime.now)
    outcome: str = Field(default="applied")  # applied | failed


# Phase F1 — EventIdentitySample (and EventIdentityCandidate) live in
# app/models/identity_models.py on a SEPARATE metadata, so create_all() never
# creates them; migration 010 does.


class SchemaMigration(SQLModel, table=True):
    """One row per migration script that has been applied to this database.
    The runner (app/migrations/runner.py) consults this to decide what is
    still pending."""

    id: str = Field(primary_key=True)  # the script's numeric prefix, e.g. "001"
    name: str
    applied_at: datetime = Field(default_factory=datetime.now)


class UpdateJob(SQLModel, table=True):
    """History of completed update runs, for the admin UI only.

    Deliberately NOT the source of truth for an update in progress: a rollback
    can restore a database snapshot taken before the update began, which would
    erase any in-flight rows written here. Live state lives in the runtime
    update_state.json outside the repo and outside the database; this table is
    written once, after a run reaches a terminal state."""

    id: str = Field(default_factory=new_id, primary_key=True)
    from_version: str
    to_version: str
    target_sha: str = Field(default="")
    previous_sha: str = Field(default="")
    status: str = Field(default="completed")  # completed | failed | rolled_back
    error: Optional[str] = None  # redacted before storage
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: datetime = Field(default_factory=datetime.now)


class CleanupLog(SQLModel, table=True):
    """Survives after its PhotoBatch is deleted — the batch's own name/id are
    stored as plain snapshot values here, not a live foreign key, since the
    whole point of this log is to be readable evidence of a deletion that
    already happened."""

    id: str = Field(default_factory=new_id, primary_key=True)
    batch_id: str  # snapshot, not a FK — the batch no longer exists after cleanup
    batch_label: str
    deleted_at: datetime = Field(default_factory=datetime.now)
    retention_days: int = 0
    photos_deleted: int = 0
    records_deleted: int = 0
    status: str = Field(default="success")  # success | partial | failed
    note: Optional[str] = None


class CameraNode(SQLModel, table=True):
    """A distributed browser-camera station's durable identity/config.

    This is NOT the same thing as CameraConfig: that table is one physical USB
    camera ffmpeg opens directly on THIS machine. A CameraNode is a browser
    tab, on any device, that owns its OWN camera via getUserMedia and sends
    recognition frames to a Local Agent (Windows) or here (mobile) - this
    machine never opens that camera at all.

    Presence (online/offline, last_seen, recording-right-now) is deliberately
    NOT stored here - that changes every few seconds and belongs in memory
    (services/node_registry.py), the same reasoning that keeps the ffmpeg
    preview stills out of the database. This table only holds what should
    survive a page refresh: what the station is called and configured to do.
    """

    id: str = Field(default_factory=new_id, primary_key=True)
    node_id: str = Field(unique=True, index=True)  # operator-chosen, e.g. "CAM-02", "MOBILE-01"
    display_name: str = Field(default="")
    activity_id: Optional[str] = Field(default=None, index=True)
    camera_label: str = Field(default="")
    mode: str = Field(default="always")           # tap | always — same meaning as the kiosk's KioskMode
    inference_mode: str = Field(default="local")  # local (Windows Local Agent) | central (mobile)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class NodeRecording(SQLModel, table=True):
    """One distributed node's browser-recorded clip, from START to STOP.

    Mirrors RecordingSession's shape on purpose (same reporting story: which
    camera, which activity, when), but is a separate table rather than reusing
    RecordingSession - that table's camera_id is a real foreign key into
    CameraConfig (the ffmpeg-attached-camera registry), which a browser node
    was never entered into and must not be forced to pretend to be."""

    id: str = Field(default_factory=new_id, primary_key=True)
    node_id: str = Field(index=True)
    node_name: str = Field(default="")
    activity_id: Optional[str] = Field(default=None, index=True)
    activity_name: str = Field(default="")
    file_path: str = Field(default="")   # relative, under storage/node_recordings/{node_id}/
    started_at: datetime = Field(default_factory=datetime.now)
    ended_at: Optional[datetime] = None
    status: str = Field(default="recording")  # recording | completed | failed
    error: Optional[str] = None
    size_bytes: int = 0
