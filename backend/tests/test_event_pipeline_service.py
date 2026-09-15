"""Phase B — Continuous Processing Pipeline.

Runs the REAL EventPhotoPipeline (all 5 stages, real threads, real bounded
queues) against an isolated temp DB/storage, with only the external
boundaries (Drive fetch, detection, matching) faked — same isolation
philosophy as the sequential-path tests. Confirms: correct final state for
a mixed batch (ambience/sorted/review/rejected/failed), max_inflight is
never exceeded, cancellation terminates promptly with zero orphaned
threads, and the registry correctly reports/clears activity.
"""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine, select

import app.services.event_pipeline_service as eps
from app.services.event_pipeline_registry import registry as pipeline_registry
from app.models.models import ConsentRecord, Person, PhotoBatch, PhotoBatchFace, PhotoBatchIngestionIssue, PhotoBatchPhoto


def _image(seed: int, size=(120, 160)) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 255, (*size, 3), dtype=np.uint8)


def _fake_face(seed: int, bbox):
    return SimpleNamespace(embedding=np.zeros(512, dtype=np.float32) + seed, bbox=bbox)


def _fake_source_item(name: str):
    return SimpleNamespace(id=f"src-{name}", name=name)


class FakeSource:
    """Mirrors PhotoSource's shape (list_items()/fetch()) — Fetch stage
    calls .fetch(item), never anything Drive-specific."""

    def __init__(self, files: dict[str, bytes | None]):
        self.files = files

    def fetch(self, item):
        data = self.files[item.name]
        if data is None:
            raise RuntimeError("simulated fetch failure")
        return data


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-pipeline-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.photo_batches_dir = self.storage / "photo_batches"
        db_path = Path(self.temp.name) / "test.db"

        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        self.batch_id = "a" * 32
        self.batch_storage_dir = f"photo_batches/{self.batch_id}"
        (self.photo_batches_dir / self.batch_id).mkdir(parents=True)
        for sub in ("ORIGINAL", "SORTED", "AMBIENCE", "MEDIA"):
            (self.photo_batches_dir / self.batch_id / sub).mkdir()

        with Session(self.engine) as session:
            dummy_embedding = np.zeros(512, dtype=np.float32).tobytes()
            self.person_a = Person(participant_id="0001", first_name="Aiko", last_name="", image_path="", embedding=dummy_embedding)
            session.add(self.person_a)
            session.commit()
            session.add(ConsentRecord(person_id=self.person_a.id, choice="consented"))
            session.add(PhotoBatch(id=self.batch_id, label="test", drive_folder_id="fake", storage_dir=self.batch_storage_dir))
            session.commit()
            session.refresh(self.person_a)

    def make_pipeline(self, source, profile=None) -> eps.EventPhotoPipeline:
        return eps.EventPhotoPipeline(
            self.batch_id, source, logo_config=None, threshold=0.45,
            is_cancelled=lambda _bid: False, profile=profile,
            storage_path=self.storage, photo_batches_dir=self.photo_batches_dir, db_engine=self.engine,
        )

    def batch(self) -> PhotoBatch:
        with Session(self.engine) as session:
            return session.get(PhotoBatch, self.batch_id)

    def photos(self) -> list[PhotoBatchPhoto]:
        with Session(self.engine) as session:
            return session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == self.batch_id)).all()


class BenchmarkProfileTests(unittest.TestCase):
    def test_hard_floor_and_ceiling_regardless_of_hardware(self):
        tiny = SimpleNamespace(cpu_cores=1, free_ram_mb=1)
        huge = SimpleNamespace(cpu_cores=256, free_ram_mb=1_000_000)
        p_tiny = eps.benchmark_profile(tiny)
        p_huge = eps.benchmark_profile(huge)
        for p in (p_tiny, p_huge):
            self.assertGreaterEqual(p["fetch_workers"], 1)
            self.assertGreaterEqual(p["decode_workers"], 1)
            self.assertGreaterEqual(p["render_workers"], 1)
            self.assertGreaterEqual(p["max_inflight"], 2)
        self.assertLessEqual(p_huge["fetch_workers"], 4)
        self.assertLessEqual(p_huge["decode_workers"], 3)
        self.assertLessEqual(p_huge["render_workers"], 4)
        self.assertLessEqual(p_huge["max_inflight"], 10)


