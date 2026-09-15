"""Phase A2 — Local-upload PhotoSource.

Covers the pure sanitize/dedupe helpers (traversal, empty names, collisions)
plus the REAL _run_photo_batch end to end for a source_type="local" batch
against an isolated temp DB + temp storage — no Drive faking needed at all,
since LocalUploadPhotoSource just reads real files from a real staging
directory, proving the accept/reject/finalize pipeline is genuinely
source-agnostic (the same claim Phase A1 made for Drive).
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine, select

import app.services.photo_processing_service as pps
from app.models.models import PhotoBatch, PhotoBatchIngestionIssue
from app.services.photo_source import (
    LocalUploadPhotoSource,
    dedupe_upload_basename,
    sanitize_upload_basename,
)


def _png_bytes(seed: int) -> bytes:
    img = np.random.default_rng(seed).integers(0, 255, (40, 40, 3), dtype=np.uint8)
    return cv2.imencode(".png", img)[1].tobytes()


class SanitizeBasenameTests(unittest.TestCase):
    def test_plain_name_unchanged(self):
        self.assertEqual(sanitize_upload_basename("IMG_0001.jpg"), "IMG_0001.jpg")

    def test_folder_upload_path_reduced_to_basename(self):
        # Browsers send a relative path (webkitRelativePath-style) for a
        # folder upload; only the trailing filename is a real "name".
        self.assertEqual(sanitize_upload_basename("Event Folder/subdir/IMG_0002.jpg"), "IMG_0002.jpg")

    def test_windows_style_separators_reduced_to_basename(self):
        self.assertEqual(sanitize_upload_basename("a\\b\\IMG_0003.jpg"), "IMG_0003.jpg")

    def test_traversal_attempt_reduced_to_harmless_basename(self):
        self.assertEqual(sanitize_upload_basename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_upload_basename("..\\..\\Windows\\win.ini"), "win.ini")

    def test_empty_or_dot_only_names_rejected(self):
        for bad in ("", "   ", ".", "..", "a/.", "a/.."):
            with self.assertRaises(ValueError):
                sanitize_upload_basename(bad)


class DedupeBasenameTests(unittest.TestCase):
    def test_first_occurrence_unchanged(self):
        taken: set[str] = set()
        self.assertEqual(dedupe_upload_basename("a.jpg", taken), "a.jpg")

    def test_collision_suffixed_before_extension(self):
        taken = {"a.jpg"}
        self.assertEqual(dedupe_upload_basename("a.jpg", taken), "a_1.jpg")

    def test_repeated_collision_increments(self):
        taken = {"a.jpg", "a_1.jpg"}
        self.assertEqual(dedupe_upload_basename("a.jpg", taken), "a_2.jpg")

    def test_collision_without_extension(self):
        taken = {"photo"}
        self.assertEqual(dedupe_upload_basename("photo", taken), "photo_1")

    def test_mutates_taken_set(self):
        taken: set[str] = set()
        dedupe_upload_basename("a.jpg", taken)
        self.assertIn("a.jpg", taken)


class LocalUploadPhotoSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-local-source-test-")
        self.addCleanup(self.temp.cleanup)
        self.staging = Path(self.temp.name)

    def test_list_items_returns_staged_files_sorted(self):
        (self.staging / "b.jpg").write_bytes(b"b")
        (self.staging / "a.jpg").write_bytes(b"a")
        source = LocalUploadPhotoSource(self.staging)
        names = [i.name for i in source.list_items()]
        self.assertEqual(names, ["a.jpg", "b.jpg"])

    def test_fetch_returns_exact_bytes(self):
        (self.staging / "a.jpg").write_bytes(b"hello")
        source = LocalUploadPhotoSource(self.staging)
        [item] = source.list_items()
        self.assertEqual(source.fetch(item), b"hello")

    def test_fetch_missing_item_raises(self):
        source = LocalUploadPhotoSource(self.staging)
        from app.services.photo_source import LocalUploadItem
        with self.assertRaises(FileNotFoundError):
            source.fetch(LocalUploadItem(id="nope.jpg", name="nope.jpg"))

    def test_list_items_ignores_subdirectories(self):
        (self.staging / "sub").mkdir()
        (self.staging / "a.jpg").write_bytes(b"a")
        source = LocalUploadPhotoSource(self.staging)
        self.assertEqual([i.name for i in source.list_items()], ["a.jpg"])


class LocalBatchLifecycleTestCase(unittest.TestCase):
    """Same isolated-environment scaffolding test_event_photo_completeness.py
    uses, but for source_type="local" batches: no Drive faking at all — the
    staging directory is populated directly, exactly as the /upload API
    route would have done before handing off to run_photo_batch()."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-local-lifecycle-test-")
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

        self.batch_id = "b" * 32
        with Session(self.engine) as session:
            batch = PhotoBatch(
                id=self.batch_id, label="local test", drive_folder_id="", source_type="local",
                storage_dir=f"photo_batches/{self.batch_id}",
            )
            session.add(batch)
            session.commit()

    def batch(self) -> PhotoBatch:
        with Session(self.engine) as session:
            return session.get(PhotoBatch, self.batch_id)

    def stage(self, files_bytes: dict[str, bytes]) -> None:
        staging = pps.upload_staging_dir(self.batch_id)
        staging.mkdir(parents=True, exist_ok=True)
        for name, data in files_bytes.items():
            (staging / name).write_bytes(data)

    def run_batch(self, detect_side_effect=None) -> None:
        detect_faces_mock = detect_side_effect if detect_side_effect is not None else (lambda img: [])
        with patch.object(pps, "detect_faces", side_effect=detect_faces_mock):
            pps._run_photo_batch(self.batch_id)


