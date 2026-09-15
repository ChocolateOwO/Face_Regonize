"""Phase D1 — Event Photo face bounding-box geometry edits.

This is a mask-geometry correction tool, NOT a Review workspace: it changes a
face's box only — never an identity, never a consent value, never a visibility
decision.

Rules this module owns:

  * Geometry is always stored in ORIGINAL-image pixel coordinates. Callers send
    boxes in that space; nothing here ever sees a display coordinate.
  * Fail-closed privacy: for a face whose privacy decision requires a mask
    (known + declined), the first edit freezes the detector's box into
    `detected_bbox`, and every edit must still CONTAIN that region — enlarge or
    extend only. A declined participant's mask can never be shrunk or moved away.
  * The edit is refused unless the batch is locally finished and nothing is
    live, and while any ZIP build or Drive upload holds the batch.
  * The ORM row is not modified until every derived file has been replaced:
    rendering uses transient face snapshots, and the row changes only inside
    `regenerate_photo`'s commit callback (under no_autoflush). Any failure
    leaves the database and the files at their previous state.
  * ORIGINAL bytes are only ever READ, to re-render at full resolution. No
    payload produced here references ORIGINAL.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image
from sqlmodel import Session, select

from app.models.models import Person, PhotoBatch, PhotoBatchFace, PhotoBatchPhoto
from app.services import batch_edit_lock
from app.services import photo_regeneration_service as prs

EDITABLE_STATUSES = frozenset({"ready", "completed", "upload_failed"})
MIN_BOX_PX = 8
# The detector writes boxes rounded to 0.1 px; containment is judged with a
# tolerance a little above that so a box drawn exactly on the detected edge is
# accepted, but nothing meaningful can slip inside it.
CONTAIN_TOLERANCE_PX = 0.5

Box = tuple[float, float, float, float]


class GeometryError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def parse_bbox(text: str) -> Box:
    x1, y1, x2, y2 = (float(v) for v in text.split(","))
    return x1, y1, x2, y2


def format_bbox(box: Box) -> str:
    """Same text format the detector writes."""
    return ",".join(f"{v:.1f}" for v in box)


def original_size(storage_path: Path, photo: PhotoBatchPhoto) -> tuple[int, int]:
    """Width/height of the ORIGINAL, read from the image header only."""
    with Image.open(storage_path / photo.original_path) as im:
        return im.size


def validate_box(box: Box, width: int, height: int) -> None:
    if len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box):
        raise GeometryError("A box needs four finite numbers: x1, y1, x2, y2.")
    x1, y1, x2, y2 = box
    if not (x1 < x2 and y1 < y2):
        raise GeometryError("A box must have x1 < x2 and y1 < y2.")
    if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
        raise GeometryError(f"The box must lie inside the image ({width} x {height}).")
    if (x2 - x1) < MIN_BOX_PX or (y2 - y1) < MIN_BOX_PX:
        raise GeometryError(f"A box must be at least {MIN_BOX_PX} x {MIN_BOX_PX} pixels.")


def contains(outer: Box, inner: Box, tol: float = CONTAIN_TOLERANCE_PX) -> bool:
    return (outer[0] <= inner[0] + tol and outer[1] <= inner[1] + tol
            and outer[2] >= inner[2] - tol and outer[3] >= inner[3] - tol)


def edit_block_reason(batch: PhotoBatch) -> str | None:
    if batch.status not in EDITABLE_STATUSES:
        return f"Boxes can only be adjusted once local processing has finished (status: {batch.status})."
    from app.api.photo_batches import batch_is_live  # deferred: the API module imports this one

    if batch_is_live(batch):
        return "Processing or a Google Drive upload is running for this batch — try again when it finishes."
    return None


def _snapshot(face: PhotoBatchFace, bbox_text: str | None = None) -> PhotoBatchFace:
    """A transient copy (never added to the session) used only for rendering."""
    return PhotoBatchFace(
        id=face.id, photo_id=face.photo_id, person_id=face.person_id, confidence=face.confidence,
        bbox=bbox_text if bbox_text is not None else face.bbox,
        consent_status_at_processing=face.consent_status_at_processing,
        blurred=face.blurred, manual_mask=face.manual_mask, detected_bbox=face.detected_bbox,
    )


def _identity(face: PhotoBatchFace, people: dict[str, Person]) -> tuple[str, dict | None]:
    """From the STORED relation only (face.person_id) — recognition is never
    re-run. A minimal summary: nothing beyond what the side panel shows."""
    if not face.person_id:
        return "unknown", None
    person = people.get(face.person_id)
    if person is None:
        return "deleted", None
    name = f"{person.first_name} {person.last_name}".strip()
    return "matched", {"person_id": person.id, "participant_id": person.participant_id, "name": name}


def faces_payload(session: Session, batch: PhotoBatch, photo: PhotoBatchPhoto, storage_path: Path) -> dict:
    faces = session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all()
    # Numbered top-to-bottom, left-to-right: a small, non-identifying label.
    faces = sorted(faces, key=lambda f: (parse_bbox(f.bbox)[1], parse_bbox(f.bbox)[0], f.id))
    ids = sorted({f.person_id for f in faces if f.person_id})
    people = {p.id: p for p in session.exec(select(Person).where(Person.id.in_(ids))).all()} if ids else {}
    width, height = original_size(storage_path, photo)
    reason = edit_block_reason(batch)
    return {
        "photo_id": photo.id,
        "filename": photo.filename,
        # The editor displays the privacy-rendered MEDIA copy — never ORIGINAL.
        "media_path": photo.media_path,
        "media_version": _media_version(storage_path, photo),
        "width": width,
        "height": height,
        "min_box": MIN_BOX_PX,
        "editable": reason is None,
        "blocked_reason": reason,
        "faces": [
            {
                "id": f.id,
                "bbox": list(parse_bbox(f.bbox)),
                "detected_bbox": list(parse_bbox(f.detected_bbox)) if f.detected_bbox else None,
                "mask_required": prs.privacy_requires_mask(f),
                "masked": prs.face_is_masked(f),
                # Follow-up Task 3 — information display only (no reassignment).
                "number": number,
                "identity": identity,
                "participant": participant,
                # The processing-time snapshot that decided masking — never live PDPA.
                "consent_status_at_processing": f.consent_status_at_processing,
                "match_confidence": (round(float(f.confidence), 4)
                                     if identity != "unknown" and f.confidence is not None else None),
            }
            for number, f in enumerate(faces, start=1)
            for identity, participant in [_identity(f, people)]
        ],
    }


def _media_version(storage_path: Path, photo: PhotoBatchPhoto) -> str | None:
    if not photo.media_path:
        return None
    try:
        st = (storage_path / photo.media_path).stat()
        return f"{st.st_mtime_ns}-{st.st_size}"
    except OSError:
        return None


def update_face_bbox(session: Session, batch: PhotoBatch, photo: PhotoBatchPhoto, face_id: str, new_box: Box,
                     *, storage_path: Path, photo_batches_dir: Path) -> PhotoBatchFace:
    reason = edit_block_reason(batch)
    if reason:
        raise GeometryError(reason, 409)
    if not photo.media_path:
        raise GeometryError("This photo has not finished processing.", 409)
    if not batch_edit_lock.reserve_edit(batch.id):
        raise GeometryError("A download, a Google Drive upload or another edit is using this batch right now.", 409)
    try:
        faces = session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all()
        target = next((f for f in faces if f.id == face_id), None)
        if target is None:
            raise GeometryError("That face does not belong to this photo.", 404)

        new_box = tuple(float(v) for v in new_box)
        width, height = original_size(storage_path, photo)
        validate_box(new_box, width, height)

        detected = parse_bbox(target.detected_bbox or target.bbox)
        if prs.privacy_requires_mask(target) and not contains(new_box, detected):
            raise GeometryError(
                "This face is masked for privacy. Its box can be enlarged or extended, "
                "but it must still cover the originally detected face.", 400)

        new_text = format_bbox(new_box)
        snapshots = [_snapshot(f, new_text if f.id == face_id else None) for f in faces]

        def commit() -> None:
            with session.no_autoflush:
                if target.detected_bbox is None:
                    target.detected_bbox = target.bbox  # frozen on the first edit, never changed again
                target.bbox = new_text
                session.add(target)
            session.commit()

        prs.regenerate_photo(session, batch, photo, snapshots, storage_path=storage_path,
                             photo_batches_dir=photo_batches_dir, commit=commit)
        session.refresh(target)
        return target
    finally:
        batch_edit_lock.release_edit(batch.id)