class MixedBatchIntegrationTests(PipelineTestCase):
    """One run covering every outcome the plan's test list requires:
    ambience, sorted (consented, unblurred), review (unknown face),
    rejected (corrupt bytes), failed (inference raises)."""

    def test_mixed_outcomes_produce_correct_final_state(self):
        good_ambience = cv2.imencode(".png", _image(1))[1].tobytes()
        good_sorted_img = _image(2)
        good_sorted = cv2.imencode(".png", good_sorted_img)[1].tobytes()
        good_review_img = _image(3)
        good_review = cv2.imencode(".png", good_review_img)[1].tobytes()
        good_failing_img = _image(4)
        good_failing = cv2.imencode(".png", good_failing_img)[1].tobytes()

        source = FakeSource({
            "ambience.png": good_ambience,
            "sorted.png": good_sorted,
            "review.png": good_review,
            "corrupt.png": b"not a real image",
            "failing.png": good_failing,
        })

        def fake_detect(img):
            if np.array_equal(img, good_sorted_img):
                return [_fake_face(1, (10.0, 10.0, 50.0, 50.0))]
            if np.array_equal(img, good_review_img):
                return [_fake_face(2, (10.0, 10.0, 50.0, 50.0))]
            if np.array_equal(img, good_failing_img):
                raise RuntimeError("simulated inference failure")
            return []  # ambience.png

        def fake_match(embeddings, threshold):
            # Only ever called with exactly one embedding in this test.
            if embeddings[0][0] == 1.0:
                return [(self.person_a.id, "Aiko", "Aiko", "0001", 0.9)]
            return [(None, None, None, None, -1.0)]

        items = [
            (1, _fake_source_item("ambience.png"), "ambience.png"),
            (2, _fake_source_item("sorted.png"), "sorted.png"),
            (3, _fake_source_item("review.png"), "review.png"),
            (4, _fake_source_item("corrupt.png"), "corrupt.png"),
            (5, _fake_source_item("failing.png"), "failing.png"),
        ]

        pipeline = self.make_pipeline(source)
        with patch.object(eps, "detect_event_faces", side_effect=fake_detect), \
             patch.object(eps.recognition_index, "match_batch", side_effect=fake_match):
            pipeline.run(items, self.batch_storage_dir, self.batch_id)

        batch = self.batch()
        self.assertEqual(batch.total_photos, 0)  # this pipeline unit test never sets it — the orchestrator (_run_photo_batch_pipeline) does
        self.assertEqual(batch.rejected_photos, 1)
        self.assertEqual(batch.failed_photos, 1)
        self.assertEqual(batch.processed_photos, 3)
        self.assertEqual(batch.ambience_photos, 1)
        # Both faced photos are delivered normally; only the one with a
        # recognized participant counts as recognized. The old "review"
        # bucket (any unmatched face) is gone along with the workflow.
        self.assertEqual(batch.recognized_photos, 1)
        self.assertEqual(batch.review_photos, 0)

        issues = []
        with Session(self.engine) as session:
            issues = session.exec(select(PhotoBatchIngestionIssue).where(PhotoBatchIngestionIssue.batch_id == self.batch_id)).all()
        self.assertEqual([i.filename for i in issues], ["corrupt.png"])

        photos = {p.filename: p for p in self.photos()}
        self.assertIsNone(photos["failing.png"].media_path)  # failed — never fabricated
        self.assertNotIn("corrupt.png", photos)  # rejected — no row at all
        self.assertEqual(photos["sorted.png"].classification, "sorted")
        self.assertEqual(photos["review.png"].classification, "sorted",
                         "an unrecognized face no longer routes anywhere special")
        self.assertEqual(photos["ambience.png"].classification, "ambience")
        self.assertFalse((self.storage / f"{self.batch_storage_dir}/REVIEW").exists(),
                         "no REVIEW/ output is written for a new batch")

        # Phase I4/J1 — one thumbnail per finalized photo, from the rendered
        # bytes, in the central THUMBNAILS/ folder.
        thumbs = sorted(p.name for p in (self.storage / f"{self.batch_storage_dir}/THUMBNAILS").iterdir())
        self.assertEqual(thumbs, ["ambience.png.jpg", "review.png.jpg", "sorted.png.jpg"])

        # D0 fan-out still holds under the pipeline: SORTED == MEDIA bytes.
        media_bytes = (self.storage / f"{self.batch_storage_dir}/MEDIA/sorted.png").read_bytes()
        sorted_bytes = (self.storage / f"{self.batch_storage_dir}/SORTED/0001_Aiko/sorted.png").read_bytes()
        self.assertEqual(media_bytes, sorted_bytes)

    def test_registry_activity_during_and_after_run(self):
        self.assertFalse(pipeline_registry.is_active(self.batch_id))
        source = FakeSource({"only.png": cv2.imencode(".png", _image(9))[1].tobytes()})
        pipeline = self.make_pipeline(source)
        # Registration happens at construction (not deferred to run()) so a
        # cancel/retention/update-preflight check can never race a narrow
        # window between "pipeline object exists" and "pipeline is running".
        self.assertTrue(pipeline_registry.is_active(self.batch_id))
        with patch.object(eps, "detect_event_faces", return_value=[]):
            pipeline.run([(1, _fake_source_item("only.png"), "only.png")], self.batch_storage_dir, self.batch_id)
        self.assertFalse(pipeline_registry.is_active(self.batch_id), "registry must clear itself once run() returns")


