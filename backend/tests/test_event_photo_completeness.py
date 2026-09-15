"""Phase N1 — Output Completeness Invariant.

Runs the REAL _run_photo_batch / _process_accepted_photo /
retry_unresolved_photos against an isolated temp DB + temp storage, with
only the external boundaries (Drive, detection, matching, threshold) faked
— this exercises the actual production control flow rather than
reimplementing the policy it's meant to verify.
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

import app.services.photo_processing_service as pps
from app.models.models import PhotoBatch, PhotoBatchIngestionIssue, PhotoBatchPhoto
from app.services.photo_batch_download_service import local_output_ready


def _png_bytes(seed: int) -> bytes:
    img = np.random.default_rng(seed).integers(0, 255, (40, 40, 3), dtype=np.uint8)
    return cv2.imencode(".png", img)[1].tobytes()


def _fake_file(name: str):
    return SimpleNamespace(id=f"drive-{name}", name=name)


class CompletenessTestCase(unittest.TestCase):
    """Shared isolated-environment scaffolding. Never touches the real
    STORAGE_PATH/DATABASE_PATH/engine — everything lives under one temp dir
    for the lifetime of the test, patched into photo_processing_service's
    module namespace (the same names its Session(engine)/STORAGE_PATH calls
    resolve at call time)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-completeness-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.photo_batches_dir = self.storage / "photo_batches"
        db_path = Path(self.temp.name) / "test.db"

        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        for target, value in (
            ("engine", self.engine),
            ("STORAGE_PATH", self.storage),
            ("PHOTO_BATCHES_DIR", self.photo_batches_dir),
        ):
            p = patch.object(pps, target, value)
            p.start()
            self.addCleanup(p.stop)

        threshold_patch = patch.object(pps.settings_cache, "get_threshold", return_value=0.45)
        threshold_patch.start()
        self.addCleanup(threshold_patch.stop)

        maintenance_patch = patch("app.services.maintenance.is_active", return_value=False)
        maintenance_patch.start()
        self.addCleanup(maintenance_patch.stop)

        self.batch_id = "a" * 32
        with Session(self.engine) as session:
            batch = PhotoBatch(
                id=self.batch_id, label="test", drive_folder_id="fake-folder",
                storage_dir=f"photo_batches/{self.batch_id}",
            )
            session.add(batch)
            session.commit()

    def batch(self) -> PhotoBatch:
        with Session(self.engine) as session:
            return session.get(PhotoBatch, self.batch_id)

    def issues(self) -> list[PhotoBatchIngestionIssue]:
        with Session(self.engine) as session:
            return session.exec(
                select(PhotoBatchIngestionIssue).where(PhotoBatchIngestionIssue.batch_id == self.batch_id)
            ).all()

    def photos(self) -> list[PhotoBatchPhoto]:
        with Session(self.engine) as session:
            return session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == self.batch_id)).all()

    def run_batch(self, files_bytes: dict[str, bytes | None], detect_side_effect=None):
        """files_bytes: {filename: bytes} — a value of None means download()
        itself raises (simulating a Drive read failure); real bytes that
        fail to decode simulate a corrupt file. Both land in rejected_photos.
        """
        files = [_fake_file(name) for name in files_bytes]

        def fake_download(file_id: str) -> bytes:
            name = file_id.removeprefix("drive-")
            data = files_bytes[name]
            if data is None:
                raise RuntimeError("simulated Drive download failure")
            return data

        detect_faces_mock = detect_side_effect if detect_side_effect is not None else (lambda img: [])

        with patch.object(pps, "list_image_files", return_value=files), \
             patch.object(pps, "download_file", side_effect=fake_download), \
             patch.object(pps, "detect_faces", side_effect=detect_faces_mock):
            pps._run_photo_batch(self.batch_id)


