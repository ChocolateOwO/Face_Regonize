"""The READY gate has NO review condition.

The Review workflow was removed from the product. `ready` is decided solely by
the Output Completeness Invariant, so a leftover review row from a batch that
predates the removal must not hold a complete batch back — and nothing may
reintroduce such a condition later. These tests exist to catch that.
"""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine, select

import app.services.photo_processing_service as pps
from app.models.models import PhotoBatch, PhotoBatchPhoto, PhotoBatchReviewItem
from app.services.photo_batch_download_service import local_output_ready


class NoReviewGateTests(unittest.TestCase):
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

    def add_item(self, batch, resolved=False):
        """A leftover row from before the removal. Nothing creates these now."""
        photo = PhotoBatchPhoto(batch_id=batch.id, filename="p.jpg", drive_file_id="", original_path="o")
        self.session.add(photo)
        self.session.commit()
        self.session.refresh(photo)
        item = PhotoBatchReviewItem(batch_id=batch.id, photo_id=photo.id, reason="score_ambiguous",
                                    resolved_at=datetime.now() if resolved else None)
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def test_a_complete_batch_reaches_ready(self):
        b = self.batch()
        pps._finalize_batch_status(b)
        self.assertEqual(b.status, "ready")

    def test_an_unresolved_legacy_review_row_does_NOT_block_ready(self):
        b = self.batch()
        self.add_item(b)
        pps._finalize_batch_status(b)
        self.assertEqual(b.status, "ready",
                         "review rows are inert; only output completeness gates ready")

    def test_the_new_axis_reaches_READY_too(self):
        b = self.batch(workflow_version=2, retention_days=3)
        self.add_item(b)
        pps._finalize_batch_status(b)
        self.assertEqual(b.local_status, "READY")

    def test_review_required_is_never_assigned(self):
        b = self.batch()
        self.add_item(b)
        pps._finalize_batch_status(b)
        self.assertNotEqual(b.status, "review_required")
        self.assertNotEqual(b.local_status, "REVIEW_REQUIRED")

    def test_finalize_takes_no_session_argument(self):
        """The parameter existed only to count review items. Its removal is
        the structural guarantee that the gate cannot consult them again."""
        import inspect

        params = list(inspect.signature(pps._finalize_batch_status).parameters)
        self.assertEqual(params, ["batch"])

    def test_the_finalize_source_contains_no_review_term(self):
        import inspect

        source = inspect.getsource(pps._finalize_batch_status).lower()
        for term in ("review_required", "_unresolved_review_count"):
            self.assertNotIn(term, source)

    def test_legacy_review_required_stays_retention_protected(self):
        """No new batch can reach it, but a row that already sits there must
        still never be deleted out from under the admin."""
        import inspect

        source = inspect.getsource(pps.run_retention_cleanup)
        self.assertIn("REVIEW_REQUIRED", source)

    def test_failed_photos_still_block_ready(self):
        b = self.batch(failed_photos=1, processed_photos=2)
        pps._finalize_batch_status(b)
        self.assertEqual(b.status, "needs_retry")

    def test_all_rejected_still_fails(self):
        b = self.batch(total_photos=2, rejected_photos=2, processed_photos=0)
        pps._finalize_batch_status(b)
        self.assertEqual(b.status, "failed")

    def test_export_is_reachable_once_the_batch_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.batch(status="ready")
            b.storage_dir = f"photo_batches/{b.id}"
            root = Path(tmp) / b.storage_dir
            (root / "SORTED").mkdir(parents=True)
            (root / "MEDIA").mkdir(parents=True)
            for i in range(3):
                (root / "MEDIA" / f"{i}.jpg").write_bytes(b"x")
            self.assertTrue(local_output_ready(b, Path(tmp)),
                            "no REVIEW/ folder exists for a new batch, and none is required")

    def test_a_legacy_review_required_batch_is_still_not_exportable(self):
        """That status can no longer be produced, but if one exists it is not
        a completed state, so export must keep refusing it."""
        with tempfile.TemporaryDirectory() as tmp:
            b = self.batch(status="review_required")
            self.assertFalse(local_output_ready(b, Path(tmp)))