class MaxInflightTests(PipelineTestCase):
    def test_max_inflight_is_never_exceeded(self):
        n_items = 12
        files = {f"p{i}.png": cv2.imencode(".png", _image(i))[1].tobytes() for i in range(n_items)}
        source = FakeSource(files)
        profile = {"fetch_workers": 3, "decode_workers": 2, "render_workers": 3, "max_inflight": 3}
        pipeline = self.make_pipeline(source, profile=profile)
        items = [(i, _fake_source_item(name), name) for i, name in enumerate(files)]

        # Slow down render so items pile up before DBCommit drains them —
        # this is what makes exceeding max_inflight actually observable.
        real_apply_logo = pipeline._apply_logo

        def slow_apply_logo(*a, **kw):
            time.sleep(0.02)
            return real_apply_logo(*a, **kw)

        pipeline._apply_logo = slow_apply_logo

        with patch.object(eps, "detect_event_faces", return_value=[]):
            pipeline.run(items, self.batch_storage_dir, self.batch_id)

        self.assertLessEqual(pipeline.max_observed_in_flight, profile["max_inflight"])
        self.assertEqual(self.batch().processed_photos, n_items)


class CancellationTests(PipelineTestCase):
    def test_cancellation_stops_promptly_with_no_orphaned_threads(self):
        n_items = 20
        files = {f"p{i}.png": cv2.imencode(".png", _image(i))[1].tobytes() for i in range(n_items)}
        source = FakeSource(files)
        profile = {"fetch_workers": 2, "decode_workers": 2, "render_workers": 2, "max_inflight": 2}
        pipeline = self.make_pipeline(source, profile=profile)
        items = [(i, _fake_source_item(name), name) for i, name in enumerate(files)]

        def slow_detect(img):
            time.sleep(0.05)
            return []

        result = {}

        def run_it():
            with patch.object(eps, "detect_event_faces", side_effect=slow_detect):
                pipeline.run(items, self.batch_storage_dir, self.batch_id)
            result["done"] = True

        before_threads = set(threading.enumerate())
        t = threading.Thread(target=run_it)
        started = time.monotonic()
        t.start()
        time.sleep(0.05)  # let a couple items get admitted
        pipeline.handle.cancel()
        t.join(timeout=10)
        elapsed = time.monotonic() - started

        self.assertTrue(result.get("done"), "pipeline.run() must return once cancelled, not hang")
        self.assertLess(elapsed, n_items * 0.05, "cancellation must cut the run short, not process everything anyway")

        # No pipeline-spawned threads left alive.
        leftover = set(threading.enumerate()) - before_threads
        leftover = {th for th in leftover if th is not t and th.is_alive()}
        self.assertEqual(leftover, set(), f"orphaned threads after cancellation: {leftover}")

        self.assertFalse(pipeline_registry.is_active(self.batch_id))
        # Fewer than the full set were ever committed — proof it actually stopped early.
        self.assertLess(self.batch().processed_photos + self.batch().failed_photos + self.batch().rejected_photos, n_items)


if __name__ == "__main__":
    unittest.main()
