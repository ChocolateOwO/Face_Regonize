"""Face detection + embedding wrapper around InsightFace.

One InsightFace FaceAnalysis app is loaded once (loading models is slow) and
reused for every request — warmed at backend startup, not on first request.
Embeddings are L2-normalized, so cosine similarity between two of them is
just a dot product (see face_recognition/index.py for the actual matching).

Only the "detection" and "recognition" sub-models are loaded. InsightFace's
buffalo_l pack ships five models (detection, two landmark models, gender/age,
recognition) and its convenience FaceAnalysis.get() call runs all five per
face by default — but we only ever read the bbox and the embedding, so the
landmark/gender-age inferences were pure waste. Restricting allowed_modules
roughly halves per-face latency for free, no accuracy trade-off, since we
never used their output.
"""
from __future__ import annotations

import logging
import time

import cv2
import numpy as np
from insightface.app import FaceAnalysis
from insightface.app.common import Face

logger = logging.getLogger(__name__)

_face_app: FaceAnalysis | None = None
MODEL_NAME = "buffalo_l"


def get_face_app() -> FaceAnalysis:
    global _face_app
    if _face_app is None:
        try:
            app = FaceAnalysis(
                name=MODEL_NAME,
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                allowed_modules=["detection", "recognition"],
            )
            app.prepare(ctx_id=0, det_size=(320, 320))
            active = app.models["recognition"].session.get_providers()
            if "CUDAExecutionProvider" not in active:
                raise RuntimeError(f"CUDA provider not active, got {active}")
            logger.info("Face recognition running on GPU (CUDAExecutionProvider)")
            _face_app = app
        except Exception as e:  # noqa: BLE001 — any GPU init failure should fall back, not crash startup
            logger.warning("GPU init failed (%s), falling back to CPU", e)
            app = FaceAnalysis(
                name=MODEL_NAME,
                providers=["CPUExecutionProvider"],
                allowed_modules=["detection", "recognition"],
            )
            app.prepare(ctx_id=0, det_size=(320, 320))
            _face_app = app

        # Pay ONNX Runtime's first-call cost (memory arena growth, kernel
        # selection) here at startup, not on the user's first real scan.
        t0 = time.perf_counter()
        detect_faces(np.zeros((320, 320, 3), dtype=np.uint8))
        logger.info("Model warm-up inference took %.0fms", (time.perf_counter() - t0) * 1000)

    return _face_app


def get_active_provider() -> str:
    app = get_face_app()
    providers = app.models["recognition"].session.get_providers()
    return providers[0] if providers else "unknown"


class DetectedFace:
    def __init__(self, embedding: np.ndarray, bbox: tuple[float, float, float, float], det_score: float):
        self.embedding = embedding
        self.bbox = bbox
        self.det_score = det_score


def resize_for_detection(image_bgr: np.ndarray, max_side: int = 960) -> tuple[np.ndarray, float]:
    """Downscale large images before detection — recognition quality is
    dominated by the detector's own internal resolution (det_size), so
    feeding it a multi-megapixel original just costs decode/resize time for
    no benefit. Returns (working_image, scale) where scale maps working-image
    coordinates back to the original (bbox * (1/scale))."""
    h, w = image_bgr.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return image_bgr, 1.0
    scale = max_side / longest
    resized = cv2.resize(image_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return resized, scale


def detect_faces(image_bgr: np.ndarray, timings: dict | None = None) -> list[DetectedFace]:
    """Re-implements FaceAnalysis.get() by hand (rather than calling it
    directly) so detection and embedding can be timed as separate stages —
    the alignment/crop step is a cheap numpy warp done inline inside the
    recognition model's .get(), so it's folded into the "embedding" number
    rather than reported as its own stage."""
    app = get_face_app()

    t0 = time.perf_counter()
    bboxes, kpss = app.det_model.detect(image_bgr, max_num=0, metric="default")
    t_detect = time.perf_counter()

    faces: list[Face] = []
    for i in range(bboxes.shape[0]):
        bbox = bboxes[i, 0:4]
        det_score = bboxes[i, 4]
        kps = kpss[i] if kpss is not None else None
        faces.append(Face(bbox=bbox, kps=kps, det_score=det_score))

    rec_model = app.models["recognition"]
    for face in faces:
        rec_model.get(image_bgr, face)
    t_embed = time.perf_counter()

    if timings is not None:
        timings["detection_ms"] = (t_detect - t0) * 1000
        timings["embedding_ms"] = (t_embed - t_detect) * 1000

    return [
        DetectedFace(
            embedding=face.normed_embedding.astype(np.float32),
            bbox=tuple(face.bbox.tolist()),
            det_score=float(face.det_score),
        )
        for face in faces
    ]


def bbox_to_str(bbox: tuple[float, float, float, float]) -> str:
    return ",".join(f"{v:.1f}" for v in bbox)