class LocalBatchCompletenessTests(LocalBatchLifecycleTestCase):
    def test_reaches_ready_with_correct_counts(self):
        self.stage({"good1.png": _png_bytes(1), "good2.png": _png_bytes(2), "corrupt.png": b"not an image"})
        self.run_batch()
        b = self.batch()
        self.assertEqual(b.status, "ready")
        self.assertEqual(b.total_photos, 3)
        self.assertEqual(b.processed_photos, 2)
        self.assertEqual(b.rejected_photos, 1)
        self.assertEqual(b.failed_photos, 0)

        media_dir = self.photo_batches_dir / self.batch_id / "MEDIA"
        self.assertEqual({p.name for p in media_dir.iterdir()}, {"good1.png", "good2.png"})

        with Session(self.engine) as session:
            issues = session.exec(
                select(PhotoBatchIngestionIssue).where(PhotoBatchIngestionIssue.batch_id == self.batch_id)
            ).all()
        self.assertEqual([i.filename for i in issues], ["corrupt.png"])

    def test_staging_directory_cleaned_up_after_ready(self):
        self.stage({"good1.png": _png_bytes(1)})
        self.run_batch()
        self.assertFalse(pps.upload_staging_dir(self.batch_id).exists())

    def test_staging_directory_cleaned_up_even_when_needs_retry(self):
        def flaky(img):
            raise RuntimeError("boom")

        self.stage({"only.png": _png_bytes(3)})
        self.run_batch(detect_side_effect=flaky)
        b = self.batch()
        self.assertEqual(b.status, "needs_retry")
        # Cleanup must not depend on reaching ready — retry re-reads ORIGINAL/,
        # never the staging area, so it's safe to remove as soon as the main
        # pass finishes regardless of outcome.
        self.assertFalse(pps.upload_staging_dir(self.batch_id).exists())

    def test_retry_after_needs_retry_succeeds_without_staging(self):
        calls = {"n": 0}

        def flaky_then_fine(img):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return []

        self.stage({"only.png": _png_bytes(4)})
        self.run_batch(detect_side_effect=flaky_then_fine)
        self.assertEqual(self.batch().status, "needs_retry")
        self.assertFalse(pps.upload_staging_dir(self.batch_id).exists())

        with patch.object(pps, "detect_faces", side_effect=lambda img: []):
            pps.retry_unresolved_photos(self.batch_id)
        self.assertEqual(self.batch().status, "ready")

    def test_all_rejected_reaches_failed_and_cleans_staging(self):
        self.stage({"corrupt1.png": b"nope", "corrupt2.png": b"also nope"})
        self.run_batch()
        b = self.batch()
        self.assertEqual(b.status, "failed")
        self.assertEqual(b.rejected_photos, 2)
        self.assertFalse(pps.upload_staging_dir(self.batch_id).exists())


class CreateLocalBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-create-local-batch-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.photo_batches_dir = self.storage / "photo_batches"
        db_path = Path(self.temp.name) / "test.db"

        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        for target, value in (("engine", self.engine), ("PHOTO_BATCHES_DIR", self.photo_batches_dir)):
            p = patch.object(pps, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_creates_row_with_local_source_type_and_directories(self):
        with Session(self.engine) as session:
            batch = pps.create_local_batch(session, "My Local Batch", 5, "user-1")
        self.assertEqual(batch.source_type, "local")
        self.assertEqual(batch.drive_folder_id, "")
        self.assertEqual(batch.label, "My Local Batch")
        self.assertEqual(batch.retention_days, 5)
        batch_dir = self.photo_batches_dir / batch.id
        for sub in ("ORIGINAL", "SORTED", "AMBIENCE", "MEDIA"):
            self.assertTrue((batch_dir / sub).is_dir())

    def test_invalid_retention_days_rejected(self):
        with Session(self.engine) as session:
            with self.assertRaises(ValueError):
                pps.create_local_batch(session, "x", 0, "user-1")
            with self.assertRaises(ValueError):
                pps.create_local_batch(session, "x", 8, "user-1")


if __name__ == "__main__":
    unittest.main()
