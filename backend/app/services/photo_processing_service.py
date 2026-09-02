"""Event photo processing pipeline.

Reuses the exact existing face recognition stack — nothing about detection,
embedding, matching, or the threshold is reimplemented here:

  - app.face_recognition.engine.detect_faces()   (detection + embedding)
  - app.face_recognition.index.recognition_index  (vectorized matching)
  - app.services.settings_cache.get_threshold()   (same threshold as the kiosk)

Flow per photo: download -> decode -> detect_faces() -> recognition_index
.match_batch() -> classify (ambience / sorted+review / review) -> look up
each matched participant's CURRENT consent status -> blur non-consented/
unrecognized faces.

There are then TWO INDEPENDENT OUTPUTS, in this order:

  STAGE 1 (local, always runs) — write ORIGINAL/SORTED/AMBIENCE/REVIEW/MEDIA
    under storage/photo_batches/{batch_id}/ and commit the DB rows. This is
    what the in-app web preview reads, so the preview never depends on
    Google Drive being reachable, connected, or working.

  STAGE 2 (Google Drive, best-effort) — mirror the same tree into the
    photographer's Drive folder and VERIFY every uploaded file actually
    landed in the folder it was meant to. Any failure here is recorded
    (drive_upload_status/drive_error) and surfaced to the admin, but never
    raises past the local result and never deletes it.

Consent is resolved from the existing ConsentRecord table (same "latest
record wins" rule the PDPA feature uses) and then frozen into
PhotoBatchFace.consent_status_at_processing at that exact moment — nothing
ever re-reads live consent for an already-processed photo again, matching
"consent is final for processed photos."
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from sqlmodel import Session, select

from app.config import PHOTO_BATCHES_DIR, STORAGE_PATH
from app.database.db import engine
from app.face_recognition.engine import detect_faces
from app.face_recognition.index import recognition_index
from app.models.models import (
    CleanupLog,
    ConsentRecord,
    Person,
    PhotoBatch,
    PhotoBatchFace,
    PhotoBatchParticipantFolder,
    PhotoBatchPhoto,
)
from app.services import storage_service
from app.services import settings_cache
from app.services.google_drive_folder_service import (
    DriveFolderError,
    download_file,
    extract_folder_id,
    get_folder_name,
    list_image_files,
)
from app.services.google_drive_oauth_service import (
    DriveOAuthError,
    copy_file,
    create_root_folder,
    get_or_create_subfolder,
    is_connected,
    upload_bytes,
)

logger = logging.getLogger(__name__)

_INVALID_FS_CHARS = '<>:"/\\|?*'


def _safe_folder_name(participant_id: str, first_name: str, last_name: str) -> str:
    raw = f"{participant_id}_{first_name}_{last_name}".strip().rstrip("_")
    return "".join(c if c not in _INVALID_FS_CHARS else "_" for c in raw)


def _consent_status(session: Session, person_id: str) -> str:
    """"consented" | "declined" | "pending" — the participant's current
    (latest) consent choice, resolved once at processing time. Distinct from
    the PDPA feature's own internal helper (not imported from there) so this
    feature has no code dependency on pdpa.py's internals."""
    record = session.exec(
        select(ConsentRecord).where(ConsentRecord.person_id == person_id).order_by(ConsentRecord.recorded_at.desc()).limit(1)
    ).first()
    return record.choice if record else "pending"


def create_batch(session: Session, folder_url_or_id: str, retention_days: int, user_id: str) -> PhotoBatch:
    if not (1 <= retention_days <= 7):
        raise ValueError("retention_days must be between 1 and 7")
    folder_id = extract_folder_id(folder_url_or_id)
    label = get_folder_name(folder_id) or folder_id  # cosmetic — never fail a batch over the label

    batch = PhotoBatch(
        label=label,
        drive_folder_id=folder_id,
        storage_dir="",  # filled in below once we have the id
        retention_days=retention_days,
        retention_start_at=datetime.now(),
        delete_at=datetime.now() + timedelta(days=retention_days),
        created_by=user_id,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)

    batch.storage_dir = f"photo_batches/{batch.id}"
    session.add(batch)
    session.commit()

    batch_dir = PHOTO_BATCHES_DIR / batch.id
    for sub in ("ORIGINAL", "SORTED", "AMBIENCE", "REVIEW", "MEDIA"):
        (batch_dir / sub).mkdir(parents=True, exist_ok=True)

    return batch


