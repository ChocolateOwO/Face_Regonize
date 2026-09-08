"""Event photo processing pipeline.

Reuses the exact existing face recognition stack — nothing about detection,
embedding, matching, or the threshold is reimplemented here:

  - app.face_recognition.engine.detect_faces()   (detection + embedding)
  - app.face_recognition.index.recognition_index  (vectorized matching)
  - app.services.settings_cache.get_threshold()   (same threshold as the kiosk)

Flow per photo: download -> decode -> detect_faces() -> recognition_index
.match_batch() -> classify (ambience / sorted+review / review) -> look up
each matched participant's CURRENT consent status -> blur only matched faces
with explicit declined consent. Unknown and pending faces remain visible.

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
import mimetypes
import json
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from sqlmodel import Session, select

from app.config import PHOTO_BATCHES_DIR, STORAGE_PATH
from app.database.db import engine
from app.services.event_photo_detection_service import detect_event_faces as detect_faces
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
_LOGO_POSITIONS = {"top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"}
_batch_lock = threading.Condition()
_active_batches: set[str] = set()
_cancelled_batches: set[str] = set()


def _cancelled(batch_id: str) -> bool:
    with _batch_lock:
        return batch_id in _cancelled_batches


def cancel_and_delete_batch(batch_id: str, timeout_seconds: float = 30) -> bool:
    """Cancel at the next safe boundary, then remove only owned local state."""
    with _batch_lock:
        _cancelled_batches.add(batch_id)
        _batch_lock.wait_for(lambda: batch_id not in _active_batches, timeout=timeout_seconds)
        if batch_id in _active_batches:
            return False
    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return True
        batch_dir = STORAGE_PATH / batch.storage_dir
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
        for photo in session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch_id)).all():
            for face in session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all(): session.delete(face)
            session.delete(photo)
        for folder in session.exec(select(PhotoBatchParticipantFolder).where(PhotoBatchParticipantFolder.batch_id == batch_id)).all(): session.delete(folder)
        session.delete(batch); session.commit()
    return True


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


def create_batch(session: Session, folder_url_or_id: str, retention_days: int, user_id: str, logo_png: bytes | None = None, logo_position: str = "bottom-right", logo_size: float = 0.15) -> PhotoBatch:
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
    if logo_png:
        logo_dir = batch_dir / "LOGO"
        logo_dir.mkdir(exist_ok=True)
        _write_bytes(logo_dir / "logo.png", logo_png)
        (logo_dir / "config.json").write_text(json.dumps({"position": logo_position, "size": logo_size}), encoding="utf-8")

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


def _load_logo_config(batch_dir: Path) -> tuple[np.ndarray, str, float] | None:
    try:
        config = json.loads((batch_dir / "LOGO" / "config.json").read_text(encoding="utf-8"))
        position, size = config["position"], float(config["size"])
        logo = cv2.imdecode(np.frombuffer((batch_dir / "LOGO" / "logo.png").read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if position not in _LOGO_POSITIONS or not 0.02 <= size <= 0.5 or logo is None or logo.ndim != 3 or logo.shape[2] not in (3, 4):
            return None
        return logo, position, size
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _apply_logo(media: np.ndarray, logo_config: tuple[np.ndarray, str, float] | None) -> None:
    if not logo_config:
        return
    logo, position, relative_size = logo_config
    height, width = media.shape[:2]
    target_width = max(1, int(width * relative_size))
    target_height = max(1, int(logo.shape[0] * target_width / logo.shape[1]))
    if target_width > width or target_height > height:
        return
    logo = cv2.resize(logo, (target_width, target_height), interpolation=cv2.INTER_AREA)
    pad = max(4, int(min(width, height) * 0.02))
    x = pad if position.endswith("left") else width - target_width - pad if position.endswith("right") else (width - target_width) // 2
    y = pad if position.startswith("top") else height - target_height - pad
    overlay = logo[:, :, :3].astype(np.float32)
    alpha = (logo[:, :, 3:4].astype(np.float32) / 255.0) if logo.shape[2] == 4 else np.ones((target_height, target_width, 1), dtype=np.float32)
    region = media[y:y + target_height, x:x + target_width].astype(np.float32)
    media[y:y + target_height, x:x + target_width] = (overlay * alpha + region * (1 - alpha)).astype(np.uint8)


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
    with _batch_lock: _active_batches.add(batch_id)
    try:
        _run_photo_batch(batch_id)
    finally:
        with _batch_lock:
            _active_batches.discard(batch_id); _batch_lock.notify_all()


def _run_photo_batch(batch_id: str) -> None:
    # An application update is mid-flight; its database backup has already
    # been taken, so anything written now could be silently discarded by a
    # rollback. The batch stays pending and can be started again afterwards.
    from app.services import maintenance

    if maintenance.is_active():
        logger.info("Photo batch %s deferred: an application update is in progress.", batch_id)
        return

    with Session(engine) as session:
        if _cancelled(batch_id): return
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return
        batch.status = "processing"
        batch.current_stage = "Listing Drive folder..."
        session.add(batch)
        session.commit()

        batch_dir = PHOTO_BATCHES_DIR / batch.id
        for sub in ("ORIGINAL", "SORTED", "AMBIENCE", "REVIEW", "MEDIA"):
            (batch_dir / sub).mkdir(parents=True, exist_ok=True)
        logo_config = _load_logo_config(batch_dir)

        # Whether Drive output is possible at all. is_connected() only reads a
        # stored refresh token out of the local database, so this costs no
        # network and can be answered before any photo is processed — the
        # common failure ("not connected") is reported immediately instead of
        # after a long local run. Creating the output FOLDERS is a Drive write
        # and therefore belongs to phase 2, not here.
        drive_enabled = is_connected()
        if not drive_enabled:
            batch.drive_error = "Google Drive is not connected — processed photos were saved locally only."
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

        # ---- PHASE 1: local processing -------------------------------------
        # Drive READS (list_image_files above, download_file below) are the
        # batch's input and stay. Drive WRITES do not happen here at all: an
        # upload used to sit between one photo's recognition and the next, so
        # network latency delayed face detection that needed nothing from it.
        for index, f in enumerate(files, start=1):
            if _cancelled(batch_id): return
            batch.current_stage = f"Processing photos — {index} of {len(files)}: {f.name}"
            session.add(batch)
            session.commit()

            try:
                file_bytes = download_file(f.id)
                img = storage_service.decode_image(file_bytes)
                if img is None:
                    raise RuntimeError("not a readable image")

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
                    media_img = img.copy()
                    _apply_logo(media_img, logo_config)
                    _write_bytes(STORAGE_PATH / media_rel, _encode(media_img, f.name) if logo_config else file_bytes)
                    photo.media_path = media_rel
                    batch.ambience_photos += 1
                    session.add(photo)
                    session.add(batch)
                    session.commit()
                    session.refresh(photo)

                    # No Drive write here — phase 2 syncs this photo later,
                    # reading the bytes back from AMBIENCE/ and MEDIA/.
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

                for person_id in matched_person_ids:
                    person = session.get(Person, person_id)
                    if not person:
                        continue
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

                # STAGE 1c — MEDIA: blur only a matched participant's explicit
                # denial. Consented, pending and unmatched faces stay visible.
                # Apply the decision independently to each face.
                media_img = img.copy()
                blurred_count = 0
                for row in face_rows:
                    if row.person_id and row.consent_status_at_processing == "declined":
                        bbox = tuple(float(v) for v in row.bbox.split(","))
                        _blur_region(media_img, bbox)
                        row.blurred = True
                        blurred_count += 1
                        session.add(row)

                _apply_logo(media_img, logo_config)

                media_bytes = _encode(media_img, f.name)
                media_rel = f"{batch.storage_dir}/MEDIA/{f.name}"
                _write_bytes(STORAGE_PATH / media_rel, media_bytes)
                photo.media_path = media_rel
                session.add(photo)
                session.commit()

                # No Drive write here either. Everything phase 2 needs to mirror
                # this photo is now on disk or in the database: the bytes under
                # ORIGINAL/ and MEDIA/, which participants it belongs to via its
                # PhotoBatchFace rows, and whether it is a review photo via
                # photo.classification.

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

        # Phase 1 is done: every local output exists and the web preview is
        # fully usable from here on, whatever Drive does next.
        local_note = "" if batch.failed_photos == 0 else f"{batch.failed_photos} photo(s) failed — see last error below."
        batch.current_stage = local_note
        session.add(batch)
        session.commit()

    # ---- PHASE 2: Drive sync -----------------------------------------------
    # Deliberately outside the session above: this opens its own session, and a
    # failure inside it must not be able to roll back any phase 1 work.
    try:
        if drive_enabled and not _cancelled(batch_id):
            _sync_batch_to_drive(batch_id)
    except Exception:  # noqa: BLE001 — never strand the batch mid-sync
        logger.exception("Photo batch %s: Drive sync raised unexpectedly", batch_id)
    finally:
        # Whatever happened above, the batch is finished as far as local
        # processing is concerned and must never be left showing "syncing".
        _finish_batch(batch_id)


def _finish_batch(batch_id: str) -> None:
    """Mark the batch completed and leave a stage message that tells the truth.

    Local processing having succeeded does NOT mean the whole batch succeeded:
    the message distinguishes a clean run from one where Drive was skipped or
    partly failed, using only fields that already exist.
    """
    with Session(engine) as session:
        if _cancelled(batch_id): return
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return
        notes: list[str] = []
        if batch.failed_photos:
            notes.append(f"{batch.failed_photos} photo(s) failed locally — see last error below.")
        if batch.drive_failed_photos:
            notes.append(f"Google Drive sync completed with errors — {batch.drive_failed_photos} photo(s) not uploaded.")
        batch.status = "completed"
        batch.current_stage = " ".join(notes)
        session.add(batch)
        session.commit()


def _sync_batch_to_drive(batch_id: str) -> None:
    """PHASE 2 — mirror an already locally-processed batch into Google Drive.

    File sync ONLY. No detection, embedding, matching or consent evaluation
    happens here; every one of those decisions was made in phase 1 and is read
    back from the database. Re-running any of it would be both wasteful and a
    way for two runs to disagree.

    Nothing local is ever deleted, rewritten or re-classified by this function.
    A batch whose Drive sync fails entirely still has all of its local output
    and still previews correctly — that is the whole point of the split.
    """
    with Session(engine) as session:
        if _cancelled(batch_id): return
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return

        batch.status = "syncing_drive"
        batch.current_stage = "Preparing Drive output folders..."
        session.add(batch)
        session.commit()

        # Output folders are created HERE, not at batch start: creating them is
        # a Drive write, and phase 1 must contain none.
        try:
            # drive.file scope: this app may only touch what it created, so the
            # output tree is rooted in the connected admin's own Drive rather
            # than inside the photographer's folder.
            root_id = create_root_folder(f"Reconize — {batch.label} — {datetime.now():%Y-%m-%d %H%M}")
            processed_id = get_or_create_subfolder(root_id, "PROCESSED")
            media_id = get_or_create_subfolder(processed_id, "MEDIA")
            ambience_id = get_or_create_subfolder(processed_id, "AMBIENCE")
            review_id = get_or_create_subfolder(processed_id, "REVIEW")
            batch.processed_folder_id = processed_id
            batch.media_folder_id = media_id
            batch.ambience_folder_id = ambience_id
            batch.review_folder_id = review_id
            session.add(batch)
            session.commit()
        except DriveOAuthError as e:
            # Connected at batch start but not usable now — an expired or
            # revoked authorisation is the usual cause, and saying so is more
            # useful than the raw API error.
            logger.warning("Photo batch %s: Drive authorisation unusable: %s", batch_id, e)
            batch.drive_error = (
                f"Google Drive authorisation failed ({e}) — processed photos were saved locally only. "
                "Reconnect Google Drive in Settings."
            )
            batch.status = "completed"
            batch.current_stage = ""
            session.add(batch)
            session.commit()
            return
        except Exception as e:  # noqa: BLE001 — Drive must never cost local results
            logger.exception("Photo batch %s: Drive output folders unavailable", batch_id)
            batch.drive_error = f"Drive output unavailable: {e}"
            batch.status = "completed"
            batch.current_stage = ""
            session.add(batch)
            session.commit()
            return

        # Only photos not yet mirrored. A row already marked "failed" is left
        # alone rather than silently retried, so its error stays visible.
        photos = session.exec(
            select(PhotoBatchPhoto).where(
                PhotoBatchPhoto.batch_id == batch_id,
                PhotoBatchPhoto.drive_upload_status == "pending",
            ).order_by(PhotoBatchPhoto.id)
        ).all()

        participant_folder_ids: dict[str, str] = {}  # person_id -> Drive folder id

        for index, photo in enumerate(photos, start=1):
            if _cancelled(batch_id): return
            batch.current_stage = f"Syncing to Google Drive — {index} of {len(photos)}: {photo.filename}"
            session.add(batch)
            session.commit()

            try:
                # Read the bytes back off disk rather than carrying every photo
                # of the batch in memory — a large batch would be gigabytes.
                original_abs = STORAGE_PATH / photo.original_path if photo.original_path else None
                media_abs = STORAGE_PATH / photo.media_path if photo.media_path else None
                if not original_abs or not original_abs.exists():
                    raise RuntimeError("local original is missing — nothing to sync")
                file_bytes = original_abs.read_bytes()
                media_bytes = media_abs.read_bytes() if media_abs and media_abs.exists() else file_bytes

                # Phase 1 knew the Drive-reported mime type, but storing it
                # would need a schema change; the extension is what Drive
                # itself derives it from anyway.
                mime_type = mimetypes.guess_type(photo.filename)[0] or "image/jpeg"

                # Rebuild the destination list from what phase 1 recorded.
                extra: list[str] = []
                if photo.classification == "ambience":
                    extra.append(ambience_id)
                else:
                    person_ids = [
                        r.person_id
                        for r in session.exec(
                            select(PhotoBatchFace).where(
                                PhotoBatchFace.photo_id == photo.id,
                                PhotoBatchFace.person_id.is_not(None),
                            )
                        ).all()
                    ]
                    seen: set[str] = set()
                    for person_id in person_ids:
                        if person_id in seen:
                            continue
                        seen.add(person_id)
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
                        extra.append(folder_id)
                    if photo.classification == "review":
                        extra.append(review_id)
                    session.commit()

                # Unchanged: uploads the source bytes once, then server-side
                # copies into the remaining folders.
                _drive_upload_photo(
                    session, batch, photo,
                    file_bytes=file_bytes, media_bytes=media_bytes, mime_type=mime_type,
                    media_folder_id=media_id, extra_folder_ids=extra,
                )
            except Exception as e:  # noqa: BLE001 — one photo must not stop the sync
                logger.exception("Photo batch %s: Drive sync failed for %s", batch_id, photo.filename)
                photo.drive_upload_status = "failed"
                photo.drive_error = str(e)
                batch.drive_failed_photos += 1
                batch.drive_error = f"{photo.filename}: {e}"
                session.add(photo)
                session.add(batch)
                session.commit()

        batch.status = "completed"
        batch.current_stage = ""
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
