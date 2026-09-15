"""Continuous Processing Pipeline (Phase B).

Bounded, staged producer/consumer replacement for the sequential per-photo
loop, used only by workflow_version=2 batches — workflow_version=1 batches
keep running photo_processing_service's untouched sequential
_run_photo_batch. Every leaf function this orchestrates (decode_image,
detect_event_faces, recognition_index.match_batch, the D0 render/fan-out
helpers, _encode) is reused verbatim, imported from
photo_processing_service — nothing here reimplements them.

5 stages, each a small worker-thread pool pulling from one bounded
queue.Queue and pushing to the next:

    Fetch -> Decode -> Inference (hard 1 worker) -> Render -> DBCommit (hard 1 worker)

Fetch/Decode/Render may run several workers in parallel (I/O- and CPU-bound
respectively); Inference and DBCommit are structurally single-worker — one
shared FaceAnalysis session (Inference; also lock-protected in engine.py)
and SQLite's single-writer model (DBCommit) — not a tunable, per the
Continuous Processing Pipeline section.

Every queue is bounded to the SAME max_inflight figure, so backpressure
from a slow stage propagates upstream naturally — this IS the admission
control, not a separate semaphore.

Cancellation is cooperative: every worker checks the batch's existing
_cancelled(batch_id) flag (the same one the sequential path already uses)
before starting a new item, and a cancelled item is drained-not-processed
(forwarded downstream as "cancelled" so the pipeline still terminates
within a bounded time) rather than aborted mid-queue, which could leave a
worker blocked forever on a full downstream queue nobody is draining.

Out-of-order completion across photos is safe: recognition_index.match_batch()
holds no cross-photo state, and each photo's own DB rows are independent of
every other photo's (confirmed during this phase's design, see the plan).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
from sqlmodel import Session, select

from app.database.db import engine as _default_engine
from app.services import event_eta_service
from app.services import storage_service
from app.services.event_pipeline_registry import registry as pipeline_registry
from app.services.event_photo_detection_service import detect_event_faces
from app.services.hardware_info_service import clamp, get_hardware_info
from app.face_recognition.index import recognition_index
from app.models.models import (
    Person,
    PhotoBatch,
    PhotoBatchFace,
    PhotoBatchIngestionIssue,
    PhotoBatchParticipantFolder,
    PhotoBatchPhoto,
)

logger = logging.getLogger(__name__)

_SENTINEL = object()


def benchmark_profile(hardware=None) -> dict[str, int]:
    """Hardware-scaled worker counts + queue bound — a tunable starting
    profile, not fixed architecture constants (see Revised Decisions #12).
    Hard floor/ceiling regardless of detected hardware."""
    hw = hardware if hardware is not None else get_hardware_info()
    cpu = hw.cpu_cores
    return {
        "fetch_workers": clamp(cpu // 2, 1, 4),
        "decode_workers": clamp(cpu // 4, 1, 3),
        "render_workers": clamp(cpu // 3, 1, 4),
        "max_inflight": clamp(hw.free_ram_mb // 300, 2, 10),
    }


@dataclass
class PhotoWorkItem:
    """Accumulates state as it flows through the 5 stages. One instance per
    source item, created once by the Fetch stage and never replaced."""

    index: int
    source_item: Any
    filename: str

    file_bytes: bytes | None = None
    img: np.ndarray | None = None
    original_rel: str | None = None

    faces: list = field(default_factory=list)
    # (person_id, confidence, bbox_str, consent_status, folder_name|None)
    face_meta: list[tuple] = field(default_factory=list)
    is_ambience: bool = False

    finalized_bytes: bytes | None = None
    classification: str | None = None
    matched_person_ids: set = field(default_factory=set)
    blurred_count: int = 0
    # Phase F1 — evaluation candidates computed in the render stage, saved in
    # the commit stage's single per-photo commit.
    candidates: list = field(default_factory=list)

    outcome: str = "pending"  # rejected | failed | cancelled | done
    error: Exception | None = None


class _BoundedStage:
    """Runs `worker_fn` in `count` threads, all pulling from `in_q`. The
    caller is responsible for pushing exactly `count` _SENTINEL values once
    upstream production is finished, then joining every thread — this
    class does not manage that sequencing itself, so stages compose in a
    strict pipeline order without needing shared atomic counters."""

    def __init__(self, name: str, count: int, worker_fn: Callable[[], None]):
        self.name = name
        self.threads = [
            threading.Thread(target=worker_fn, name=f"{name}-{i}", daemon=True) for i in range(count)
        ]

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def join(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self.threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
        return all(not t.is_alive() for t in self.threads)


class EventPhotoPipeline:
    """One run, for one batch. Construct, call run(), done — not reused
    across batches. `is_cancelled` is injected (batch_id) -> bool, matching
    photo_processing_service._cancelled(), so this module never needs to
    import photo_processing_service (avoiding a circular import; that
    module lazily imports THIS one instead)."""

    def __init__(
        self,
        batch_id: str,
        source,
        logo_config,
        threshold: float,
        is_cancelled: Callable[[str], bool],
        progress: Callable[[str], None] | None = None,
        profile: dict[str, int] | None = None,
        mask_config=None,
        storage_path: Path | None = None,
        photo_batches_dir: Path | None = None,
        db_engine: Any = None,
    ) -> None:
        self.batch_id = batch_id
        self.source = source
        self.logo_config = logo_config
        self.mask_config = mask_config
        self.threshold = threshold
        self.is_cancelled = is_cancelled
        self.progress = progress or (lambda _msg: None)
        self.profile = profile or benchmark_profile()
        # Explicit parameter, not the bare module-level default, for the
        # same reason storage_path/photo_batches_dir are: a real running
        # process only ever has ONE engine (this module's own import and
        # photo_processing_service's own import are always the identical
        # object), but a test that patches photo_processing_service.engine
        # needs that same override to actually reach the DB work done here.
        self.engine = db_engine if db_engine is not None else _default_engine
        self.handle = pipeline_registry.register(batch_id)

        # The TRUE global admission gate: bounding each queue's own maxsize
        # independently is NOT sufficient (an item can be in-flight in any
        # of 5 different queues/workers at once, so 5 independently-bounded
        # queues allow up to ~5x max_inflight concurrent items). One shared
        # semaphore, acquired before admitting an item and released only
        # once it is fully committed, is what actually caps total
        # concurrent in-flight items pipeline-wide.
        self._admission = threading.Semaphore(self.profile["max_inflight"])

        # Instrumentation, not just for tests: how many items are actively
        # admitted (semaphore held) at any moment — the direct, observable
        # proof that max_inflight bounds real concurrent work.
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self.max_observed_in_flight = 0

        # Deferred import — see module docstring; breaks the circular
        # dependency between this module and photo_processing_service.
        from app.services.photo_processing_service import (
            _apply_logo, _apply_privacy_mask, _blur_region, _consent_status, _encode, _safe_folder_name, _write_bytes,
        )

        self._apply_logo = _apply_logo
        self._blur_region = _blur_region
        self._apply_privacy_mask = _apply_privacy_mask
        self._consent_status = _consent_status
        self._encode = _encode
        self._safe_folder_name = _safe_folder_name
        self._write_bytes = _write_bytes

        # Explicit parameters, not a fresh `from app.config import ...` here
        # — the caller (photo_processing_service._run_photo_batch_pipeline)
        # passes ITS OWN STORAGE_PATH/PHOTO_BATCHES_DIR references, the same
        # dependency-injection pattern Phase A1 already used for
        # list_image_files/download_file. A fresh import from app.config
        # would always happen to match in a real running process (those
        # values never change after process startup) but silently diverges
        # from whatever a test has patched on photo_processing_service —
        # this keeps there being exactly one source of truth either way.
        if storage_path is None or photo_batches_dir is None:
            from app.config import PHOTO_BATCHES_DIR, STORAGE_PATH

            storage_path = storage_path if storage_path is not None else STORAGE_PATH
            photo_batches_dir = photo_batches_dir if photo_batches_dir is not None else PHOTO_BATCHES_DIR
        self._storage_path = storage_path
        self._photo_batches_dir = photo_batches_dir

    def _write_thumbnail(self, batch_storage_dir: str, filename: str, finalized: bytes) -> None:
        """Phase I4/J1 — ONE grid thumbnail per photo, from the FINALIZED
        (privacy-rendered) bytes so a masked face stays masked in the grid.
        Best-effort by design: the grid falls back to full resolution."""
        from app.services import photo_thumbnail_service

        photo_thumbnail_service.write_thumbnail(
            self._storage_path / photo_thumbnail_service.thumbnail_relpath(batch_storage_dir, filename),
            finalized,
        )

    # ---- stage bodies ------------------------------------------------

    def _cancelled_now(self) -> bool:
        return self.is_cancelled(self.batch_id) or self.handle.is_cancelled()

    def _fetch_worker(self, in_q: "queue.Queue", out_q: "queue.Queue") -> None:
        while True:
            item = in_q.get()
            if item is _SENTINEL:
                return
            if self._cancelled_now():
                item.outcome = "cancelled"
                out_q.put(item)
                continue
            try:
                item.file_bytes = self.source.fetch(item.source_item)
                out_q.put(item)
            except Exception as e:  # noqa: BLE001 — one bad source item must not kill the batch
                logger.exception("Pipeline %s: fetch failed for %s", self.batch_id, item.filename)
                item.outcome = "rejected"
                item.error = e
                out_q.put(item)

    def _decode_worker(self, in_q: "queue.Queue", out_q: "queue.Queue", batch_storage_dir: str) -> None:
        while True:
            item = in_q.get()
            if item is _SENTINEL:
                return
            if item.outcome != "pending":
                out_q.put(item)
                continue
            if self._cancelled_now():
                item.outcome = "cancelled"
                out_q.put(item)
                continue
            try:
                img = storage_service.decode_image(item.file_bytes)
                if img is None:
                    raise RuntimeError("not a readable image")
                original_rel = f"{batch_storage_dir}/ORIGINAL/{item.filename}"
                self._write_bytes(self._storage_path / original_rel, item.file_bytes)
                item.img = img
                item.original_rel = original_rel
                out_q.put(item)
            except Exception as e:  # noqa: BLE001 — one bad source item must not kill the batch
                logger.exception("Pipeline %s: decode/validate rejected %s", self.batch_id, item.filename)
                item.outcome = "rejected"
                item.error = e
                out_q.put(item)

    def _inference_worker(self, in_q: "queue.Queue", out_q: "queue.Queue") -> None:
        """Hard single worker — one shared FaceAnalysis session (also
        lock-protected in engine.py against the kiosk), and no throughput
        to gain from more threads (see Continuous Processing Pipeline)."""
        while True:
            item = in_q.get()
            if item is _SENTINEL:
                return
            if item.outcome != "pending":
                out_q.put(item)
                continue
            if self._cancelled_now():
                item.outcome = "cancelled"
                out_q.put(item)
                continue
            try:
                faces = detect_event_faces(item.img)
                if not faces:
                    item.is_ambience = True
                    out_q.put(item)
                    continue

                embeddings = np.stack([f.embedding for f in faces])
                matches = recognition_index.match_batch(embeddings, self.threshold)

                with Session(self.engine) as session:
                    for face, (person_id, _first, _full, _pid, score) in zip(faces, matches):
                        bbox_str = ",".join(f"{v:.1f}" for v in face.bbox)
                        if person_id:
                            item.matched_person_ids.add(person_id)
                            consent = self._consent_status(session, person_id)
                            person = session.get(Person, person_id)
                            folder_name = (
                                self._safe_folder_name(person.participant_id, person.first_name, person.last_name)
                                if person else None
                            )
                        else:
                            consent = "no_match"
                            folder_name = None
                        item.face_meta.append((person_id, float(score), bbox_str, consent, folder_name))
                item.faces = faces
                out_q.put(item)
            except Exception as e:  # noqa: BLE001 — one bad photo must not kill the batch
                logger.exception("Pipeline %s: inference failed for %s", self.batch_id, item.filename)
                item.outcome = "failed"
                item.error = e
                out_q.put(item)

    def _render_worker(self, in_q: "queue.Queue", out_q: "queue.Queue", batch_id_for_paths: str, batch_storage_dir: str) -> None:
        while True:
            item = in_q.get()
            if item is _SENTINEL:
                return
            if item.outcome != "pending":
                out_q.put(item)
                continue
            if self._cancelled_now():
                item.outcome = "cancelled"
                out_q.put(item)
                continue
            try:
                media_img = item.img.copy()

                if item.is_ambience:
                    self._apply_logo(media_img, self.logo_config)
                    finalized = self._encode(media_img, item.filename) if self.logo_config else item.file_bytes
                    ambience_rel = f"{batch_storage_dir}/AMBIENCE/{item.filename}"
                    media_rel = f"{batch_storage_dir}/MEDIA/{item.filename}"
                    self._write_bytes(self._storage_path / ambience_rel, finalized)
                    self._write_bytes(self._storage_path / media_rel, finalized)
                    self._write_thumbnail(batch_storage_dir, item.filename, finalized)
                    item.finalized_bytes = finalized
                    item.classification = "ambience"
                    out_q.put(item)
                    continue

                for person_id, _score, bbox_str, consent, _folder in item.face_meta:
                    if person_id and consent == "declined":
                        bbox = tuple(float(v) for v in bbox_str.split(","))
                        self._apply_privacy_mask(media_img, bbox, self.mask_config)
                        item.blurred_count += 1

                self._apply_logo(media_img, self.logo_config)
                finalized = self._encode(media_img, item.filename)
                item.finalized_bytes = finalized
                from app.services import identity_candidate_service
                item.candidates = identity_candidate_service.build_candidates(
                    engine=self.engine, batch_id=self.batch_id, filename=item.filename, img=item.img,
                    faces=item.faces, predicted=[m[0] for m in item.face_meta], threshold=self.threshold,
                )
                # An unrecognized face routes nowhere special: the Review
                # workflow is removed, so no REVIEW/ output is written and
                # "review" is never assigned.
                item.classification = "sorted"

                seen_folders: set[str] = set()
                for person_id, _score, _bbox, _consent, folder_name in item.face_meta:
                    if not person_id or not folder_name or folder_name in seen_folders:
                        continue
                    seen_folders.add(folder_name)
                    dest_dir = self._photo_batches_dir / batch_id_for_paths / "SORTED" / folder_name
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    self._write_bytes(dest_dir / item.filename, finalized)

                media_rel = f"{batch_storage_dir}/MEDIA/{item.filename}"
                self._write_bytes(self._storage_path / media_rel, finalized)
                self._write_thumbnail(batch_storage_dir, item.filename, finalized)
                out_q.put(item)
            except Exception as e:  # noqa: BLE001 — one bad photo must not kill the batch
                logger.exception("Pipeline %s: render failed for %s", self.batch_id, item.filename)
                item.outcome = "failed"
                item.error = e
                out_q.put(item)

    def _commit_worker(self, in_q: "queue.Queue", batch_storage_dir: str, done_event: threading.Event) -> None:
        """Hard single worker — SQLite has one writer; this is also the
        ONLY place any of the 5 stages touches the database, so there is
        exactly one place any counter is ever incremented."""
        with Session(self.engine) as session:
            batch = session.get(PhotoBatch, self.batch_id)
            while True:
                item = in_q.get()
                if item is _SENTINEL:
                    break
                try:
                    self._commit_item(session, batch, item, batch_storage_dir)
                except Exception as e:  # noqa: BLE001 — one bad DB write must not wedge the commit stage
                    logger.exception("Pipeline %s: DB commit failed for %s", self.batch_id, item.filename)
                    # This session is shared by every photo in the batch. Left
                    # in a failed transaction, EVERY later photo's commit would
                    # fail too — so roll back first. Then record the photo as
                    # failed (retriable, its files are already on disk), so the
                    # batch cannot reach ready silently missing it.
                    session.rollback()
                    # A successfully rendered item reaches the commit with its
                    # outcome still unset — "done" is simply the fall-through
                    # after the rejected/cancelled/failed branches — so test
                    # for "not already one of those" rather than for "done".
                    if item.outcome not in ("rejected", "cancelled", "failed"):
                        item.outcome, item.error = "failed", e
                        try:
                            self._commit_item(session, batch, item, batch_storage_dir)
                        except Exception:  # noqa: BLE001
                            logger.exception("Pipeline %s: could not record %s as failed", self.batch_id, item.filename)
                            session.rollback()
                finally:
                    # Release the admission slot ONLY once fully committed —
                    # this is what makes max_inflight a real global cap
                    # rather than a per-queue depth limit.
                    with self._in_flight_lock:
                        self._in_flight -= 1
                    self._admission.release()
        done_event.set()

    def _commit_item(self, session: Session, batch: PhotoBatch, item: PhotoWorkItem, batch_storage_dir: str) -> None:
        if item.outcome == "rejected":
            batch.rejected_photos += 1
            session.add(PhotoBatchIngestionIssue(batch_id=batch.id, filename=item.filename, reason=str(item.error)))
            session.add(batch)
            session.commit()
            event_eta_service.record_photo_done(self.batch_id)
            return

        if item.outcome == "cancelled":
            # Never fabricate a row for an item the pipeline never actually
            # touched — simply not counted anywhere; the batch is about to
            # stop (or already stopped) via cancellation regardless.
            return

        if item.outcome == "failed":
            photo = PhotoBatchPhoto(
                batch_id=batch.id, filename=item.filename,
                drive_file_id=getattr(item.source_item, "id", ""),
                original_path=item.original_rel or "",
            )
            session.add(photo)
            batch.failed_photos += 1
            batch.last_error = f"{item.filename}: {item.error}"
            batch.current_stage = f"Skipped {item.filename}: {item.error}"
            session.add(batch)
            session.commit()
            event_eta_service.record_photo_done(self.batch_id)
            return

        # "done"
        photo = PhotoBatchPhoto(
            batch_id=batch.id, filename=item.filename,
            drive_file_id=getattr(item.source_item, "id", ""),
            original_path=item.original_rel or "",
            faces_total=len(item.faces),
            faces_matched=len(item.matched_person_ids),
            faces_unknown=sum(1 for m in item.face_meta if m[3] == "no_match"),
            classification=item.classification,
            media_path=f"{batch_storage_dir}/MEDIA/{item.filename}",
        )
        # Amendment A1 — ONE commit for everything this photo contributes: its
        # row, its face rows, participant-folder counts and every counter.
        # (PhotoBatchPhoto.id is assigned at construction, so the face rows can
        # reference it before anything is flushed.) A failure rolls all of it
        # back — see _commit_worker — never leaving a half-recorded photo.
        session.add(photo)

        face_rows = [
            PhotoBatchFace(
                photo_id=photo.id, person_id=person_id, confidence=score, bbox=bbox_str,
                consent_status_at_processing=consent, blurred=bool(person_id and consent == "declined"),
            )
            for person_id, score, bbox_str, consent, _folder in item.face_meta
        ]
        for row in face_rows:
            session.add(row)
        # Phase F1 — evaluation candidates, in this same commit (bounded,
        # idempotent; never trusted, never used to match).
        from app.services import identity_candidate_service
        identity_candidate_service.replace_candidates(session, batch.id, photo.id, item.candidates,
                                                      [r.id for r in face_rows])

        if item.classification == "ambience":
            batch.ambience_photos += 1
        else:
            # Counts photos in which at least one participant was identified —
            # the same reading the sequential path uses now that the "review"
            # bucket (every face had to match) is gone.
            if item.matched_person_ids:
                batch.recognized_photos += 1
            for person_id in item.matched_person_ids:
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
            batch.faces_detected += len(item.faces)
            batch.faces_recognized += len(item.matched_person_ids)
            batch.faces_unknown += photo.faces_unknown
            batch.blurred_faces += item.blurred_count
            consented_count = sum(1 for m in item.face_meta if m[3] == "consented")
            batch.consented_faces += consented_count
            batch.not_consented_faces += len(item.face_meta) - consented_count

        batch.processed_photos += 1
        session.add(batch)
        session.commit()
        event_eta_service.record_photo_done(self.batch_id)

    # ---- orchestration -------------------------------------------------

    def run(self, items: list[tuple[int, Any, str]], batch_storage_dir: str, batch_id_for_paths: str) -> None:
        """items: [(index, source_item, filename), ...] — the full listing,
        already known upfront (same as the sequential path's `files`)."""
        p = self.profile
        maxsize = p["max_inflight"]
        fetch_q, decode_q, inference_q, render_q, commit_q = (queue.Queue(maxsize=maxsize) for _ in range(5))
        commit_done = threading.Event()

        fetch_stage = _BoundedStage("fetch", p["fetch_workers"], lambda: self._fetch_worker(fetch_q, decode_q))
        decode_stage = _BoundedStage(
            "decode", p["decode_workers"], lambda: self._decode_worker(decode_q, inference_q, batch_storage_dir)
        )
        inference_stage = _BoundedStage("inference", 1, lambda: self._inference_worker(inference_q, render_q))
        render_stage = _BoundedStage(
            "render", p["render_workers"],
            lambda: self._render_worker(render_q, commit_q, batch_id_for_paths, batch_storage_dir),
        )
        commit_thread = threading.Thread(
            target=self._commit_worker, args=(commit_q, batch_storage_dir, commit_done), daemon=True,
        )

        try:
            fetch_stage.start()
            decode_stage.start()
            inference_stage.start()
            render_stage.start()
            commit_thread.start()

            work_items = [PhotoWorkItem(index=i, source_item=src, filename=name) for i, src, name in items]
            for wi in work_items:
                if self._cancelled_now():
                    break  # stop admitting more; already-admitted items still drain through
                acquired = False
                while not acquired:
                    acquired = self._admission.acquire(timeout=0.5)
                    if not acquired and self._cancelled_now():
                        break
                if not acquired:
                    break
                with self._in_flight_lock:
                    self._in_flight += 1
                    self.max_observed_in_flight = max(self.max_observed_in_flight, self._in_flight)
                fetch_q.put(wi)
            for _ in range(p["fetch_workers"]):
                fetch_q.put(_SENTINEL)

            fetch_stage.join()
            for _ in range(p["decode_workers"]):
                decode_q.put(_SENTINEL)
            decode_stage.join()
            inference_q.put(_SENTINEL)
            inference_stage.join()
            for _ in range(p["render_workers"]):
                render_q.put(_SENTINEL)
            render_stage.join()
            commit_q.put(_SENTINEL)
            commit_thread.join(timeout=60)
        finally:
            commit_done.wait(timeout=5)
            self.handle.mark_finished()
            pipeline_registry.unregister(self.batch_id)
