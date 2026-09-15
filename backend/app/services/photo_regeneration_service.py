"""Targeted single-photo regeneration.

Re-render ONE photo from its immutable ORIGINAL and swap the corrected bytes
into every derived destination, with a compensating rollback so the delivered
artifacts are never left in a mixed state.

SQLite and the filesystem cannot share a transaction, so the ordering here is
deliberate, not incidental:

    1. render every affected destination to TEMP paths (nothing live touched)
    2. snapshot the live files that are about to be replaced or removed
    3. replace/remove live files one at a time
       -> on ANY failure: restore every already-changed destination and
          discard the temps
    4. only once every destination landed, run the caller's DB commit
       -> if THAT commit fails: roll the DB back and restore ALL files from
          the step-2 snapshot, so the artifacts and the DB never disagree

Multiple independent ``os.replace()`` calls are NOT atomic as a group — the
filesystem offers no such primitive — so the compensating rollback above is
what provides the guarantee, not an assumed atomicity. The outcome is always
all-old or all-new.

This module is deliberately neutral: it knows about photos, faces, consent and
files, and nothing about why a regeneration was requested.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np
from sqlmodel import Session

from app.models.models import Person, PhotoBatch, PhotoBatchFace, PhotoBatchPhoto

logger = logging.getLogger(__name__)

TEMP_SUFFIX = ".regen-tmp"


class RegenerationError(ValueError):
    """A regeneration that must be refused, or that failed and was rolled back."""


# --------------------------------------------------------------------------
# The hard privacy rule
# --------------------------------------------------------------------------

def privacy_requires_mask(face: PhotoBatchFace) -> bool:
    """A face matched to a participant who explicitly DECLINED is always
    masked. This is the same predicate the automatic pipeline uses, re-evaluated
    here rather than trusting anything a client sent.

    Consent comes from the face's stored snapshot, never from a fresh read of
    the live PDPA record, so two regenerations of the same photo can never
    render different privacy outcomes just because time passed.
    """
    return bool(face.person_id) and face.consent_status_at_processing == "declined"


def face_is_masked(face: PhotoBatchFace) -> bool:
    """Whether this face renders concealed. A declined participant is masked
    unconditionally — no stored override can reveal them."""
    if privacy_requires_mask(face):
        return True
    return face.manual_mask is True


# --------------------------------------------------------------------------
# Rendering (reuses D0's renderer — never re-detects, never re-matches)
# --------------------------------------------------------------------------

def render_photo_bytes(photo: PhotoBatchPhoto, faces: list[PhotoBatchFace], original_bytes: bytes,
                       logo_config, mask_config) -> bytes:
    """Re-render ONE photo from its immutable ORIGINAL using the geometry and
    identity already stored on its faces. Detection and matching are NOT re-run:
    what changed is geometry or presentation, not the pixels to analyse."""
    from app.services.photo_processing_service import _apply_logo, _apply_privacy_mask, _encode

    img = cv2.imdecode(np.frombuffer(original_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RegenerationError(f"ORIGINAL for {photo.filename} could not be decoded.")
    media_img = img.copy()
    for face in faces:
        if face_is_masked(face):
            bbox = tuple(float(v) for v in face.bbox.split(","))
            _apply_privacy_mask(media_img, bbox, mask_config)
    _apply_logo(media_img, logo_config)
    return _encode(media_img, photo.filename)


def sorted_dir(photo_batches_dir: Path, batch: PhotoBatch, person: Person) -> Path:
    from app.services.photo_processing_service import _safe_folder_name

    folder = _safe_folder_name(person.participant_id, person.first_name, person.last_name)
    return photo_batches_dir / batch.id / "SORTED" / folder


def destination_paths(session: Session, batch: PhotoBatch, photo: PhotoBatchPhoto,
                      faces: list[PhotoBatchFace], storage_path: Path, photo_batches_dir: Path,
                      person_ids: set[str]) -> list[Path]:
    """Every derived file this photo should hold AFTER the regeneration.

    MEDIA always; AMBIENCE when the photo has no faces; one SORTED copy per
    matched participant. No ``REVIEW/`` output is produced — that workflow was
    removed from the product.

    A legacy batch may still have a ``REVIEW/`` file on disk from before the
    removal. It is included ONLY when it already exists, never created: leaving
    a stale copy behind would keep an out-of-date render (potentially with a
    face this regeneration is masking) sitting in the delivered tree.
    """
    root = storage_path / batch.storage_dir
    paths = [root / "MEDIA" / photo.filename]

    if not faces:
        paths.append(root / "AMBIENCE" / photo.filename)

    legacy_review = root / "REVIEW" / photo.filename
    if legacy_review.is_file():
        paths.append(legacy_review)

    for person_id in sorted(person_ids):
        person = session.get(Person, person_id)
        if person:
            paths.append(sorted_dir(photo_batches_dir, batch, person) / photo.filename)
    return paths


# --------------------------------------------------------------------------
# File-level primitives (patched by the failure-injection tests)
# --------------------------------------------------------------------------

def _atomic_replace(temp: Path, dest: Path) -> None:
    from app.services.fs_util import replace_with_retry

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Retried only on PermissionError: on Windows a browser download of the
    # live MEDIA/thumbnail briefly blocks replacing it. A persistent failure
    # still raises and the compensating rollback runs unchanged.
    replace_with_retry(temp, dest)


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


@dataclass
class _FileGuard:
    """Remembers what every touched path looked like BEFORE the operation, so
    any partial change can be undone exactly. ``None`` means the file did not
    exist, which restore must reproduce by deleting it again."""

    snapshot: dict[Path, bytes | None] = field(default_factory=dict)
    changed: list[Path] = field(default_factory=list)
    temps: list[Path] = field(default_factory=list)

    def remember(self, path: Path) -> None:
        if path in self.snapshot:
            return
        self.snapshot[path] = path.read_bytes() if path.is_file() else None

    def restore_all(self) -> None:
        """Put every changed path back exactly as it was. Best-effort per path:
        one failure must not abandon the rest."""
        for path in self.changed:
            previous = self.snapshot.get(path)
            try:
                if previous is None:
                    _remove_file(path)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(previous)
            except OSError:
                logger.exception("Regeneration rollback could not restore %s", path)

    def discard_temps(self) -> None:
        for temp in self.temps:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                logger.exception("Regeneration rollback could not remove temp %s", temp)


# --------------------------------------------------------------------------
# The regeneration flow
# --------------------------------------------------------------------------

def regenerate_photo(
    session: Session,
    batch: PhotoBatch,
    photo: PhotoBatchPhoto,
    faces: list[PhotoBatchFace],
    *,
    storage_path: Path,
    photo_batches_dir: Path,
    stale_person_ids: Iterable[str] = (),
    commit: Callable[[], None] | None = None,
) -> bytes:
    """Re-render `photo` and swap the result into every derived destination.

    `faces` must already carry the intended post-change state (the caller
    mutates them in memory; nothing is committed until every file has landed).
    `stale_person_ids` names participants whose SORTED copy of this photo must
    be REMOVED — a membership change can drop a photo from a folder as well as
    add it, and removals are snapshotted and rolled back exactly like
    replacements.

    `commit` performs the caller's DB writes and is invoked only once every
    destination has landed. If it raises, the files are restored to their
    pre-change content so the artifacts and the DB agree on the old state.

    Returns the finalized bytes. Raises `RegenerationError` on any failure,
    having first put every file back.
    """
    from app.services.photo_processing_service import _load_logo_config, _load_mask_config

    guard = _FileGuard()
    batch_dir = photo_batches_dir / batch.id
    logo_config = _load_logo_config(batch_dir)
    mask_config = _load_mask_config(batch_dir)

    try:
        # ---- STEP 1: render every destination to TEMP paths ---------------
        original = storage_path / photo.original_path
        if not original.is_file():
            raise RegenerationError(f"ORIGINAL for {photo.filename} is missing; cannot regenerate.")
        finalized = render_photo_bytes(photo, list(faces), original.read_bytes(),
                                       logo_config, mask_config)

        person_ids = {f.person_id for f in faces if f.person_id}
        wanted = destination_paths(session, batch, photo, list(faces), storage_path,
                                   photo_batches_dir, person_ids)

        stale: list[Path] = []
        for person_id in stale_person_ids:
            if not person_id or person_id in person_ids:
                continue
            person = session.get(Person, person_id)
            if person:
                stale.append(sorted_dir(photo_batches_dir, batch, person) / photo.filename)

        temp_for: dict[Path, Path] = {}
        for dest in wanted:
            temp = dest.parent / f".{dest.name}{TEMP_SUFFIX}"
            temp.parent.mkdir(parents=True, exist_ok=True)
            temp.write_bytes(finalized)
            temp_for[dest] = temp
            guard.temps.append(temp)

        # The grid thumbnail is one more guarded destination, with its own
        # bytes. It is replaced LAST (after MEDIA) and under the thumbnail's own
        # lock, so an on-demand generation can never write a stale tile over
        # it. If it cannot be encoded, the old tile is REMOVED — guarded like
        # any other removal — rather than left showing the previous render; it
        # is then regenerated on demand from the new MEDIA.
        from app.services import photo_thumbnail_service as pts

        thumb_dest = storage_path / pts.thumbnail_relpath(batch.storage_dir, photo.filename)
        thumb_bytes = pts.encode_thumbnail(finalized)
        if thumb_bytes is not None:
            thumb_temp = thumb_dest.parent / f".{thumb_dest.name}{TEMP_SUFFIX}"
            thumb_temp.parent.mkdir(parents=True, exist_ok=True)
            thumb_temp.write_bytes(thumb_bytes)
            temp_for[thumb_dest] = thumb_temp
            guard.temps.append(thumb_temp)
        elif thumb_dest.exists():
            stale.append(thumb_dest)

        # ---- STEP 2: snapshot everything about to change ------------------
        for path in list(temp_for) + stale:
            guard.remember(path)

        # ---- STEP 3: replace/remove live files one at a time --------------
        for dest, temp in temp_for.items():
            if dest == thumb_dest:
                with pts.thumbnail_lock(dest):
                    _atomic_replace(temp, dest)
            else:
                _atomic_replace(temp, dest)
            guard.changed.append(dest)
        for path in stale:
            guard.changed.append(path)
            _remove_file(path)

    except Exception as e:  # noqa: BLE001 — any failure before the commit
        guard.restore_all()
        guard.discard_temps()
        session.rollback()
        if isinstance(e, RegenerationError):
            raise
        raise RegenerationError(f"Could not regenerate this photo's outputs: {e}") from e

    # ---- STEP 4: every destination landed — now the DB, as one unit -------
    if commit is not None:
        try:
            commit()
        except Exception as e:  # noqa: BLE001
            # The one remaining window: files are already new, the DB is not.
            # Treat it exactly like a mid-replacement failure — restore ALL
            # files so the artifacts and the DB agree on the PRE-change state.
            session.rollback()
            guard.restore_all()
            guard.discard_temps()
            raise RegenerationError(f"Could not save this change: {e}") from e

    guard.discard_temps()
    return finalized
