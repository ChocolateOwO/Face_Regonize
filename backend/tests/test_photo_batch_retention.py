"""Phase M1 — Retention narrow fix.

run_retention_cleanup must never delete a batch that is still actively or
meaningfully engaged with: an in-flight/failed Drive upload, or a batch
sitting in needs_retry awaiting retry-unresolved (Phase N1). Runs the real
function against an isolated temp DB/storage.
"""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine, select

import app.services.photo_processing_service as pps
from app.services import batch_edit_lock
from app.services import drive_destination_service as dds
from app.models.models import CleanupLog, PhotoBatch


class RetentionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-retention-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.storage.mkdir()
        db_path = Path(self.temp.name) / "test.db"

        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        for target, value in (("engine", self.engine), ("STORAGE_PATH", self.storage)):
            p = patch.object(pps, target, value)
            p.start()
            self.addCleanup(p.stop)

        maintenance_patch = patch("app.services.maintenance.is_active", return_value=False)
        maintenance_patch.start()
        self.addCleanup(maintenance_patch.stop)

    def make_batch(self, batch_id: str, status: str, expired: bool = True, workflow_version: int = 1,
                   local_status: str = "CREATED", drive_status: str = "NOT_UPLOADED") -> PhotoBatch:
        storage_dir = f"photo_batches/{batch_id}"
        (self.storage / storage_dir).mkdir(parents=True)
        (self.storage / storage_dir / "marker.txt").write_text("still here")
        delete_at = datetime.now() - timedelta(days=1) if expired else datetime.now() + timedelta(days=1)
        batch = PhotoBatch(
            id=batch_id, label="test", drive_folder_id="fake", storage_dir=storage_dir,
            status=status, delete_at=delete_at, workflow_version=workflow_version,
            local_status=local_status, drive_status=drive_status,
        )
        with Session(self.engine) as session:
            session.add(batch)
            session.commit()
            session.refresh(batch)
        return batch

    def batch_exists(self, batch_id: str) -> bool:
        with Session(self.engine) as session:
            return session.get(PhotoBatch, batch_id) is not None


class ProtectedStatusTests(RetentionTestCase):
    def test_expired_uploading_batch_survives(self):
        batch_id = "a" * 32
        self.make_batch(batch_id, "uploading")
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))
        self.assertTrue((self.storage / f"photo_batches/{batch_id}/marker.txt").exists())

    def test_expired_upload_failed_batch_survives(self):
        batch_id = "b" * 32
        self.make_batch(batch_id, "upload_failed")
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))

    def test_expired_needs_retry_batch_survives(self):
        """New in this phase: N1's needs_retry status must not lose its
        recoverable (retriable) work to retention."""
        batch_id = "c" * 32
        self.make_batch(batch_id, "needs_retry")
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))

    def test_expired_ready_batch_is_still_deleted(self):
        """The narrow fix protects specific statuses — it must not turn into
        a blanket retention freeze for every other terminal state."""
        batch_id = "d" * 32
        self.make_batch(batch_id, "ready")
        cleaned = pps.run_retention_cleanup()
        self.assertEqual(cleaned, 1)
        self.assertFalse(self.batch_exists(batch_id))
        self.assertFalse((self.storage / f"photo_batches/{batch_id}").exists())

    def test_expired_completed_batch_is_still_deleted(self):
        batch_id = "e" * 32
        self.make_batch(batch_id, "completed")
        pps.run_retention_cleanup()
        self.assertFalse(self.batch_exists(batch_id))

    def test_non_expired_batch_survives_regardless_of_status(self):
        batch_id = "f" * 32
        self.make_batch(batch_id, "ready", expired=False)
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))

    def test_cleanup_log_written_only_for_deleted_batches(self):
        protected_id, deleted_id = "1" * 32, "2" * 32
        self.make_batch(protected_id, "uploading")
        self.make_batch(deleted_id, "ready")
        pps.run_retention_cleanup()
        with Session(self.engine) as session:
            logs = session.exec(select(CleanupLog)).all()
        self.assertEqual([log.batch_id for log in logs], [deleted_id])

    def test_mixed_batch_cleanup_only_removes_eligible_ones(self):
        ids = {status: f"{i}" * 32 for i, status in enumerate(
            ("uploading", "upload_failed", "needs_retry", "ready", "completed", "failed"), start=1
        )}
        for status, batch_id in ids.items():
            self.make_batch(batch_id, status)
        cleaned = pps.run_retention_cleanup()
        self.assertEqual(cleaned, 3)  # ready, completed, failed
        for status, batch_id in ids.items():
            should_survive = status in ("uploading", "upload_failed", "needs_retry")
            self.assertEqual(self.batch_exists(batch_id), should_survive, status)