class ReviewRowCleanupTests(unittest.TestCase):
    """Deleting a batch must still take any leftover review rows with it.

    Nothing creates them any more, but a batch that predates the removal can
    still carry some, and orphans would outlive the photos they describe.
    """

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)

    def test_delete_review_rows_removes_items_and_their_decisions(self):
        from app.models.models import PhotoBatchReviewDecision

        b = PhotoBatch(label="b", drive_folder_id="", storage_dir="d")
        self.session.add(b)
        self.session.commit()
        self.session.refresh(b)
        photo = PhotoBatchPhoto(batch_id=b.id, filename="p.jpg", drive_file_id="", original_path="o")
        self.session.add(photo)
        self.session.commit()
        self.session.refresh(photo)
        item = PhotoBatchReviewItem(batch_id=b.id, photo_id=photo.id, reason="manual")
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        self.session.add(PhotoBatchReviewDecision(review_item_id=item.id, photo_id=photo.id,
                                                  action="skip", outcome="applied"))
        self.session.commit()

        removed = pps._delete_review_rows(self.session, b.id)
        self.session.commit()
        self.assertEqual(removed, 2, "one item + one decision")
        self.assertEqual(self.session.exec(select(PhotoBatchReviewItem)).all(), [])
        self.assertEqual(self.session.exec(select(PhotoBatchReviewDecision)).all(), [])

    def test_other_batches_review_rows_are_untouched(self):
        keep = PhotoBatch(label="keep", drive_folder_id="", storage_dir="d")
        drop = PhotoBatch(label="drop", drive_folder_id="", storage_dir="d")
        self.session.add(keep)
        self.session.add(drop)
        self.session.commit()
        self.session.refresh(keep)
        self.session.refresh(drop)
        for b in (keep, drop):
            photo = PhotoBatchPhoto(batch_id=b.id, filename="p.jpg", drive_file_id="", original_path="o")
            self.session.add(photo)
            self.session.commit()
            self.session.refresh(photo)
            self.session.add(PhotoBatchReviewItem(batch_id=b.id, photo_id=photo.id, reason="manual"))
        self.session.commit()

        pps._delete_review_rows(self.session, drop.id)
        self.session.commit()
        remaining = self.session.exec(select(PhotoBatchReviewItem)).all()
        self.assertEqual([r.batch_id for r in remaining], [keep.id])

    def test_both_deletion_paths_call_the_cleanup(self):
        import inspect

        self.assertIn("_delete_review_rows", inspect.getsource(pps._delete_owned_state))
        self.assertIn("_delete_review_rows", inspect.getsource(pps.run_retention_cleanup))


class ReviewSurfaceIsGoneTests(unittest.TestCase):
    """Structural guards: the removed surface must stay removed."""

    def test_the_review_service_module_no_longer_exists(self):
        import importlib.util

        self.assertIsNone(importlib.util.find_spec("app.services.review_service"))

    def test_the_batches_api_exposes_no_review_route(self):
        from app.api.photo_batches import router

        paths = [r.path for r in router.routes]
        self.assertEqual([p for p in paths if "review" in p], [])

    def test_processing_never_classifies_a_photo_as_review(self):
        import inspect

        source = inspect.getsource(pps._process_accepted_photo)
        self.assertNotIn('classification = "review"', source)
        self.assertNotIn('"review" if', source)

    def test_processing_writes_no_review_output(self):
        import inspect

        source = inspect.getsource(pps._process_accepted_photo)
        self.assertNotIn("/REVIEW/", source)

    def test_the_regeneration_core_survived_the_removal(self):
        from app.services import photo_regeneration_service as prs

        for name in ("render_photo_bytes", "destination_paths", "_FileGuard",
                     "_atomic_replace", "privacy_requires_mask", "regenerate_photo"):
            self.assertTrue(hasattr(prs, name), f"{name} must survive for the D1 editor")


if __name__ == "__main__":
    unittest.main()
