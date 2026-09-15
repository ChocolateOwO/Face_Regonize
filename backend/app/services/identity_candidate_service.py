"""Phase F1 — capture of Event-photo faces as EVALUATION CANDIDATES only.

What this is: a bounded, deterministic, stratified sample of Event faces
(embedding + the signals a future calibration needs), written inside each
photo's single finalize commit so it is exactly as durable and as idempotent
as the photo's own face rows.

What this is not: trusted evidence or matching. Nothing here feeds the
gallery, the kiosk RecognitionIndex, or any live match decision, and
promotion to a trusted EventIdentitySample is hard-disabled until F2
calibration exists (promotion_enabled() is False and promote_candidate()
refuses). `predicted_person_id` records what the existing matcher said;
ground-truth labels come only from a future offline import.

Selection (Amendment A4): per (batch, capture bucket, extractor version) at
most QUOTA_PER_BUCKET candidates are kept — the ones with the smallest
selection_key, where the key is a hash of stable ids (batch, filename, face
index). That "bottom-k" rule is order-independent, so the final sample is the
same whatever order photos finish in, a retried photo re-enters with the same
keys, and nothing is ever buffered in memory. A unique constraint on
(photo_id, face_index, extractor_version) makes a duplicate impossible.

Buckets are STRATA for the sample, not decisions. Capturing never raises into
photo processing: if anything about capture fails — or the identity tables do
not exist on this database — the photo is processed exactly as before and
simply contributes no candidates.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import logging
import threading
import time

import cv2
import numpy as np
from sqlalchemy import inspect as sa_inspect
from sqlmodel import Session, func, select

from app.models.identity_models import EventIdentityCandidate, EventIdentitySample
from app.services.event_identity_gallery import EventIdentityGallery

logger = logging.getLogger(__name__)

CAPTURE_ENABLED = True
QUOTA_PER_BUCKET = 50
BUCKETS = ("matched", "ambiguous", "low_score", "no_match")
# Stratification bins only — deliberately NOT calibrated gates.
AMBIGUOUS_MARGIN = 0.05
LOW_SCORE_FLOOR = 0.30
GALLERY_TTL_S = 120.0

CANDIDATE_TABLE = EventIdentityCandidate.__tablename__
SAMPLE_TABLE = EventIdentitySample.__tablename__


class PromotionDisabledError(RuntimeError):
    pass


def promotion_enabled() -> bool:
    """Trusted promotion stays OFF until F2 produces calibrated thresholds from
    human-labeled data. There is intentionally no setting that turns it on."""
    return False


def promote_candidate(*_args, **_kwargs):
    raise PromotionDisabledError(
        "Promoting a candidate to a trusted identity sample is disabled until "
        "calibrated thresholds exist (Phase F2)."
    )


def _has_tables(bind, *tables: str) -> bool:
    """Migration 010 creates these; a database without them (an old test DB,
    or one where 010 never ran) simply gets no candidates."""
    try:
        insp = sa_inspect(bind)
        return all(insp.has_table(t) for t in tables)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def _file_sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


@lru_cache(maxsize=1)
def extractor_version() -> str:
    """Identifies the exact extractor behind an embedding: model pack, det/rec
    ONNX hashes, detector input size and the Event tiling parameters. Two
    embeddings are only comparable when this matches."""
    from app.face_recognition import engine
    from app.services.event_photo_detection_service import TILE_OVERLAP, TILE_SIZE

    det = rec = "unknown"
    try:
        app = engine.get_face_app()
        det_file = getattr(getattr(app, "det_model", None), "model_file", None)
        rec_file = getattr(app.models.get("recognition"), "model_file", None)
        det = _file_sha(det_file) if det_file else "unknown"
        rec = _file_sha(rec_file) if rec_file else "unknown"
    except Exception:  # noqa: BLE001 — provenance must never break processing
        logger.warning("Could not fingerprint the face models; extractor_version is partial", exc_info=True)
    return f"{engine.MODEL_NAME}|det:{det}|rec:{rec}|det_size:320|tile:{TILE_SIZE}/{TILE_OVERLAP}"


# ---------------------------------------------------------------------------
# Scoring context (a private gallery per batch — never the global one)
# ---------------------------------------------------------------------------

_gallery_lock = threading.Lock()
_galleries: dict[tuple[str, int], tuple[float, EventIdentityGallery]] = {}


def _gallery_for(batch_id: str, engine) -> EventIdentityGallery:
    key = (batch_id, id(engine))
    now = time.monotonic()
    with _gallery_lock:
        for stale in [k for k, (t, _) in _galleries.items() if now - t > 10 * GALLERY_TTL_S]:
            del _galleries[stale]
        cached = _galleries.get(key)
        if cached and now - cached[0] <= GALLERY_TTL_S:
            return cached[1]
    gallery = EventIdentityGallery()
    with Session(engine) as session:
        gallery.rebuild(session)
    with _gallery_lock:
        _galleries[key] = (now, gallery)
    return gallery


def capture_bucket(predicted: bool, top1: float, margin: float, has_top2: bool) -> str:
    ambiguous = has_top2 and margin < AMBIGUOUS_MARGIN
    if predicted:
        return "ambiguous" if ambiguous else "matched"
    if top1 >= LOW_SCORE_FLOOR:
        return "ambiguous" if ambiguous else "low_score"
    return "no_match"


def selection_key(batch_id: str, filename: str, face_index: int) -> str:
    return hashlib.sha256(f"{batch_id}|{filename}|{face_index}".encode("utf-8")).hexdigest()


def sharpness(img: np.ndarray, bbox) -> float:
    h, w = img.shape[:2]
    x1, y1, x2, y2 = (int(round(v)) for v in bbox)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return 0.0
    crop = img[y1:y2, x1:x2]
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


def build_candidates(*, engine, batch_id: str, filename: str, img: np.ndarray, faces: list,
                     predicted: list, threshold: float) -> list[EventIdentityCandidate]:
    """Compute candidate rows for one photo's faces. No database writes.

    `faces[i]` needs `.embedding` and `.bbox` (and optionally `.det_score`);
    `predicted[i]` is the existing matcher's person id or None. Returned rows
    carry face_index but no photo_id/face_id yet — the finalize commit sets
    those. Never raises: returns [] if anything goes wrong.
    """
    if not CAPTURE_ENABLED or not faces or img is None:
        return []
    try:
        if not _has_tables(engine, CANDIDATE_TABLE, SAMPLE_TABLE):
            return []
        gallery = _gallery_for(batch_id, engine)
        version = extractor_version()
        h, w = img.shape[:2]
        rows: list[EventIdentityCandidate] = []
        for index, (face, person_id) in enumerate(zip(faces, predicted)):
            embedding = np.asarray(face.embedding, dtype=np.float32).reshape(-1)
            if embedding.shape != (512,) or not np.all(np.isfinite(embedding)):
                continue
            match = gallery.score(embedding)
            has_top2 = match.top2_person_id is not None
            x1, y1, x2, y2 = (float(v) for v in face.bbox)
            rows.append(EventIdentityCandidate(
                batch_id=batch_id, photo_id="", face_index=index,
                predicted_person_id=person_id or None,
                top1_person_id=match.top1_person_id, top1_score=float(match.top1_score),
                top2_person_id=match.top2_person_id, top2_score=float(match.top2_score),
                margin=float(match.margin),
                capture_bucket=capture_bucket(bool(person_id), float(match.top1_score), float(match.margin), has_top2),
                selection_key=selection_key(batch_id, filename, index),
                face_area_px=int(max(0.0, x2 - x1) * max(0.0, y2 - y1)),
                det_score=float(getattr(face, "det_score", 0.0) or 0.0),
                sharpness=sharpness(img, (x1, y1, x2, y2)),
                bbox=f"{x1:.1f},{y1:.1f},{x2:.1f},{y2:.1f}",
                image_w=int(w), image_h=int(h),
                extractor_version=version, recognition_threshold=float(threshold),
                embedding=embedding.tobytes(),
            ))
        return rows
    except Exception:  # noqa: BLE001 — capture must never affect processing
        logger.warning("Identity candidate capture skipped for %s", filename, exc_info=True)
        return []


def replace_candidates(session: Session, batch_id: str, photo_id: str, rows: list[EventIdentityCandidate],
                       face_ids: list | None = None) -> None:
    """Inside the photo's finalize commit: replace this photo's candidates and
    apply the bottom-k quota per bucket. Idempotent for a retried photo."""
    if not _has_tables(session.connection(), CANDIDATE_TABLE):
        return
    for old in session.exec(select(EventIdentityCandidate).where(EventIdentityCandidate.photo_id == photo_id)).all():
        session.delete(old)
    session.flush()
    for row in sorted(rows, key=lambda r: r.selection_key):
        row.photo_id = photo_id
        if face_ids is not None and 0 <= row.face_index < len(face_ids):
            row.face_id = face_ids[row.face_index]
        same_stratum = (
            (EventIdentityCandidate.batch_id == batch_id)
            & (EventIdentityCandidate.capture_bucket == row.capture_bucket)
            & (EventIdentityCandidate.extractor_version == row.extractor_version)
        )
        count = session.exec(select(func.count()).select_from(EventIdentityCandidate).where(same_stratum)).one()
        if count >= QUOTA_PER_BUCKET:
            worst = session.exec(
                select(EventIdentityCandidate).where(same_stratum)
                .order_by(EventIdentityCandidate.selection_key.desc()).limit(1)
            ).first()
            if worst is None or row.selection_key >= worst.selection_key:
                continue
            session.delete(worst)
            session.flush()
        session.add(row)
        session.flush()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def delete_for_batch(session: Session, batch_id: str) -> int:
    """With the source batch (delete and retention). Returns rows removed."""
    if not _has_tables(session.connection(), CANDIDATE_TABLE):
        return 0
    rows = session.exec(select(EventIdentityCandidate).where(EventIdentityCandidate.batch_id == batch_id)).all()
    for row in rows:
        session.delete(row)
    return len(rows)


def forget_person(session: Session, person_id: str | None = None) -> None:
    """Participant deletion (`person_id=None` = the full wipe): candidates stop
    pointing at the person — a prediction or label of them is nulled and the
    candidate is excluded from evaluation — and trusted samples of the person
    are deleted."""
    if not _has_tables(session.connection(), CANDIDATE_TABLE, SAMPLE_TABLE):
        return

    def gone(value) -> bool:
        return value is not None and (person_id is None or value == person_id)

    C = EventIdentityCandidate
    query = select(C)
    if person_id is not None:
        query = query.where((C.predicted_person_id == person_id) | (C.label_person_id == person_id)
                            | (C.top1_person_id == person_id) | (C.top2_person_id == person_id))
    for row in session.exec(query).all():
        if gone(row.predicted_person_id) or gone(row.label_person_id):
            row.label_status = "excluded"
        if gone(row.predicted_person_id):
            row.predicted_person_id = None
        if gone(row.label_person_id):
            row.label_person_id = None
        if gone(row.top1_person_id):
            row.top1_person_id = None
        if gone(row.top2_person_id):
            row.top2_person_id = None
        session.add(row)

    samples = select(EventIdentitySample)
    if person_id is not None:
        samples = samples.where(EventIdentitySample.person_id == person_id)
    for sample in session.exec(samples).all():
        session.delete(sample)