class NewAxisRetentionTests(RetentionTestCase):
    """Phase M2 — for workflow_version>=2, local_status/drive_status are
    authoritative instead of the legacy `status` string. The legacy string
    is still written alongside (for display/back-compat) but must NOT be
    what retention consults for these batches."""

    def test_expired_local_status_processing_survives_even_with_ready_legacy_status(self):
        # Deliberately mismatched: proves the NEW axis, not the legacy
        # string, is what actually gates deletion for workflow_version>=2.
        batch_id = "a" * 32
        self.make_batch(batch_id, status="ready", workflow_version=2, local_status="PROCESSING")
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))

    def test_expired_local_status_review_required_survives(self):
        batch_id = "b" * 32
        self.make_batch(batch_id, status="ready", workflow_version=2, local_status="REVIEW_REQUIRED")
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))

    def test_expired_drive_status_uploading_survives_even_with_ready_local_status(self):
        batch_id = "c" * 32
        self.make_batch(batch_id, status="ready", workflow_version=2, local_status="READY", drive_status="UPLOADING")
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))

    def test_expired_drive_status_upload_failed_survives(self):
        batch_id = "d" * 32
        self.make_batch(batch_id, status="ready", workflow_version=2, local_status="READY", drive_status="UPLOAD_FAILED")
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))

    def test_expired_local_status_ready_and_drive_not_uploaded_is_deleted(self):
        batch_id = "e" * 32
        self.make_batch(batch_id, status="ready", workflow_version=2, local_status="READY", drive_status="NOT_UPLOADED")
        cleaned = pps.run_retention_cleanup()
        self.assertEqual(cleaned, 1)
        self.assertFalse(self.batch_exists(batch_id))

    def test_expired_local_status_ready_and_drive_uploaded_is_deleted(self):
        """UPLOADED means the Drive copy is durable and independent of local
        storage — original expiry continues, it is not held forever."""
        batch_id = "f" * 32
        self.make_batch(batch_id, status="completed", workflow_version=2, local_status="READY", drive_status="UPLOADED")
        cleaned = pps.run_retention_cleanup()
        self.assertEqual(cleaned, 1)
        self.assertFalse(self.batch_exists(batch_id))

    def test_workflow_version_1_ignores_new_axis_fields_entirely(self):
        """A workflow_version=1 batch with new-axis fields sitting at
        protection-looking values must still be governed ONLY by the
        legacy status (M1 unchanged) — proves no accidental cross-talk."""
        batch_id = "1" * 32
        self.make_batch(batch_id, status="ready", workflow_version=1, local_status="PROCESSING", drive_status="UPLOADING")
        cleaned = pps.run_retention_cleanup()
        self.assertEqual(cleaned, 1)
        self.assertFalse(self.batch_exists(batch_id))

    def test_mixed_workflow_versions_each_governed_by_their_own_rules(self):
        v1_protected = "2" * 32
        v1_deletable = "3" * 32
        v2_protected = "4" * 32
        v2_deletable = "5" * 32
        self.make_batch(v1_protected, status="needs_retry", workflow_version=1)
        self.make_batch(v1_deletable, status="ready", workflow_version=1)
        self.make_batch(v2_protected, status="ready", workflow_version=2, local_status="PROCESSING")
        self.make_batch(v2_deletable, status="ready", workflow_version=2, local_status="READY")
        cleaned = pps.run_retention_cleanup()
        self.assertEqual(cleaned, 2)
        self.assertTrue(self.batch_exists(v1_protected))
        self.assertFalse(self.batch_exists(v1_deletable))
        self.assertTrue(self.batch_exists(v2_protected))
        self.assertFalse(self.batch_exists(v2_deletable))


if __name__ == "__main__":
    unittest.main()