def _encode(img: np.ndarray, filename: str) -> bytes:
    ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".jpg"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise RuntimeError(f"cv2.imencode failed for {filename}")
    return buf.tobytes()


def _write_bytes(path: Path, data: bytes) -> None:
    """cv2.imwrite() silently fails (returns False, no exception) on Windows
    when the path contains non-ASCII characters, because it goes through a
    C-level fopen() that doesn't understand Unicode paths there — this
    project's own storage path does (Thai characters). Everything written
    here therefore goes through Python's own file handle, which has no such
    restriction; images are encoded to bytes first via _encode()."""
    path.write_bytes(data)


def _blur_region(img: np.ndarray, bbox: tuple[float, float, float, float]) -> None:
    """Blurs in place. Expands the tight detection bbox by a margin so hair/
    ears/forehead near the face are covered too, and uses a kernel large
    enough relative to the face that the result isn't reversible by simple
    sharpening."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    pad_x, pad_y = bw * 0.25, bh * 0.35
    ex1 = max(0, int(x1 - pad_x))
    ey1 = max(0, int(y1 - pad_y))
    ex2 = min(w, int(x2 + pad_x))
    ey2 = min(h, int(y2 + pad_y))
    if ex2 <= ex1 or ey2 <= ey1:
        return
    region = img[ey1:ey2, ex1:ex2]
    k = max(31, (min(region.shape[0], region.shape[1]) // 2) | 1)  # odd kernel, scales with face size
    blurred = cv2.GaussianBlur(region, (k, k), 0)
    blurred = cv2.GaussianBlur(blurred, (k, k), 0)  # two passes — stronger, harder to reverse
    img[ey1:ey2, ex1:ex2] = blurred


def _drive_upload_photo(
    session: Session,
    batch: PhotoBatch,
    photo: PhotoBatchPhoto,
    *,
    file_bytes: bytes,
    media_bytes: bytes,
    mime_type: str,
    media_folder_id: str,
    extra_folder_ids: list[str],
) -> None:
    """STAGE 2: mirror one already-locally-saved photo into Google Drive.

    Every upload/copy is verified by google_drive_oauth_service (the file id
    is read back and its parent folder confirmed) — an upload is only ever
    recorded as successful once that check passes. Any failure is caught
    here, recorded on the photo and the batch, and never propagated: the
    local result and the web preview must survive a broken Drive.
    """
    try:
        # The unmodified original is uploaded once, then server-side-copied
        # into every other folder it belongs in — the same bytes never leave
        # this app more than once.
        original_copy_id: str | None = None
        for folder_id in extra_folder_ids:
            if not folder_id:
                continue
            if original_copy_id is None:
                original_copy_id = upload_bytes(folder_id, photo.filename, file_bytes, mime_type)
            else:
                copy_file(original_copy_id, folder_id, photo.filename)

        if media_folder_id:
            photo.media_drive_file_id = upload_bytes(media_folder_id, photo.filename, media_bytes, mime_type)

        photo.drive_upload_status = "uploaded"
        photo.drive_error = None
    except Exception as e:  # noqa: BLE001 — Drive must never break the local result
        logger.exception("Photo batch %s: Drive upload failed for %s", batch.id, photo.filename)
        photo.drive_upload_status = "failed"
        photo.drive_error = str(e)
        batch.drive_failed_photos += 1
        batch.drive_error = f"{photo.filename}: {e}"
    finally:
        session.add(photo)
        session.add(batch)
        session.commit()


def run_photo_batch(batch_id: str) -> None:
    # An application update is mid-flight; its database backup has already
    # been taken, so anything written now could be silently discarded by a
    # rollback. The batch stays pending and can be started again afterwards.
    from app.services import maintenance

    if maintenance.is_active():
        logger.info("Photo batch %s deferred: an application update is in progress.", batch_id)
        return

    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return
        batch.status = "processing"
        batch.current_stage = "Preparing Drive output folders..."
        session.add(batch)
        session.commit()

        batch_dir = PHOTO_BATCHES_DIR / batch.id
        for sub in ("ORIGINAL", "SORTED", "AMBIENCE", "REVIEW", "MEDIA"):
            (batch_dir / sub).mkdir(parents=True, exist_ok=True)

        # Drive output is OPTIONAL. If it isn't connected or the folder tree
        # can't be created, processing still runs and the web preview still
        # gets populated — only the Drive mirror is skipped.
        processed_id = media_id = ambience_id = review_id = ""
        drive_enabled = is_connected()
        if drive_enabled:
            try:
                # drive.file scope: this app may only touch what it created,
                # so the output tree is rooted in the connected admin's own
                # Drive rather than inside the photographer's folder.
                root_id = create_root_folder(f"Reconize — {batch.label} — {datetime.now():%Y-%m-%d %H%M}")
                processed_id = get_or_create_subfolder(root_id, "PROCESSED")
                media_id = get_or_create_subfolder(processed_id, "MEDIA")
                ambience_id = get_or_create_subfolder(processed_id, "AMBIENCE")
                review_id = get_or_create_subfolder(processed_id, "REVIEW")
                batch.processed_folder_id = processed_id
                batch.media_folder_id = media_id
                batch.ambience_folder_id = ambience_id
                batch.review_folder_id = review_id
            except (DriveFolderError, DriveOAuthError) as e:
                logger.exception("Photo batch %s: Drive output folders unavailable", batch.id)
                drive_enabled = False
                batch.drive_error = f"Drive output unavailable: {e}"
        else:
            batch.drive_error = "Google Drive is not connected — processed photos were saved locally only."

        batch.current_stage = "Listing Drive folder..."
        session.add(batch)
        session.commit()

        threshold = settings_cache.get_threshold()

        try:
            files = list_image_files(batch.drive_folder_id)
        except DriveFolderError as e:
            batch.status = "failed"
            batch.current_stage = str(e)
            session.add(batch)
            session.commit()
            return

        batch.total_photos = len(files)
        session.add(batch)
        session.commit()

        participant_folder_ids: dict[str, str] = {}  # person_id -> Drive folder id, cached for this run

        for f in files:
            batch.current_stage = f"Processing {f.name}..."
            session.add(batch)
            session.commit()

            try:
                file_bytes = download_file(f.id)
                img = storage_service.decode_image(file_bytes)
                if img is None:
                    raise RuntimeError("not a readable image")

                mime_type = f.mime_type or "image/jpeg"

                # STAGE 1a — the untouched original always lands locally first.
                original_rel = f"{batch.storage_dir}/ORIGINAL/{f.name}"
                _write_bytes(STORAGE_PATH / original_rel, file_bytes)

                faces = detect_faces(img)
                photo = PhotoBatchPhoto(
                    batch_id=batch.id,
                    filename=f.name,
                    drive_file_id=f.id,
                    original_path=original_rel,
                    faces_total=len(faces),
                )

                if not faces:
                    # AMBIENCE: no faces, so nothing to blur — the Media copy
                    # is just the original.
                    photo.classification = "ambience"
                    ambience_rel = f"{batch.storage_dir}/AMBIENCE/{f.name}"
                    media_rel = f"{batch.storage_dir}/MEDIA/{f.name}"
                    _write_bytes(STORAGE_PATH / ambience_rel, file_bytes)
                    _write_bytes(STORAGE_PATH / media_rel, file_bytes)
                    photo.media_path = media_rel
                    batch.ambience_photos += 1
                    session.add(photo)
                    session.add(batch)
                    session.commit()
                    session.refresh(photo)

                    if drive_enabled:
                        _drive_upload_photo(
                            session, batch, photo,
                            file_bytes=file_bytes, media_bytes=file_bytes, mime_type=mime_type,
                            media_folder_id=media_id, extra_folder_ids=[ambience_id],
                        )

                    batch.processed_photos += 1
                    session.add(batch)
                    session.commit()
                    continue

                embeddings = np.stack([face.embedding for face in faces])
                matches = recognition_index.match_batch(embeddings, threshold)

                has_unknown = False
                matched_person_ids: set[str] = set()
                face_rows: list[PhotoBatchFace] = []
                for face, (person_id, _first, _full, _pid, score) in zip(faces, matches):
                    if person_id:
                        matched_person_ids.add(person_id)
                        consent = _consent_status(session, person_id)
                    else:
                        has_unknown = True
                        consent = "no_match"
                    face_rows.append(
                        PhotoBatchFace(
                            photo_id="",  # filled in after photo.id exists
                            person_id=person_id,
                            confidence=score,
                            bbox=",".join(f"{v:.1f}" for v in face.bbox),
                            consent_status_at_processing=consent,
                        )
                    )

                photo.faces_matched = len(matched_person_ids)
                photo.faces_unknown = sum(1 for r in face_rows if r.consent_status_at_processing == "no_match")
                photo.classification = "review" if has_unknown else "sorted"
                session.add(photo)
                session.commit()
                session.refresh(photo)

                for row in face_rows:
                    row.photo_id = photo.id
                    session.add(row)
                session.commit()

                # STAGE 1b — local copies: REVIEW (if any unknown face) and one
                # per recognized participant under SORTED/. A photo containing
                # several recognized people is copied into each of their
                # folders; the ORIGINAL is never modified.
                if has_unknown:
                    _write_bytes(STORAGE_PATH / f"{batch.storage_dir}/REVIEW/{f.name}", file_bytes)
                    batch.review_photos += 1
                else:
                    batch.recognized_photos += 1

                sorted_person_ids: list[str] = []
                for person_id in matched_person_ids:
                    person = session.get(Person, person_id)
                    if not person:
                        continue
                    sorted_person_ids.append(person_id)
                    folder_name = _safe_folder_name(person.participant_id, person.first_name, person.last_name)
                    dest_dir = batch_dir / "SORTED" / folder_name
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    _write_bytes(dest_dir / f.name, file_bytes)

                    pf = session.exec(
                        select(PhotoBatchParticipantFolder).where(
                            PhotoBatchParticipantFolder.batch_id == batch.id,
                            PhotoBatchParticipantFolder.person_id == person_id,
                        )
                    ).first()
                    if not pf:
                        pf = PhotoBatchParticipantFolder(batch_id=batch.id, person_id=person_id, folder_id="")
                    pf.photo_count += 1
                    session.add(pf)
                session.commit()

                # STAGE 1c — MEDIA: blur every face that is not explicitly
                # consented (declined, pending, or unrecognized). Consented
                # faces stay visible; the photo is never blurred as a whole.
                media_img = img.copy()
                blurred_count = 0
                for row in face_rows:
                    if row.consent_status_at_processing != "consented":
                        bbox = tuple(float(v) for v in row.bbox.split(","))
                        _blur_region(media_img, bbox)
                        row.blurred = True
                        blurred_count += 1
                        session.add(row)

                media_bytes = _encode(media_img, f.name)
                media_rel = f"{batch.storage_dir}/MEDIA/{f.name}"
                _write_bytes(STORAGE_PATH / media_rel, media_bytes)
                photo.media_path = media_rel
                session.add(photo)
                session.commit()

                # STAGE 2 — Drive mirror, best-effort and fully isolated from
                # everything above.
                if drive_enabled:
                    participant_targets: list[str] = []
                    try:
                        for person_id in sorted_person_ids:
                            folder_id = participant_folder_ids.get(person_id)
                            if not folder_id:
                                person = session.get(Person, person_id)
                                if not person:
                                    continue
                                folder_id = get_or_create_subfolder(
                                    processed_id,
                                    _safe_folder_name(person.participant_id, person.first_name, person.last_name),
                                )
                                participant_folder_ids[person_id] = folder_id
                                pf = session.exec(
                                    select(PhotoBatchParticipantFolder).where(
                                        PhotoBatchParticipantFolder.batch_id == batch.id,
                                        PhotoBatchParticipantFolder.person_id == person_id,
                                    )
                                ).first()
                                if pf:
                                    pf.folder_id = folder_id
                                    session.add(pf)
                            participant_targets.append(folder_id)
                        session.commit()
                    except (DriveFolderError, DriveOAuthError) as e:
                        logger.exception("Photo batch %s: participant folder creation failed for %s", batch.id, f.name)
                        participant_targets = []
                        batch.drive_error = f"{f.name}: {e}"

                    extra = list(participant_targets)
                    if has_unknown:
                        extra.append(review_id)
                    _drive_upload_photo(
                        session, batch, photo,
                        file_bytes=file_bytes, media_bytes=media_bytes, mime_type=mime_type,
                        media_folder_id=media_id, extra_folder_ids=extra,
                    )

                batch.faces_detected += len(faces)
                batch.faces_recognized += len(matched_person_ids)
                batch.faces_unknown += photo.faces_unknown
                batch.blurred_faces += blurred_count
                consented_count = sum(1 for r in face_rows if r.consent_status_at_processing == "consented")
                batch.consented_faces += consented_count
                batch.not_consented_faces += len(face_rows) - consented_count
                batch.processed_photos += 1
                session.add(batch)
                session.commit()

            except Exception as e:  # noqa: BLE001 — one bad photo must not kill the whole batch
                logger.exception("Photo batch %s: failed to process %s", batch.id, f.name)
                batch.processed_photos += 1
                batch.failed_photos += 1
                batch.last_error = f"{f.name}: {e}"
                batch.current_stage = f"Skipped {f.name}: {e}"
                session.add(batch)
                session.commit()

        batch.status = "completed"
        batch.current_stage = "" if batch.failed_photos == 0 else f"{batch.failed_photos} photo(s) failed — see last error below."
        session.add(batch)
        session.commit()


def change_retention(session: Session, batch: PhotoBatch, new_retention_days: int) -> PhotoBatch:
    if not (1 <= new_retention_days <= 7):
        raise ValueError("retention_days must be between 1 and 7")
    batch.retention_days = new_retention_days
    batch.delete_at = batch.retention_start_at + timedelta(days=new_retention_days)
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def run_retention_cleanup() -> int:
    """Deletes every PhotoBatch whose delete_at has passed — its local
    storage directory (the web-preview copies) and its PhotoBatchPhoto/
    PhotoBatchFace/PhotoBatchParticipantFolder rows, plus the PhotoBatch row
    itself. Never touches Person, Attendance, FaceDetection, Upload, or any
    other batch, and never touches the batch's Google Drive PROCESSED folder
    — that stays in the photographer's Drive as the durable deliverable;
    only this app's own local copies and records expire. Writes one
    CleanupLog entry per deleted batch. Returns the number cleaned up."""
    # Never delete during an update window: the deletion would not be captured
    # by the pre-update database backup, so a rollback would resurrect the DB
    # rows while the files were already gone.
    from app.services import maintenance

    if maintenance.is_active():
        return 0

    cleaned = 0
    with Session(engine) as session:
        now = datetime.now()
        expired = session.exec(select(PhotoBatch).where(PhotoBatch.delete_at <= now)).all()
        for batch in expired:
            status = "success"
            note = "Local copies and processing records deleted — the batch's Google Drive PROCESSED folder was not touched."
            try:
                if batch.storage_dir:
                    batch_dir = STORAGE_PATH / batch.storage_dir
                    if batch_dir.exists():
                        shutil.rmtree(batch_dir)
            except Exception as e:  # noqa: BLE001 — still remove the DB records even if file cleanup partially fails
                status = "partial"
                note = f"File cleanup issue: {e}"

            photos = session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch.id)).all()
            photo_count = len(photos)
            face_count = 0
            for photo in photos:
                faces = session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all()
                face_count += len(faces)
                for face in faces:
                    session.delete(face)
                session.delete(photo)

            folders = session.exec(
                select(PhotoBatchParticipantFolder).where(PhotoBatchParticipantFolder.batch_id == batch.id)
            ).all()
            for folder in folders:
                session.delete(folder)

            records_deleted = photo_count + face_count + len(folders) + 1  # +1 for the batch row itself
            session.add(CleanupLog(
                batch_id=batch.id,
                batch_label=batch.label,
                retention_days=batch.retention_days,
                photos_deleted=photo_count,
                records_deleted=records_deleted,
                status=status,
                note=note,
            ))
            session.delete(batch)
            session.commit()
            cleaned += 1
    return cleaned
