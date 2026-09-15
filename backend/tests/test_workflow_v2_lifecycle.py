"""Phase M2 — local_status/drive_status lifecycle and delete_at timing for
workflow_version>=2 batches. Runs the REAL pps.run_photo_batch() /
retry_unresolved_photos() end to end (Drive fetch/list faked, detection
real-or-faked per scenario) against an isolated temp DB/storage — the
same isolation pattern test_event_photo_completeness.py already uses.
"""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine

import app.services.photo_processing_service as pps
import app.services.event_pipeline_service as eps
from app.models.models import PhotoBatch


def _png_bytes(seed: int) -> bytes:
    img = np.random.default_rng(seed).integers(0, 255, (40, 40, 3), dtype=np.uint8)
    return cv2.imencode(".png", img)[1].tobytes()


def _fake_file(name: str):
    return SimpleNamespace(id=f"drive-{name}", name=name)


class LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-v2-lifecycle-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.photo_batches_dir = self.storage / "photo_batches"
        db_path = Path(self.temp.name) / "test.db"

        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        for target, value in (
            ("engine", self.engine), ("STORAGE_PATH", self.storage), ("PHOTO_BATCHES_DIR", self.photo_batches_dir),
        ):
            p = patch.object(pps, target, value)
            p.start()
            self.addCleanup(p.stop)

        patch.object(pps.settings_cache, "get_threshold", return_value=0.45).start()
        patch("app.services.maintenance.is_active", return_value=False).start()
        self.addCleanup(patch.stopall)

        self.batch_id = "a" * 32
        with Session(self.engine) as session:
            batch = PhotoBatch(
                id=self.batch_id, label="test", drive_folder_id="fake-folder",
                storage_dir=f"photo_batches/{self.batch_id}", workflow_version=2,
                retention_days=7, retention_start_at=datetime.now(),
                delete_at=datetime.now() + timedelta(days=7),  # creation-time placeholder
            )
            session.add(batch)
            session.commit()

    def batch(self) -> PhotoBatch:
        with Session(self.engine) as session:
            return session.get(PhotoBatch, self.batch_id)

    def run_batch(self, files_bytes, detect_side_effect=None):
        files = [_fake_file(name) for name in files_bytes]

        def fake_download(file_id):
            name = file_id.removeprefix("drive-")
            data = files_bytes[name]
            if data is None:
                raise RuntimeError("simulated Drive download failure")
            return data

        detect_faces_mock = detect_side_effect if detect_side_effect is not None else (lambda img: [])
        # workflow_version=2 always routes through EventPhotoPipeline, whose
        # detection call is eps.detect_event_faces — a SEPARATE reference
        # from pps.detect_faces (used only by the sequential path and by
        # retry_unresolved_photos). Patching pps.detect_faces here would
        # silently have no effect on what the pipeline actually calls.
        with patch.object(pps, "list_image_files", return_value=files), \
             patch.object(pps, "download_file", side_effect=fake_download), \
             patch.object(eps, "detect_event_faces", side_effect=detect_faces_mock):
            pps.run_photo_batch(self.batch_id)


class LocalStatusTransitionTests(LifecycleTestCase):
    def test_reaches_local_status_ready_on_success(self):
        deadline_before = self.batch().delete_at
        self.run_batch({"a.png": _png_bytes(1), "b.png": _png_bytes(2)})
        b = self.batch()
        self.assertEqual(b.status, "ready")
        self.assertEqual(b.local_status, "READY")
        # The deadline comes from the retention anchor, so reaching READY
        # leaves it exactly where it was.
        self.assertEqual(b.delete_at, deadline_before)

    def test_local_status_failed_when_everything_rejected(self):
        self.run_batch({"bad.png": b"not a real image"})
        b = self.batch()
        self.assertEqual(b.status, "failed")
        self.assertEqual(b.local_status, "FAILED")

    def test_local_status_stays_processing_while_needs_retry(self):
        def flaky(img):
            raise RuntimeError("boom")

        self.run_batch({"only.png": _png_bytes(3)}, detect_side_effect=flaky)
        b = self.batch()
        self.assertEqual(b.status, "needs_retry")
        self.assertEqual(b.local_status, "PROCESSING")

    def test_local_status_reaches_ready_after_retry_resolves(self):
        calls = {"n": 0}

        def flaky_then_fine(img):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return []

        self.run_batch({"only.png": _png_bytes(4)}, detect_side_effect=flaky_then_fine)
        self.assertEqual(self.batch().local_status, "PROCESSING")

        with patch.object(pps, "detect_faces", side_effect=lambda img: []):
            pps.retry_unresolved_photos(self.batch_id)
        b = self.batch()
        self.assertEqual(b.status, "ready")
        self.assertEqual(b.local_status, "READY")


class DeleteAtSetOnceTests(LifecycleTestCase):
    def test_delete_at_comes_from_the_retention_anchor_and_ready_never_moves_it(self):
        with Session(self.engine) as session:
            before = session.get(PhotoBatch, self.batch_id)
            deadline_before = before.delete_at

        self.run_batch({"a.png": _png_bytes(1)})
        self.assertEqual(self.batch().delete_at, deadline_before,
                         "retention counts from when it was applied — reaching READY must not re-anchor it")

    def test_delete_at_not_moved_by_a_retry_that_reaches_ready(self):
        """A batch stuck in needs_retry for a while, THEN resolved, keeps the
        deadline it was given when retention was applied — a late READY must
        not buy it a longer life."""
        def flaky(img):
            raise RuntimeError("boom")

        self.run_batch({"only.png": _png_bytes(5)}, detect_side_effect=flaky)
        needs_retry_delete_at = self.batch().delete_at

        with patch.object(pps, "detect_faces", side_effect=lambda img: []):
            pps.retry_unresolved_photos(self.batch_id)

        self.assertEqual(self.batch().local_status, "READY")
        self.assertEqual(self.batch().delete_at, needs_retry_delete_at)

    def test_download_never_touches_delete_at(self):
        """Downloading (reading finalized output) must never be able to
        reset delete_at — there is no code path from the download service
        into PhotoBatch.delete_at at all; this asserts that invariant by
        construction: local_output_ready()/output_manifest() are read-only."""
        import inspect
        from app.services import photo_batch_download_service as download

        source = inspect.getsource(download)
        self.assertNotIn("delete_at", source, "the download service must never write delete_at")

    def test_delete_at_never_recomputed_on_a_second_finalize_to_ready(self):
        """Defensive: even if _finalize_batch_status were ever invoked
        again on an already-READY batch (not possible in today's flow),
        delete_at must not move a second time."""
        self.run_batch({"a.png": _png_bytes(1)})
        first = self.batch()
        first_delete_at = first.delete_at
        with Session(self.engine) as session:
            b = session.get(PhotoBatch, self.batch_id)
            pps._finalize_batch_status(b)
            session.add(b)
            session.commit()
        second_delete_at = self.batch().delete_at
        self.assertEqual(first_delete_at, second_delete_at)


if __name__ == "__main__":
    unittest.main()