class RejectionVsFailureTests(CompletenessTestCase):
    def test_pre_acceptance_rejection_never_touches_failed_or_processed(self):
        self.run_batch({
            "good1.png": _png_bytes(1),
            "corrupt.png": b"not a real image",
            "good2.png": _png_bytes(2),
        })
        batch = self.batch()
        self.assertEqual(batch.rejected_photos, 1)
        self.assertEqual(batch.failed_photos, 0)
        self.assertEqual(batch.processed_photos, 2)
        self.assertEqual(batch.total_photos, 3)
        self.assertEqual(batch.status, "ready")

        issues = self.issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].filename, "corrupt.png")
        self.assertTrue(issues[0].reason)

        # A rejected item never gets a PhotoBatchPhoto row at all — it can
        # never be retried into existence.
        filenames = {p.filename for p in self.photos()}
        self.assertNotIn("corrupt.png", filenames)

    def test_drive_download_failure_is_also_a_rejection(self):
        self.run_batch({"good.png": _png_bytes(1), "missing.png": None})
        batch = self.batch()
        self.assertEqual(batch.rejected_photos, 1)
        self.assertEqual(batch.failed_photos, 0)
        self.assertEqual([i.filename for i in self.issues()], ["missing.png"])

    def test_post_acceptance_failure_increments_failed_not_processed(self):
        """The literal historical bug (917 accepted / 854 MEDIA / 63 missing):
        a post-acceptance exception must NEVER also increment
        processed_photos — only failed_photos."""
        calls = {"n": 0}

        def flaky_detect(img):
            calls["n"] += 1
            if calls["n"] == 2:  # fail exactly the second accepted photo
                raise RuntimeError("simulated detection failure")
            return []

        self.run_batch(
            {"good1.png": _png_bytes(1), "good2.png": _png_bytes(2), "good3.png": _png_bytes(3)},
            detect_side_effect=flaky_detect,
        )
        batch = self.batch()
        self.assertEqual(batch.rejected_photos, 0)
        self.assertEqual(batch.failed_photos, 1)
        self.assertEqual(batch.processed_photos, 2)  # NOT 3 — the exact regression this phase fixes
        self.assertEqual(batch.status, "needs_retry")
        self.assertIn("good2.png", batch.last_error)

        # The failed photo has a PhotoBatchPhoto row (accepted) but no
        # media_path (never fabricated) — never silently dropped.
        failed_row = next(p for p in self.photos() if p.filename == "good2.png")
        self.assertIsNone(failed_row.media_path)
        self.assertFalse((self.storage / f"{batch.storage_dir}/MEDIA/good2.png").exists())

    def test_during_processing_inequality_never_inverts(self):
        """Snapshots the live DB state right as each accepted photo is about
        to be finalized — the moment BEFORE that photo's own counters land —
        proving the inequality holds mid-batch, not just once everything
        finishes."""
        snapshots = []

        def observing_detect(img):
            snapshots.append((self.batch().processed_photos, self.batch().failed_photos))
            return []

        self.run_batch(
            {f"p{i}.png": _png_bytes(i) for i in range(5)},
            detect_side_effect=observing_detect,
        )
        accepted = self.batch().total_photos - self.batch().rejected_photos
        self.assertEqual(len(snapshots), 5)
        for processed, failed in snapshots:
            self.assertLessEqual(processed + failed, accepted)
        # After the main pass completes, the invariant is an equality.
        final = self.batch()
        self.assertEqual(final.processed_photos + final.failed_photos, accepted)

    def test_never_fabricates_media_when_everything_is_rejected(self):
        """A batch where every source item is rejected must produce ZERO
        MEDIA files — no fallback path may ever copy an unprocessed
        ORIGINAL into MEDIA just to have something to show."""
        self.run_batch({"bad1.png": b"garbage", "bad2.png": b"also garbage"})
        batch = self.batch()
        self.assertEqual(batch.rejected_photos, 2)
        self.assertEqual(batch.processed_photos, 0)
        self.assertEqual(batch.failed_photos, 0)
        media_dir = self.storage / f"{batch.storage_dir}/MEDIA"
        self.assertEqual(list(media_dir.iterdir()) if media_dir.exists() else [], [])


