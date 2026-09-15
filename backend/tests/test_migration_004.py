"""Phase 0 — migration 004 (workflow-axis columns), restored verbatim from
the Main tree. Exercises the raw sqlite3 upgrade() directly (matching how
runner.py actually calls it) and the full runner against a fresh DB, so
this never needs app.config/main or a real database file.
"""
import importlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import SQLModel, create_engine

import app.models.models  # noqa: F401 - registers every table on SQLModel.metadata

m004 = importlib.import_module("app.migrations.004_photo_batch_local_first")
runner = importlib.import_module("app.migrations.runner")


def _make_photobatch_table(conn: sqlite3.Connection) -> None:
    """Minimal shape matching migrations 001-003's photobatch, enough for
    004's ALTER/backfill logic (status + the two drive-failure columns it
    reads) plus photobatchphoto for the drive_status EXISTS subqueries."""
    conn.execute(
        """CREATE TABLE photobatch (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            drive_failed_photos INTEGER NOT NULL DEFAULT 0,
            drive_error TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE photobatchphoto (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            drive_upload_status TEXT
        )"""
    )


def _insert_batch(conn, batch_id, status, drive_failed=0, drive_error=None):
    conn.execute(
        "INSERT INTO photobatch (id, status, drive_failed_photos, drive_error) VALUES (?, ?, ?, ?)",
        (batch_id, status, drive_failed, drive_error),
    )


class Migration004ColumnsAndBackfillTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        _make_photobatch_table(self.conn)

    def _row(self, batch_id):
        cur = self.conn.execute(
            "SELECT source_type, source_status, local_status, drive_status, workflow_version "
            "FROM photobatch WHERE id=?",
            (batch_id,),
        )
        return cur.fetchone()

    def test_adds_all_five_columns_with_defaults(self):
        _insert_batch(self.conn, "b-pending", "pending")
        m004.upgrade(self.conn)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(photobatch)")}
        self.assertTrue({"source_type", "source_status", "local_status", "drive_status", "workflow_version"} <= cols)

    def test_backfill_completed_maps_to_ready(self):
        _insert_batch(self.conn, "b-completed", "completed")
        m004.upgrade(self.conn)
        source_type, source_status, local_status, drive_status, workflow_version = self._row("b-completed")
        self.assertEqual(source_type, "drive")
        self.assertEqual(source_status, "READY")
        self.assertEqual(local_status, "READY")
        self.assertEqual(workflow_version, 1)

    def test_backfill_processing_maps_to_processing(self):
        _insert_batch(self.conn, "b-proc", "processing")
        m004.upgrade(self.conn)
        _, source_status, local_status, _, _ = self._row("b-proc")
        self.assertEqual(source_status, "PENDING")  # only completed/syncing_drive/failed are distinguished for source_status
        self.assertEqual(local_status, "PROCESSING")

    def test_backfill_failed_maps_to_failed(self):
        _insert_batch(self.conn, "b-failed", "failed")
        m004.upgrade(self.conn)
        _, source_status, local_status, _, _ = self._row("b-failed")
        self.assertEqual(source_status, "FAILED")
        self.assertEqual(local_status, "FAILED")

    def test_backfill_pending_falls_through_to_defaults(self):
        _insert_batch(self.conn, "b-pending2", "pending")
        m004.upgrade(self.conn)
        _, source_status, local_status, _, _ = self._row("b-pending2")
        self.assertEqual(source_status, "PENDING")
        self.assertEqual(local_status, "CREATED")

    def test_backfill_syncing_drive_maps_drive_status_uploading(self):
        _insert_batch(self.conn, "b-sync", "syncing_drive")
        m004.upgrade(self.conn)
        _, _, _, drive_status, _ = self._row("b-sync")
        self.assertEqual(drive_status, "UPLOADING")

    def test_backfill_drive_failure_maps_upload_failed(self):
        _insert_batch(self.conn, "b-drivefail", "ready", drive_failed=2)
        m004.upgrade(self.conn)
        self.assertEqual(self._row("b-drivefail")[3], "UPLOAD_FAILED")

    def test_backfill_all_photos_uploaded_maps_uploaded(self):
        _insert_batch(self.conn, "b-uploaded", "ready")
        self.conn.execute(
            "INSERT INTO photobatchphoto (id, batch_id, drive_upload_status) VALUES ('p1', 'b-uploaded', 'uploaded')"
        )
        m004.upgrade(self.conn)
        self.assertEqual(self._row("b-uploaded")[3], "UPLOADED")

    def test_backfill_default_not_uploaded(self):
        _insert_batch(self.conn, "b-none", "ready")
        m004.upgrade(self.conn)
        self.assertEqual(self._row("b-none")[3], "NOT_UPLOADED")

    def test_no_photobatch_table_is_a_safe_noop(self):
        conn = sqlite3.connect(":memory:")
        try:
            m004.upgrade(conn)  # must not raise even though photobatch doesn't exist
        finally:
            conn.close()

    def test_idempotent_rerun_never_changes_live_values(self):
        _insert_batch(self.conn, "b-idem", "completed")
        m004.upgrade(self.conn)
        first = self._row("b-idem")

        # Simulate an admin/operator having since changed a live value —
        # re-running the migration must NEVER touch it, since none of the
        # column names are newly-added the second time around.
        self.conn.execute("UPDATE photobatch SET local_status='REVIEW_REQUIRED' WHERE id='b-idem'")
        m004.upgrade(self.conn)  # second run: idempotent no-op, must not raise
        second = self._row("b-idem")

        self.assertEqual(second[2], "REVIEW_REQUIRED")  # untouched by the re-run
        self.assertEqual(second[0], first[0])
        self.assertEqual(second[4], first[4])

    def test_already_migrated_db_rerun_adds_no_duplicate_columns(self):
        _insert_batch(self.conn, "b-already", "completed")
        m004.upgrade(self.conn)
        m004.upgrade(self.conn)  # must not raise ("duplicate column name")
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(photobatch)")]
        self.assertEqual(len(cols), len(set(cols)))


class Migration004RunnerIntegrationTests(unittest.TestCase):
    """Exercises the real run_pending_migrations() path against a fresh,
    on-disk DB carrying only 001-003 (this worktree's actual starting
    state), confirming 004 is discovered, applied, and recorded."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-migration004-test-")
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "test.db"
        # Mirrors init_db(): create_all() makes every declared table
        # (including SchemaMigration and photobatch, at their CURRENT
        # SQLModel field set) before any ALTER-based migration runs.
        engine = create_engine(f"sqlite:///{self.db_path.as_posix()}")
        SQLModel.metadata.create_all(engine)
        engine.dispose()

    def _run(self):
        with patch.object(runner, "DATABASE_PATH", self.db_path):
            return runner.run_pending_migrations()

    def test_fresh_db_applies_004_after_001_003(self):
        applied_first = self._run()
        self.assertIn("004", applied_first)

        conn = sqlite3.connect(str(self.db_path))
        try:
            applied_ids = {r[0] for r in conn.execute("SELECT id FROM schemamigration")}
            self.assertIn("004", applied_ids)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(photobatch)")}
            self.assertTrue({"source_type", "source_status", "local_status", "drive_status", "workflow_version"} <= cols)
        finally:
            conn.close()

        # Re-running the whole runner (simulating a second backend startup)
        # must be a safe no-op: 004 is already recorded, so it is skipped.
        applied_second = self._run()
        self.assertNotIn("004", applied_second)


if __name__ == "__main__":
    unittest.main()
