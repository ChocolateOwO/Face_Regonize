"""Phase G4 — local-processing ETA (EWMA, transient, never persisted)."""
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import event_eta_service as eta


class EtaServiceTests(unittest.TestCase):
    def setUp(self):
        self.bid = "batch-eta"
        eta.forget(self.bid)
        self.addCleanup(eta.forget, self.bid)

    def advance(self, seconds: float, times: int = 1):
        """Drive the clock deterministically — a real sleep would make this
        slow and flaky."""
        now = [1000.0]

        def fake_monotonic():
            return now[0]

        with patch.object(eta.time, "monotonic", fake_monotonic):
            eta.start(self.bid)
            for _ in range(times):
                now[0] += seconds
                eta.record_photo_done(self.bid)

    def test_no_eta_before_warmup(self):
        self.advance(2.0, times=eta._WARMUP_PHOTOS - 1)
        self.assertIsNone(eta.eta_seconds(self.bid, remaining_photos=10))
        self.assertTrue(eta.is_estimating(self.bid))

    def test_eta_available_after_warmup(self):
        self.advance(2.0, times=eta._WARMUP_PHOTOS)
        self.assertFalse(eta.is_estimating(self.bid))
        self.assertEqual(eta.eta_seconds(self.bid, remaining_photos=10), 20)

    def test_eta_scales_with_remaining_photos(self):
        self.advance(2.0, times=5)
        one = eta.eta_seconds(self.bid, 1)
        ten = eta.eta_seconds(self.bid, 10)
        self.assertEqual(ten, one * 10)

    def test_no_eta_when_nothing_remains(self):
        self.advance(2.0, times=5)
        self.assertIsNone(eta.eta_seconds(self.bid, remaining_photos=0))

    def test_unknown_batch_has_no_eta_and_is_not_estimating(self):
        self.assertIsNone(eta.eta_seconds("never-started", 5))
        self.assertFalse(eta.is_estimating("never-started"))

    def test_ewma_follows_recent_photos_more_than_old_ones(self):
        """A batch that speeds up should see its estimate come down, rather
        than staying anchored to early slow photos."""
        now = [1000.0]
        with patch.object(eta.time, "monotonic", lambda: now[0]):
            eta.start(self.bid)
            for _ in range(5):       # slow start: 10s per photo
                now[0] += 10.0
                eta.record_photo_done(self.bid)
            slow = eta.eta_seconds(self.bid, 10)
            for _ in range(10):      # then fast: 1s per photo
                now[0] += 1.0
                eta.record_photo_done(self.bid)
            fast = eta.eta_seconds(self.bid, 10)
        self.assertLess(fast, slow)

    def test_forget_clears_state(self):
        self.advance(2.0, times=5)
        self.assertIsNotNone(eta.eta_seconds(self.bid, 10))
        eta.forget(self.bid)
        self.assertIsNone(eta.eta_seconds(self.bid, 10))

    def test_nothing_is_persisted(self):
        """The ETA must never reach the database — a stale estimate read back
        after a restart would be worse than none."""
        import inspect

        source = inspect.getsource(eta)
        for forbidden in ("Session(", "session.add", "commit()", "PhotoBatch"):
            self.assertNotIn(forbidden, source)


class EtaApiShapeTests(unittest.TestCase):
    def test_batch_out_reports_eta_only_while_running(self):
        from app.api import photo_batches as pb
        from app.models.models import PhotoBatch

        running = PhotoBatch(label="x", drive_folder_id="", storage_dir="d", status="processing",
                             total_photos=10, processed_photos=2)
        finished = PhotoBatch(label="x", drive_folder_id="", storage_dir="d", status="ready",
                              total_photos=10, processed_photos=10)
        eta.forget(running.id)
        self.addCleanup(eta.forget, running.id)

        now = [500.0]
        with patch.object(eta.time, "monotonic", lambda: now[0]):
            eta.start(running.id)
            for _ in range(5):
                now[0] += 3.0
                eta.record_photo_done(running.id)

        secs, estimating = pb._eta(running)
        self.assertEqual(secs, 24)          # 8 remaining * 3s
        self.assertFalse(estimating)
        self.assertEqual(pb._eta(finished), (None, False))


if __name__ == "__main__":
    unittest.main()
