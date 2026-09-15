"""People-view stutter fix — the backend-owned `live` flag.

Measured cause: the batch page polled every 2 s forever and re-rendered the
whole tree on every tick, even for a finished batch. The page now polls fast
only while the backend says the batch is live. These tests pin what "live"
means, and — structurally — that every status the code can write is classified,
so a future status cannot silently fall into the wrong bucket.
"""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.photo_batches import IDLE_STATUSES, LIVE_STATUSES, _batch_out, batch_is_live
from app.models.models import PhotoBatch
from app.services import drive_destination_service as dds
from app.services import photo_processing_service as pps
from app.services.event_pipeline_registry import registry as pipeline_registry

APP = Path(__file__).resolve().parents[1] / "app"
# The only modules that write PhotoBatch.status. (nodes.py / import_service.py
# write `.status` on unrelated rows and are deliberately not scanned.)
STATUS_WRITERS = ("services/photo_processing_service.py", "services/drive_destination_service.py")


def batch(**kwargs):
    defaults = dict(label="b", drive_folder_id="", storage_dir="d")
    defaults.update(kwargs)
    return PhotoBatch(**defaults)


class ClassificationCompletenessTests(unittest.TestCase):
    def test_every_status_the_code_writes_is_classified(self):
        written = set()
        for rel in STATUS_WRITERS:
            source = (APP / rel).read_text(encoding="utf-8")
            written |= set(re.findall(r'batch\.status\s*=\s*"([a-z_]+)"', source))
        self.assertTrue(written, "the scan must find the writers")
        unclassified = written - LIVE_STATUSES - IDLE_STATUSES
        self.assertEqual(unclassified, set(), f"classify these in LIVE/IDLE_STATUSES: {unclassified}")

    def test_the_model_default_is_classified(self):
        self.assertIn(batch().status, LIVE_STATUSES | IDLE_STATUSES)

    def test_the_two_sets_do_not_overlap(self):
        self.assertEqual(LIVE_STATUSES & IDLE_STATUSES, set())


class PersistedStateTests(unittest.TestCase):
    def test_in_progress_statuses_are_live(self):
        for status in ("pending", "processing", "uploading", "syncing_drive"):
            self.assertTrue(batch_is_live(batch(status=status)), status)

    def test_finished_and_waiting_statuses_are_idle(self):
        for status in ("ready", "completed", "failed", "upload_failed", "review_required"):
            self.assertFalse(batch_is_live(batch(status=status)), status)

    def test_needs_retry_is_idle_even_though_its_local_status_is_PROCESSING(self):
        """needs_retry maps local_status to PROCESSING for retention, but the
        batch is waiting for the admin — polling it fast forever is exactly the
        jank being fixed."""
        self.assertFalse(batch_is_live(batch(status="needs_retry", local_status="PROCESSING")))

    def test_a_drive_upload_keeps_the_page_live_even_after_local_is_done(self):
        """Local processing being terminal must not freeze Drive progress."""
        self.assertTrue(batch_is_live(batch(status="ready", drive_status="UPLOADING")))


class RuntimeSignalTests(unittest.TestCase):
    """The in-process registries win over a persisted row that lags."""

    def test_an_active_run_or_retry_is_live(self):
        b = batch(status="ready")
        with pps._batch_lock:
            pps._active_batches.add(b.id)
        try:
            self.assertTrue(batch_is_live(b))
        finally:
            with pps._batch_lock:
                pps._active_batches.discard(b.id)
        self.assertFalse(batch_is_live(b))

    def test_a_held_drive_upload_reservation_is_live(self):
        b = batch(status="ready")
        self.assertTrue(dds.reserve_upload(b.id))
        try:
            self.assertTrue(batch_is_live(b))
        finally:
            with dds._upload_lock:
                dds._uploading_batches.discard(b.id)
        self.assertFalse(batch_is_live(b))

    def test_a_registered_pipeline_is_live(self):
        b = batch(status="ready")
        pipeline_registry.register(b.id)
        try:
            self.assertTrue(batch_is_live(b))
        finally:
            pipeline_registry.unregister(b.id)
        self.assertFalse(batch_is_live(b))


class SerializerTests(unittest.TestCase):
    def test_the_payload_exposes_live_and_both_status_axes(self):
        out = _batch_out(batch(status="processing", local_status="PROCESSING", drive_status="NOT_UPLOADED"))
        self.assertIs(out["live"], True)
        self.assertEqual(out["local_status"], "PROCESSING")
        self.assertEqual(out["drive_status"], "NOT_UPLOADED")

    def test_every_payload_value_is_a_primitive(self):
        """The page's shallow equality check (sameBatch) relies on this: a
        nested object would compare unequal on every tick and defeat it."""
        out = _batch_out(batch(status="processing"))
        for key, value in out.items():
            self.assertIsInstance(value, (str, int, float, bool, type(None), __import__("datetime").datetime),
                                  f"{key} must stay a primitive")


if __name__ == "__main__":
    unittest.main()