class RetentionDeadlineTests(RetentionTestCase):
    """Retention counts from when the retention setting was applied. Reaching
    READY never re-anchors it — not on a straight run, and not after a batch
    was paused and resumed (which would otherwise hand it a longer life the
    longer it sat paused)."""

    def walk(self, batch_id: str, path: tuple[str, ...], workflow_version: int = 2):
        batch = self.make_batch(batch_id, "processing", expired=False, workflow_version=workflow_version,
                                local_status="PROCESSING")
        with Session(self.engine) as session:
            row = session.get(PhotoBatch, batch_id)
            for local_status in path:
                pps._set_new_axis_local_status(row, local_status)
            session.add(row)
            session.commit()
            session.refresh(row)
            return batch.delete_at, row.delete_at, row.local_status

    def test_processing_to_ready_keeps_the_deadline(self):
        before, after, local_status = self.walk("f" * 32, ("PROCESSING", "READY"))
        self.assertEqual(after, before)
        self.assertEqual(local_status, "READY")

    def test_processing_pause_resume_ready_keeps_the_deadline(self):
        # The axis a paused batch really carries, then the resumed run, then READY.
        before, after, local_status = self.walk("a" * 32, ("PROCESSING", "PROCESSING", "PROCESSING", "READY"))
        self.assertEqual(after, before, "a pause must not buy the batch extra retention")
        self.assertEqual(local_status, "READY")

    def test_reaching_ready_again_after_a_retry_keeps_the_deadline(self):
        before, after, _ = self.walk("b" * 32, ("PROCESSING", "READY", "PROCESSING", "READY"))
        self.assertEqual(after, before)

    def test_changing_the_retention_period_is_the_only_thing_that_moves_it(self):
        batch_id = "9" * 32
        self.make_batch(batch_id, "ready", expired=False, workflow_version=2, local_status="READY")
        with Session(self.engine) as session:
            row = session.get(PhotoBatch, batch_id)
            row.retention_start_at = datetime.now() - timedelta(days=1)
            session.add(row)
            session.commit()
            updated = pps.change_retention(session, row, 3)
            self.assertEqual(updated.delete_at, updated.retention_start_at + timedelta(days=3))


class PausedBatchRetentionTests(RetentionTestCase):
    """A paused batch keeps the deadline it already had. It is never exempt
    from retention — only a live worker or a real reservation defers it."""

    def make_paused(self, batch_id: str, expired: bool = True, workflow_version: int = 2) -> PhotoBatch:
        # workflow_version 2 keeps the unfinished local_status a paused batch
        # really has; that must no longer protect it forever.
        return self.make_batch(batch_id, "paused", expired=expired, workflow_version=workflow_version,
                               local_status="PROCESSING" if workflow_version >= 2 else "CREATED")

    def test_expired_paused_batch_is_deleted_when_nothing_holds_it(self):
        for version in (1, 2):
            with self.subTest(workflow_version=version):
                batch_id = f"{version}" * 32
                self.make_paused(batch_id, workflow_version=version)
                pps.run_retention_cleanup()
                self.assertFalse(self.batch_exists(batch_id))

    def test_paused_batch_survives_until_its_own_deadline(self):
        batch_id = "c" * 32
        batch = self.make_paused(batch_id, expired=False)
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))
        with Session(self.engine) as session:
            self.assertEqual(session.get(PhotoBatch, batch_id).delete_at, batch.delete_at,
                             "pausing never moved the deadline")

    def test_expired_paused_batch_survives_while_a_worker_or_reservation_holds_it(self):
        batch_id = "d" * 32
        self.make_paused(batch_id)
        holds = (
            ("a resumed run", lambda: pps._active_batches.add(batch_id), lambda: pps._active_batches.discard(batch_id)),
            ("a pause finishing", lambda: pps._pause_waiters.add(batch_id), lambda: pps._pause_waiters.discard(batch_id)),
            ("a Drive upload", lambda: dds.reserve_upload(batch_id), lambda: dds._release_upload(batch_id)),
            ("a ZIP download", lambda: batch_edit_lock.begin_download(batch_id), lambda: batch_edit_lock.end_download(batch_id)),
        )
        for name, hold, release in holds:
            with self.subTest(hold=name):
                hold()
                pps.run_retention_cleanup()
                self.assertTrue(self.batch_exists(batch_id), f"deleted while {name} was active")
                release()
        pps.run_retention_cleanup()
        self.assertFalse(self.batch_exists(batch_id), "deletable once nothing holds it")

    def test_expired_pausing_batch_waits_for_the_pause_then_expires(self):
        batch_id = "e" * 32
        self.make_batch(batch_id, "pausing", workflow_version=2, local_status="PROCESSING")
        pps._pause_waiters.add(batch_id)
        self.addCleanup(pps._pause_waiters.discard, batch_id)
        pps.run_retention_cleanup()
        self.assertTrue(self.batch_exists(batch_id))
        # The pause completes: the batch is now paused, with the same deadline.
        pps._pause_waiters.discard(batch_id)
        with Session(self.engine) as session:
            batch = session.get(PhotoBatch, batch_id)
            batch.status = "paused"
            session.add(batch)
            session.commit()
        pps.run_retention_cleanup()
        self.assertFalse(self.batch_exists(batch_id))

