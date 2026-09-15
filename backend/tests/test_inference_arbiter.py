"""Phase G2 — inference arbiter (process-wide lock around the one shared
FaceAnalysis session). Proves engine.detect_faces() (kiosk/enrollment) and
event_photo_detection_service.detect_event_faces() (Event Auto mode)
actually serialize against EACH OTHER through the same lock object, not
just against themselves — the real correctness gap this phase closes.

Uses a fake FaceAnalysis app (no real model/GPU load) so this test is fast
and runs anywhere; the thing under test is the lock, not InsightFace.
"""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

import app.face_recognition.engine as engine
import app.services.event_photo_detection_service as event_detect


class _ConcurrencyProbe:
    """Shared across fake model calls from BOTH entry points — records
    whether more than one call was ever "inside" at the same instant."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter(self):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def exit(self):
        with self.lock:
            self.active -= 1


def _make_fake_app(probe: _ConcurrencyProbe, sleep_s: float = 0.02):
    class FakeDetModel:
        def detect(self, image, max_num=0, metric="default"):
            probe.enter()
            try:
                time.sleep(sleep_s)
                return np.zeros((0, 5), dtype=np.float32), None
            finally:
                probe.exit()

    class FakeRecognitionModel:
        def get(self, image, face):
            probe.enter()
            try:
                time.sleep(sleep_s)
                face.normed_embedding = np.zeros(512, dtype=np.float32)
            finally:
                probe.exit()

    class FakeApp:
        det_model = FakeDetModel()
        models = {"recognition": FakeRecognitionModel()}

    return FakeApp()


class InferenceArbiterTests(unittest.TestCase):
    def test_event_detection_imports_the_same_lock_instance(self):
        # detect_event_faces() imports inference_lock lazily from engine —
        # confirm it is the SAME object, not a second Lock() elsewhere.
        import inspect

        source = inspect.getsource(event_detect.detect_event_faces)
        self.assertIn("from app.face_recognition.engine import", source)
        self.assertIn("inference_lock", source)

    def test_kiosk_and_event_calls_never_overlap_inside_the_shared_model(self):
        probe = _ConcurrencyProbe()
        fake_app = _make_fake_app(probe)

        image = np.zeros((100, 100, 3), dtype=np.uint8)

        errors = []

        def run_kiosk():
            try:
                engine.detect_faces(image)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def run_event():
            try:
                event_detect.detect_event_faces(image)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=run_kiosk) for _ in range(3)] + \
                  [threading.Thread(target=run_event) for _ in range(3)]
        # Patched ONCE, around every thread: mock.patch is not thread-safe, and
        # six threads entering/exiting the same patch can interleave so that the
        # restore puts a mock back instead of the real function — leaving
        # engine.get_face_app permanently mocked for the rest of the suite.
        with patch.object(engine, "get_face_app", return_value=fake_app):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertLessEqual(probe.max_active, 1, "kiosk and Event calls overlapped inside the shared model — the arbiter did not serialize them")

    def test_lock_is_uncontended_for_a_single_caller(self):
        """Sanity check: the lock itself adds no correctness issue for the
        single-caller case (no deadlock, returns normally)."""
        probe = _ConcurrencyProbe()
        fake_app = _make_fake_app(probe, sleep_s=0.001)
        image = np.zeros((50, 50, 3), dtype=np.uint8)
        with patch.object(engine, "get_face_app", return_value=fake_app):
            result = engine.detect_faces(image)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
