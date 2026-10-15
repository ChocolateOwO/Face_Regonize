"""Workstream 2 — POST /api/look: stateless recognition for robot-hub frames.

Detection is faked (no model load); decoding, size/format checks, matching,
sorting/normalization, the busy/unavailable paths, the raw-body ASGI cap and
the engine's bounded lock wait are the real code."""
from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, UploadFile

from app.api import look as look_api
from app.face_recognition import engine
from app.face_recognition.engine import DetectedFace
from app.face_recognition.index import RecognitionIndex
from app.models.models import User
from app.services import look_service, node_registry, settings_cache, storage_service

USER = User(id="u1", username="admin", password_hash="x")


def unit(i: int) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    v[i] = 1.0
    return v


def jpeg(w: int, h: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.random.default_rng(0).integers(0, 255, (h, w, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


class LookCase(unittest.TestCase):
    def setUp(self):
        people = [SimpleNamespace(id=f"p{i}", first_name=f, last_name=l, participant_id=f"{i + 1:04d}",
                                  embedding=unit(i).tobytes())
                  for i, (f, l) in enumerate([("Ann", "Alpha"), ("Ben", "Beta")])]
        index = RecognitionIndex()
        index.rebuild(people)
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-look-")
        self.addCleanup(self.temp.cleanup)
        storage = Path(self.temp.name)
        self.faces: list[DetectedFace] = []
        self.detect_calls: list[dict] = []

        def fake_detect(img, timings=None, lock_timeout=None):
            self.detect_calls.append({"shape": img.shape, "lock_timeout": lock_timeout})
            if isinstance(self.faces, Exception):
                raise self.faces
            return list(self.faces)

        for target, name, value in [
            (look_service, "recognition_index", index),
            (look_service.engine, "is_ready", lambda: True),
            (look_service.engine, "detect_faces", fake_detect),
            (storage_service, "STORAGE_PATH", storage),
            (storage_service, "EVENTS_DIR", storage / "events"),
            (storage_service, "THUMBNAILS_DIR", storage / "thumbnails"),
            (storage_service, "save_event_image", lambda *a, **k: (_ for _ in ()).throw(AssertionError("saved"))),
            (look_api.look_service.logger, "disabled", True),
        ]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        from app.services import events_feed
        p = patch.object(events_feed, "publish", side_effect=AssertionError("published"))
        p.start()
        self.addCleanup(p.stop)
        settings_cache.set_threshold(0.45)
        node_registry._nodes.clear()
        self.storage = storage

    def call(self, data: bytes, want: str = "faces", source: str | None = "nong-cam-1"):
        return look_api.look(photo=UploadFile(io.BytesIO(data), filename="f.jpg"), source=source, want=want, user=USER)

    def status_of(self, data: bytes, **kw) -> HTTPException:
        with self.assertRaises(HTTPException) as ctx:
            self.call(data, **kw)
        return ctx.exception


class ResponseTests(LookCase):
    def test_sizes_sort_normalization_and_unknown(self):
        for w, h in ((320, 240), (640, 480)):
            self.faces = [
                DetectedFace(unit(1), (0.0, 0.0, w * 0.1, h * 0.1), 0.7),           # small, Ben
                DetectedFace(unit(500), (w * 0.5, h * 0.5, w * 0.9, h * 0.9), 0.6),  # large, unknown
                DetectedFace(unit(0), (-5.0, h * 0.2, w * 0.3, h * 0.6), 0.8),       # medium, Ann, off-edge
            ]
            out = self.call(jpeg(w, h))
            self.assertEqual(out["image"], {"w": w, "h": h})
            self.assertIs(out["kept"], False)
            self.assertIsInstance(out["took_ms"], float)
            self.assertEqual([f["status"] for f in out["faces"]], ["unknown", "matched", "matched"])
            unknown, ann, ben = out["faces"]
            self.assertEqual(set(unknown), {"box", "face_score", "status"})
            self.assertEqual(unknown["box"], [0.5, 0.5, 0.9, 0.9])
            self.assertEqual(ann["box"][0], 0.0)  # clipped into the frame
            self.assertEqual((ann["participant_id"], ann["first_name"], ann["name"], ann["person_id"]),
                             ("0001", "Ann", "Ann Alpha", "p0"))
            self.assertEqual((ben["name"], ben["confidence"], ben["face_score"]), ("Ben Beta", 1.0, 0.7))
            self.assertTrue(all(0.0 <= v <= 1.0 for f in out["faces"] for v in f["box"]))
            self.assertEqual(self.detect_calls[-1]["lock_timeout"], look_service.LOCK_TIMEOUT_S)

    def test_no_faces_is_200_with_empty_list(self):
        self.faces = []
        out = self.call(jpeg(640, 480))
        self.assertEqual(out["faces"], [])

    def test_nothing_is_kept_after_many_calls(self):
        self.faces = [DetectedFace(unit(0), (10.0, 10.0, 60.0, 60.0), 0.9)]
        for _ in range(50):
            self.call(jpeg(320, 240))
        self.assertEqual([p for p in self.storage.rglob("*")], [])
        self.assertEqual(node_registry._nodes, {})


class ErrorTests(LookCase):
    def test_png_and_garbage_are_400(self):
        ok, png = cv2.imencode(".png", np.zeros((10, 10, 3), np.uint8))
        self.assertEqual(self.status_of(png.tobytes()).status_code, 400)
        self.assertEqual(self.status_of(b"hello").status_code, 400)
        self.assertEqual(self.detect_calls, [])

    def test_truncated_jpeg_is_400(self):
        data = jpeg(640, 480)
        self.assertEqual(self.status_of(data[: len(data) // 2]).status_code, 400)

    def test_oversize_bytes_is_413(self):
        data = b"\xff\xd8\xff" + b"\0" * look_service.MAX_PHOTO_BYTES + b"\xff\xd9"
        self.assertEqual(self.status_of(data).status_code, 413)

    def test_oversize_dimensions_is_413_before_decode(self):
        with patch.object(storage_service, "decode_image", side_effect=AssertionError("decoded")):
            self.assertEqual(self.status_of(jpeg(2100, 16)).status_code, 413)
            self.assertEqual(self.status_of(jpeg(2048, 2100)).status_code, 413)

    def test_busy_semaphore_is_429(self):
        held = [look_service._admission.acquire(blocking=False) for _ in range(look_service.MAX_IN_FLIGHT)]
        try:
            self.assertTrue(all(held))
            exc = self.status_of(jpeg(320, 240))
            self.assertEqual((exc.status_code, exc.headers), (429, {"Retry-After": "0"}))
        finally:
            for _ in held:
                look_service._admission.release()
        self.call(jpeg(320, 240))  # capacity is back

    def test_lock_timeout_is_429_and_releases_admission(self):
        self.faces = engine.InferenceBusy("busy")
        for _ in range(look_service.MAX_IN_FLIGHT + 1):
            self.assertEqual(self.status_of(jpeg(320, 240)).status_code, 429)
        self.faces = []
        self.call(jpeg(320, 240))

    def test_not_ready_and_model_failure_are_503(self):
        with patch.object(look_service.engine, "is_ready", lambda: False):
            self.assertEqual(self.status_of(jpeg(320, 240)).status_code, 503)
        self.faces = RuntimeError("CUDA died")
        self.assertEqual(self.status_of(jpeg(320, 240)).status_code, 503)

    def test_want_persons_is_reserved(self):
        exc = self.status_of(jpeg(320, 240), want="persons")
        self.assertEqual(exc.status_code, 422)
        self.assertEqual(self.detect_calls, [])

    def test_missing_token_is_401(self):
        from app.auth.deps import get_current_user
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(request=None, credentials=None, session=None)
        self.assertEqual(ctx.exception.status_code, 401)


class OpenApiTests(unittest.TestCase):
    def test_look_is_documented(self):
        app = FastAPI()
        app.include_router(look_api.router)
        spec = app.openapi()
        self.assertIn("/api/look", spec["paths"])
        ref = spec["paths"]["/api/look"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
        props = spec["components"]["schemas"][ref.rsplit("/", 1)[1]]["properties"]
        self.assertEqual(set(props), {"photo", "source", "want"})


class EngineLockTimeoutTests(unittest.TestCase):
    """The additive lock_timeout: None keeps the old blocking wait; a number
    bounds it. The lock is always released, including on failure."""

    def setUp(self):
        det = SimpleNamespace(detect=lambda img, max_num=0, metric="default": (np.zeros((0, 5), np.float32), None))
        fake_app = SimpleNamespace(det_model=det, models={"recognition": SimpleNamespace(get=lambda img, face: None)})
        p = patch.object(engine, "get_face_app", lambda: fake_app)
        p.start()
        self.addCleanup(p.stop)
        self.img = np.zeros((8, 8, 3), np.uint8)

    def test_timeout_raises_busy_when_lock_is_held(self):
        engine.inference_lock.acquire()
        try:
            with self.assertRaises(engine.InferenceBusy):
                engine.detect_faces(self.img, lock_timeout=0.05)
        finally:
            engine.inference_lock.release()
        self.assertEqual(engine.detect_faces(self.img, lock_timeout=0.05), [])
        self.assertFalse(engine.inference_lock.locked())

    def test_default_waits_for_the_lock(self):
        engine.inference_lock.acquire()
        done = []
        t = threading.Thread(target=lambda: done.append(engine.detect_faces(self.img)))
        t.start()
        t.join(0.2)
        self.assertEqual(done, [])  # still waiting, not failing
        engine.inference_lock.release()
        t.join(5)
        self.assertEqual(done, [[]])
        self.assertFalse(engine.inference_lock.locked())

    def test_lock_released_on_failure(self):
        bad = SimpleNamespace(det_model=SimpleNamespace(detect=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))),
                              models={})
        with patch.object(engine, "get_face_app", lambda: bad):
            with self.assertRaises(RuntimeError):
                engine.detect_faces(self.img)
        self.assertFalse(engine.inference_lock.locked())


class BodyLimitMiddlewareTests(unittest.TestCase):
    def run_asgi(self, path: str, chunks: list[bytes], headers=()):
        reached = {"app": False, "body": b""}

        async def app(scope, receive, send):
            reached["app"] = True
            while True:
                msg = await receive()
                reached["body"] += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        messages = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
                    for i, c in enumerate(chunks)]
        sent = []

        async def receive():
            return messages.pop(0)

        async def send(msg):
            sent.append(msg)

        guard = look_api.LookBodyLimit(app, limit=100)
        scope = {"type": "http", "path": path, "method": "POST", "headers": list(headers)}
        asyncio.run(guard(scope, receive, send))
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, body, reached

    def test_chunked_body_over_limit_is_413(self):
        status, body, reached = self.run_asgi("/api/look", [b"x" * 60, b"x" * 60])
        self.assertEqual(status, 413)
        self.assertIn("exceeds", json.loads(body)["detail"])

    def test_content_length_over_limit_is_rejected_before_the_app(self):
        status, _, reached = self.run_asgi("/api/look", [b"x"], headers=[(b"content-length", b"5000")])
        self.assertEqual(status, 413)
        self.assertFalse(reached["app"])

    def test_under_limit_and_other_paths_pass_through(self):
        self.assertEqual(self.run_asgi("/api/look", [b"x" * 50, b"x" * 50])[0], 200)
        status, _, reached = self.run_asgi("/api/recognition/upload", [b"x" * 500])
        self.assertEqual((status, len(reached["body"])), (200, 500))


if __name__ == "__main__":
    unittest.main()