class LocalOutputReadyTests(CompletenessTestCase):
    def test_ready_true_with_rejected_photos_present(self):
        """The exact regression this phase's correction targets: an earlier
        pass claimed no code change was needed here, which would have
        wrongly denied a fully-completed batch that has any rejections."""
        self.run_batch({
            "good1.png": _png_bytes(1),
            "corrupt.png": b"garbage",
            "good2.png": _png_bytes(2),
        })
        batch = self.batch()
        self.assertGreater(batch.rejected_photos, 0)
        self.assertTrue(local_output_ready(batch, self.storage))

    def test_ready_false_while_failed_photos_outstanding(self):
        def flaky_detect(img):
            raise RuntimeError("always fails")

        self.run_batch({"only.png": _png_bytes(1)}, detect_side_effect=flaky_detect)
        batch = self.batch()
        self.assertEqual(batch.failed_photos, 1)
        self.assertFalse(local_output_ready(batch, self.storage))

    def test_ready_false_when_still_processing(self):
        batch = PhotoBatch(
            id="b" * 32, label="mid", drive_folder_id="fake", storage_dir=f"photo_batches/{'b'*32}",
            status="processing", total_photos=5, processed_photos=2,
        )
        self.assertFalse(local_output_ready(batch, self.storage))

    def test_ready_false_when_media_count_short(self):
        """Defense-in-depth: DB says complete but disk is short a file —
        must not be trusted blindly."""
        self.run_batch({"good1.png": _png_bytes(1), "good2.png": _png_bytes(2)})
        batch = self.batch()
        self.assertTrue(local_output_ready(batch, self.storage))
        media_dir = self.storage / f"{batch.storage_dir}/MEDIA"
        next(media_dir.iterdir()).unlink()  # delete one on-disk MEDIA file
        self.assertFalse(local_output_ready(batch, self.storage))


class RetryUnresolvedTests(CompletenessTestCase):
    def test_retry_resolves_failed_photo_without_touching_others(self):
        calls = {"n": 0}

        def flaky_then_fine(img):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first attempt fails")
            return []

        self.run_batch(
            {"first.png": _png_bytes(1), "second.png": _png_bytes(2)},
            detect_side_effect=flaky_then_fine,
        )
        batch = self.batch()
        self.assertEqual(batch.status, "needs_retry")
        self.assertEqual(batch.failed_photos, 1)

        second_media_before = (self.storage / f"{batch.storage_dir}/MEDIA/second.png").read_bytes()

        with patch.object(pps, "detect_faces", side_effect=lambda img: []):
            pps._retry_unresolved_photos(self.batch_id)

        batch = self.batch()
        self.assertEqual(batch.failed_photos, 0)
        self.assertEqual(batch.processed_photos, 2)
        self.assertEqual(batch.status, "ready")

        first_row = next(p for p in self.photos() if p.filename == "first.png")
        self.assertIsNotNone(first_row.media_path)

        # The already-finalized photo must be byte-identical — retry only
        # ever touches the unresolved set.
        second_media_after = (self.storage / f"{batch.storage_dir}/MEDIA/second.png").read_bytes()
        self.assertEqual(second_media_before, second_media_after)

    def test_retry_never_touches_rejected_photos(self):
        self.run_batch({"good.png": _png_bytes(1), "corrupt.png": b"garbage"})
        batch = self.batch()
        self.assertEqual(batch.rejected_photos, 1)
        self.assertEqual(batch.failed_photos, 0)  # nothing to retry
        with patch.object(pps, "detect_faces", side_effect=lambda img: []):
            pps._retry_unresolved_photos(self.batch_id)  # must be a safe no-op
        batch = self.batch()
        self.assertEqual(batch.rejected_photos, 1)
        self.assertEqual(len(self.issues()), 1)


if __name__ == "__main__":
    unittest.main()
