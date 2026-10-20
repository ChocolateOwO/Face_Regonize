"""Event-only crowd detection. Shared model configuration is never mutated."""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

TILE_SIZE = 640
TILE_OVERLAP = 192


@dataclass
class Candidate:
    bbox: np.ndarray
    kps: np.ndarray | None
    score: float
    edge: bool
    order: int


def _starts(length: int) -> list[int]:
    if length <= TILE_SIZE:
        return [0]
    return sorted(set(range(0, length - TILE_SIZE + 1, TILE_SIZE - TILE_OVERLAP))
                  | {length - TILE_SIZE})


def _same_face(a: Candidate, b: Candidate) -> bool:
    aw, ah = a.bbox[2:] - a.bbox[:2]
    bw, bh = b.bbox[2:] - b.bbox[:2]
    intersection = np.maximum(0, np.minimum(a.bbox[2:], b.bbox[2:])
                              - np.maximum(a.bbox[:2], b.bbox[:2])).prod()
    iou = intersection / (aw * ah + bw * bh - intersection)
    centers = np.abs((a.bbox[:2] + a.bbox[2:] - b.bbox[:2] - b.bbox[2:]) / 2)
    close = np.all(centers <= .25 * np.minimum([aw, ah], [bw, bh]))
    return bool(iou >= .4 or (intersection / min(aw * ah, bw * bh) >= .8 and close))


def collect_candidates(image: np.ndarray, detector, stats: dict | None = None) -> list[Candidate]:
    """Run global then one tile at a time; retain only small detection metadata.

    640px tiles feed the unchanged 320px detector at at most 2:1 reduction.
    30% overlap gives boundary faces another complete crop. No image upscale,
    tile batch, second model, detector-size mutation or GPU parallelism.
    """
    h, w = image.shape[:2]
    if h == 0 or w == 0:
        return []
    candidates = []
    global_count = 0
    tile_count = 0

    def scan(x, y, width, height, tiled):
        nonlocal global_count, tile_count
        boxes, landmarks = detector.detect(image[y:y+height, x:x+width], max_num=0, metric="default")
        if tiled:
            tile_count += 1
        for i, box in enumerate(boxes):
            raw = np.asarray(box[:4], dtype=np.float64)
            score = float(box[4])
            if not np.all(np.isfinite(raw)) or not np.isfinite(score):
                continue
            if raw[2] <= raw[0] or raw[3] <= raw[1]:
                continue
            clipped = np.clip(raw, [0, 0, 0, 0], [width, height, width, height])
            if np.any(clipped[2:] - clipped[:2] <= 0):
                continue
            kps = None if landmarks is None else np.asarray(landmarks[i], dtype=np.float32).copy()
            if kps is not None:
                if kps.shape != (5, 2) or not np.all(np.isfinite(kps)):
                    continue
                kps += [x, y]
            edge = tiled and ((x > 0 and raw[0] < 8) or (y > 0 and raw[1] < 8)
                              or (x+width < w and raw[2] > width-8)
                              or (y+height < h and raw[3] > height-8))
            candidates.append(Candidate(clipped + [x, y, x, y], kps, score, bool(edge), len(candidates)))
            if not tiled:
                global_count += 1

    scan(0, 0, w, h, False)
    if max(h, w) > TILE_SIZE:
        for y in _starts(h):
            for x in _starts(w):
                scan(x, y, min(TILE_SIZE, w-x), min(TILE_SIZE, h-y), True)
    # Prefer complete crops, then confidence. Stable source order breaks ties.
    kept = []
    for candidate in sorted(candidates, key=lambda c: (c.edge, -c.score, c.order)):
        if not any(_same_face(candidate, previous) for previous in kept):
            kept.append(candidate)
    kept.sort(key=lambda c: (*c.bbox[[1, 0, 3, 2]], c.order))
    if stats is not None:
        stats.update(global_faces=global_count, unique_faces=len(kept),
                     duplicates_removed=len(candidates)-len(kept), tiles=tile_count)
    return kept


EMBED_CHUNK = 8  # faces embedded per lock hold


def detect_event_faces(image: np.ndarray, stats: dict | None = None):
    # Lazy imports keep geometry tests independent of model loading and app DB.
    from app.face_recognition.engine import BACKGROUND, DetectedFace, get_face_app, hold
    from insightface.app.common import Face

    started = time.perf_counter()
    app = get_face_app()

    # Phase G2: the SAME lock engine.detect_faces() (enrollment, and the
    # kiosk in shared mode) uses — both are direct calls into the one
    # FaceAnalysis session, so both must serialize against each other.
    # 2026-10-20: held for ONE detector call (global pass or one tile) or one
    # chunk of embeddings at a time, not for the whole photo, so a live
    # caller sharing this session waits at most one call instead of a whole
    # tiled scan. Same model, same calls, same order: results are unchanged.
    class _OneCallAtATime:
        def detect(self, crop, max_num=0, metric="default"):
            with hold(BACKGROUND, "event_photo"):  # engine.inference_lock
                return app.det_model.detect(crop, max_num=max_num, metric=metric)

    candidates = collect_candidates(image, _OneCallAtATime(), stats)
    faces = []
    for start in range(0, len(candidates), EMBED_CHUNK):
        with hold(BACKGROUND, "event_photo"):
            for candidate in candidates[start:start + EMBED_CHUNK]:
                face = Face(bbox=candidate.bbox.astype(np.float32), kps=candidate.kps,
                            det_score=candidate.score)
                # Align from the immutable original using remapped landmarks, not tiles.
                app.models["recognition"].get(image, face)
                faces.append(DetectedFace(face.normed_embedding.astype(np.float32),
                                          tuple(candidate.bbox.tolist()), candidate.score))
    if stats is not None:
        stats["total_ms"] = (time.perf_counter() - started) * 1000
    return faces
