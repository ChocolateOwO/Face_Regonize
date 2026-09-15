"""Phase F1 — historical candidate backfill: isolation, guards and bounds.

Uses a temp DB, temp storage and a FAKE model builder; never loads the real
models, never touches the kiosk singleton, the inference lock or matching.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine, select

from app.face_recognition import engine as face_engine
from app.models.identity_models import EventIdentityCandidate, identity_metadata
from app.models.models import PhotoBatch, PhotoBatchFace, PhotoBatchPhoto
from app.services import identity_backfill_service as bf
from app.services import identity_candidate_service as ics

BOXES = [(40.0, 40.0, 100.0, 110.0), (200.0, 60.0, 260.0, 130.0)]


def _hw(free=8000, cuda=True):
    return lambda: SimpleNamespace(usable_providers=["CUDAExecutionProvider"] if cuda else ["CPUExecutionProvider"],
                                   gpus=[SimpleNamespace(free_vram_mb=free)])


class FakeDetector:
    def detect(self, image, max_num=0, metric="default"):
        boxes = np.array([[*b, 0.9] for b in BOXES], dtype=np.float32)
        kps = np.array([[[b[0] + 5, b[1] + 5]] * 5 for b in BOXES], dtype=np.float32)
        return boxes, kps


class FakeRecognizer:
    calls = 0

    def get(self, img, face):
        FakeRecognizer.calls += 1
        v = np.zeros(512, dtype=np.float32)
        v[int(face.bbox[0]) % 512] = 1.0
        face.embedding = v


def fake_builder():
    return SimpleNamespace(det_model=FakeDetector(), models={"recognition": FakeRecognizer()})


class ForbiddenLock:
    def __enter__(self):
        raise AssertionError("the backfill must never take the kiosk inference lock")

    def __exit__(self, *a):
        return False

    def acquire(self, *a, **k):
        raise AssertionError("the backfill must never take the kiosk inference lock")

    def locked(self):
        return False


class BackfillCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-backfill-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.storage = root / "storage"
        self.pbd = self.storage / "photo_batches"
        self.engine = create_engine(f"sqlite:///{(root / 't.db').as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        identity_metadata.create_all(self.engine)
        for target, value in ((ics, "extractor_version"),):
            p = patch.object(target, value, return_value="test-v1")
            p.start()
            self.addCleanup(p.stop)
        for guard in (patch.object(face_engine, "get_face_app", side_effect=AssertionError("no kiosk singleton")),
                      patch.object(face_engine, "inference_lock", ForbiddenLock()),
                      patch("app.face_recognition.index.RecognitionIndex.match_batch",
                            side_effect=AssertionError("no matching"))):
            guard.start()
            self.addCleanup(guard.stop)
        bf._status.clear()
        bf._status["state"] = "idle"
        self.batch_ids = []

    def make_batch(self, photos=2, status="ready", person="p-alice"):
        with Session(self.engine) as session:
            batch = PhotoBatch(label="hist", drive_folder_id="", storage_dir="", retention_days=7, status=status)
            session.add(batch)
            session.commit()
            batch.storage_dir = f"photo_batches/{batch.id}"
            (self.pbd / batch.id / "ORIGINAL").mkdir(parents=True)
            for i in range(photos):
                name = f"p{i}.png"
                img = np.random.default_rng(i).integers(0, 255, (300, 400, 3), dtype=np.uint8)
                (self.pbd / batch.id / "ORIGINAL" / name).write_bytes(cv2.imencode(".png", img)[1].tobytes())
                photo = PhotoBatchPhoto(batch_id=batch.id, filename=name, drive_file_id="",
                                        original_path=f"{batch.storage_dir}/ORIGINAL/{name}",
                                        media_path=f"{batch.storage_dir}/MEDIA/{name}", faces_total=2, classification="sorted")
                session.add(photo)
                session.add(PhotoBatchFace(photo_id=photo.id, person_id=person, confidence=0.9,
                                           bbox=",".join(f"{v:.1f}" for v in BOXES[0]),
                                           consent_status_at_processing="consented"))
                session.add(PhotoBatchFace(photo_id=photo.id, person_id=None, confidence=-1.0,
                                           bbox=",".join(f"{v:.1f}" for v in BOXES[1]),
                                           consent_status_at_processing="no_match"))
            session.add(batch)
            session.commit()
            self.batch_ids.append(batch.id)
            return batch.id

    def backfill(self, **kw):
        kw.setdefault("busy", lambda engine: None)
        kw.setdefault("hw_probe", _hw())
        return bf.start(engine=self.engine, storage_path=self.storage, photo_batches_dir=self.pbd,
                        builder=fake_builder, run_inline=True, **kw)

    def candidates(self):
        with Session(self.engine) as session:
            return session.exec(select(EventIdentityCandidate)).all()


class GuardTests(BackfillCase):
    def test_refuses_without_a_usable_cuda_gpu(self):
        with self.assertRaises(bf.BackfillRefused):
            self.backfill(hw_probe=_hw(cuda=False))
        self.assertEqual(bf.status()["state"], "refused")

    def test_refuses_when_free_vram_is_below_need_plus_margin(self):
        with self.assertRaises(bf.BackfillRefused):
            self.backfill(hw_probe=_hw(free=bf.VRAM_NEED_MB + bf.VRAM_MARGIN_MB - 1))

    def test_aborts_if_loading_the_model_left_too_little_headroom(self):
        self.make_batch()
        readings = iter([8000, bf.VRAM_MARGIN_MB - 1])
        probe = lambda: SimpleNamespace(usable_providers=["CUDAExecutionProvider"],
                                        gpus=[SimpleNamespace(free_vram_mb=next(readings))])
        self.backfill(hw_probe=probe)
        self.assertEqual(bf.status()["state"], "refused")
        self.assertEqual(self.candidates(), [])

    def test_the_bound_is_validated(self):
        for bad in (0, bf.MAX_PHOTOS_CAP + 1):
            with self.assertRaises(bf.BackfillRefused):
                self.backfill(max_photos=bad)

    def test_only_one_job_at_a_time(self):
        bf._status["state"] = "running"
        with self.assertRaises(bf.BackfillRefused):
            self.backfill()


class RunTests(BackfillCase):
    def test_backfills_candidates_linked_to_the_stored_faces_and_predictions(self):
        self.make_batch(photos=2)
        self.backfill()
        st = bf.status()
        self.assertEqual((st["state"], st["processed"], st["captured"]), ("done", 2, 4))
        self.assertIsNotNone(st["vram_delta_mb"])
        with Session(self.engine) as session:
            face_ids = {f.id for f in session.exec(select(PhotoBatchFace)).all()}
        rows = self.candidates()
        self.assertEqual(len(rows), 4)
        self.assertEqual({r.face_id for r in rows}, face_ids)
        self.assertEqual(sorted(str(r.predicted_person_id) for r in rows), ["None", "None", "p-alice", "p-alice"],
                         "predictions are copied from the stored face rows — no re-matching")

    def test_is_bounded_and_resumable_without_duplicates(self):
        self.make_batch(photos=3)
        self.backfill(max_photos=2)
        self.assertEqual(bf.status()["processed"], 2)
        self.backfill(max_photos=10)
        self.assertEqual(bf.status()["processed"], 1, "only the remaining photo")
        self.backfill(max_photos=10)
        self.assertEqual(bf.status()["total"], 0, "nothing left to do")
        self.assertEqual(len(self.candidates()), 6)

    def test_only_idle_batches_and_the_requested_batch_are_touched(self):
        busy_batch = self.make_batch(photos=1, status="processing")
        wanted = self.make_batch(photos=1)
        other = self.make_batch(photos=1)
        self.backfill(batch_id=wanted)
        self.assertEqual({r.batch_id for r in self.candidates()}, {wanted})
        self.backfill()
        self.assertNotIn(busy_batch, {r.batch_id for r in self.candidates()})
        self.assertIn(other, {r.batch_id for r in self.candidates()})

    def test_pauses_while_busy_then_continues(self):
        self.make_batch(photos=1)
        reasons = iter(["an Event batch is processing", "a kiosk recognition happened recently", None])
        seen = []

        def busy(engine):
            reason = next(reasons, None)
            seen.append(bf.status()["state"])
            return reason

        with patch.object(bf, "PAUSE_POLL_S", 0.0):
            self.backfill(busy=busy)
        self.assertIn("paused", seen)
        self.assertEqual(bf.status()["state"], "done")

    def test_cancel_stops_between_photos(self):
        self.make_batch(photos=3)
        calls = {"n": 0}

        def busy(engine):
            calls["n"] += 1
            if calls["n"] == 2:
                bf.cancel()
            return None

        self.backfill(busy=busy)
        self.assertEqual(bf.status()["state"], "cancelled")
        self.assertEqual(bf.status()["processed"], 1)

    def test_a_missing_original_is_skipped_not_fatal(self):
        batch_id = self.make_batch(photos=2)
        (self.pbd / batch_id / "ORIGINAL" / "p0.png").unlink()
        self.backfill()
        self.assertEqual(bf.status()["state"], "done")
        self.assertEqual({r.face_index for r in self.candidates()}, {0, 1})
        self.assertEqual(len(self.candidates()), 2)


class SummaryTests(BackfillCase):
    def test_summary_is_counts_only(self):
        self.make_batch(photos=1)
        self.backfill()
        s = bf.summary(self.engine)
        self.assertEqual(s["candidates"], 2)
        self.assertEqual(s["trusted_samples"], 0)
        self.assertFalse(s["promotion_enabled"])
        self.assertNotIn("embedding", repr(s))


if __name__ == "__main__":
    unittest.main()
