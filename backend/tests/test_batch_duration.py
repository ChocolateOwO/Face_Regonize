"""Phase G4 — the final elapsed duration is persisted, not a browser timer.

The live ETA is in-memory, so it vanishes on refresh or restart. These tests
pin the persisted counterpart: when the timestamps are written, what the API
derives from them, and that a batch predating the columns reports nothing
rather than a fabricated number.
"""
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine

import app.services.photo_processing_service as pps
from app.api.photo_batches import _elapsed_seconds
from app.models.models import PhotoBatch


class ElapsedSecondsTests(unittest.TestCase):
    def batch(self, **kwargs):
        return PhotoBatch(label="b", drive_folder_id="", storage_dir="d", **kwargs)

    def test_a_finished_run_reports_its_duration(self):
        start = datetime(2026, 9, 11, 10, 0, 0)
        b = self.batch(processing_started_at=start,
                       processing_finished_at=start + timedelta(seconds=154))
        self.assertEqual(_elapsed_seconds(b), 154)

    def test_a_running_batch_reports_nothing(self):
        b = self.batch(processing_started_at=datetime.now(), processing_finished_at=None)
        self.assertIsNone(_elapsed_seconds(b), "no duration until the run actually ends")

    def test_a_batch_from_before_this_shipped_reports_nothing(self):
        """Both columns are NULL for historical rows. The UI omits the line
        rather than showing a made-up zero."""
        b = self.batch()
        self.assertIsNone(_elapsed_seconds(b))

    def test_a_negative_interval_is_refused_rather_than_shown(self):
        start = datetime(2026, 9, 11, 10, 0, 0)
        b = self.batch(processing_started_at=start, processing_finished_at=start - timedelta(seconds=5))
        self.assertIsNone(_elapsed_seconds(b))

    def test_sub_second_runs_round_down_to_zero_not_to_none(self):
        start = datetime(2026, 9, 11, 10, 0, 0)
        b = self.batch(processing_started_at=start, processing_finished_at=start + timedelta(milliseconds=400))
        self.assertEqual(_elapsed_seconds(b), 0)


class FinalizeWritesTheFinishTimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)

    def batch(self, **kwargs):
        defaults = dict(label="b", drive_folder_id="", storage_dir="d", source_type="drive",
                        total_photos=3, processed_photos=3, failed_photos=0, rejected_photos=0)
        defaults.update(kwargs)
        b = PhotoBatch(**defaults)
        self.session.add(b)
        self.session.commit()
        self.session.refresh(b)
        return b

    def test_finalizing_a_ready_batch_stamps_the_finish_time(self):
        b = self.batch(processing_started_at=datetime.now())
        pps._finalize_batch_status(b)
        self.assertEqual(b.status, "ready")
        self.assertIsNotNone(b.processing_finished_at)

    def test_a_failed_run_is_also_stamped(self):
        """The run really did stop; leaving it unstamped would look like it is
        still going forever."""
        b = self.batch(total_photos=2, rejected_photos=2, processed_photos=0,
                       processing_started_at=datetime.now())
        pps._finalize_batch_status(b)
        self.assertEqual(b.status, "failed")
        self.assertIsNotNone(b.processing_finished_at)

    def test_a_needs_retry_run_is_also_stamped(self):
        b = self.batch(failed_photos=1, processed_photos=2, processing_started_at=datetime.now())
        pps._finalize_batch_status(b)
        self.assertEqual(b.status, "needs_retry")
        self.assertIsNotNone(b.processing_finished_at)

    def test_the_duration_is_of_the_run_not_of_the_batch_row(self):
        """created_at could be days earlier — the figure must come from the
        processing window, never from when the batch was created."""
        b = self.batch(created_at=datetime.now() - timedelta(days=3),
                       processing_started_at=datetime.now() - timedelta(seconds=30))
        pps._finalize_batch_status(b)
        elapsed = _elapsed_seconds(b)
        self.assertIsNotNone(elapsed)
        self.assertLess(elapsed, 120, "must measure the run, not the batch's age")


class SourceContractTests(unittest.TestCase):
    """Where the timestamps are written is the whole correctness story, so it
    is asserted structurally rather than left to a live run."""

    def test_the_start_is_stamped_where_the_eta_starts(self):
        import inspect

        source = inspect.getsource(pps._run_photo_batch)
        self.assertIn("processing_started_at", source)
        self.assertIn("event_eta_service.start", source)

    def test_a_rerun_clears_the_previous_finish_time(self):
        import inspect

        source = inspect.getsource(pps._run_photo_batch)
        self.assertIn("processing_finished_at = None", source,
                      "a running batch must not still advertise an old finish time")

    def test_a_retry_starts_its_own_window(self):
        import inspect

        source = inspect.getsource(pps._retry_unresolved_photos)
        self.assertIn("processing_started_at", source)
        self.assertIn("processing_finished_at = None", source)

    def test_the_finish_is_stamped_in_the_one_shared_finalize(self):
        import inspect

        source = inspect.getsource(pps._finalize_batch_status)
        self.assertIn("processing_finished_at", source)


class MigrationTests(unittest.TestCase):
    def test_008_adds_both_columns_and_is_idempotent(self):
        from app.migrations import runner

        module = __import__("app.migrations.008_batch_processing_duration", fromlist=["upgrade"])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE photobatch (id TEXT PRIMARY KEY)")
                conn.commit()
                module.upgrade(conn)
                module.upgrade(conn)  # running twice must not raise
                cols = {row[1] for row in conn.execute("PRAGMA table_info(photobatch)")}
            finally:
                conn.close()
        self.assertIn("processing_started_at", cols)
        self.assertIn("processing_finished_at", cols)
        self.assertTrue(hasattr(runner, "add_column_if_missing"))

    def test_008_is_registered_in_order(self):
        from app.migrations.runner import _discover

        ids = [row[0] for row in _discover()]
        self.assertIn("008", ids)
        self.assertEqual(ids, sorted(ids), "migrations must be discovered in order")


if __name__ == "__main__":
    unittest.main()
