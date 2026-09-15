"""Phase D1 — face bounding-box geometry editor.

Geometry only (never identity / consent / visibility). These tests pin:
validation, the fail-closed containment rule for masked faces, the
availability gate and activity lock, the transactional guarantee (DB and files
unchanged on any failure, ORM untouched until every file landed), thumbnail
invalidation, full-resolution re-render, and that a declined face stays masked.
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session

from app.models.models import PhotoBatchFace
from app.services import batch_edit_lock
from app.services import drive_destination_service as dds
from app.services import face_geometry_service as fgs
from app.services import photo_processing_service as pps
from app.services import photo_regeneration_service as prs
from app.services import photo_thumbnail_service as pts
from app.services.event_pipeline_registry import registry as pipeline_registry
from tests.test_photo_regeneration import RegenerationTestCase

DECLINED_BOX = (10.0, 10.0, 40.0, 40.0)   # set by RegenerationTestCase
UNKNOWN_BOX = (60.0, 60.0, 90.0, 90.0)


def _decode(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


class GeometryTestCase(RegenerationTestCase):
    def setUp(self):
        super().setUp()
        self.batch.status = "ready"
        self.session.add(self.batch)
        self.session.commit()
        self.addCleanup(lambda: batch_edit_lock.release_edit(self.batch.id))

    def edit(self, face, box):
        return fgs.update_face_bbox(self.session, self.batch, self.photo, face.id, box,
                                    storage_path=self.storage, photo_batches_dir=self.photo_batches_dir)

    def db_face(self, face_id) -> PhotoBatchFace:
        with Session(self.engine) as fresh:
            return fresh.get(PhotoBatchFace, face_id)

    def original(self) -> np.ndarray:
        return _decode((self.storage / self.photo.original_path).read_bytes())


class ValidationTests(GeometryTestCase):
    def test_boxes_outside_the_image_are_refused(self):
        for box in ((-1, 10, 40, 40), (10, 10, 121, 40), (10, 10, 40, 999)):
            with self.assertRaises(fgs.GeometryError, msg=str(box)):
                self.edit(self.unknown_face, box)

    def test_inverted_and_tiny_boxes_are_refused(self):
        for box in ((40, 10, 10, 40), (10, 10, 12, 12)):
            with self.assertRaises(fgs.GeometryError):
                self.edit(self.unknown_face, box)

    def test_non_finite_numbers_are_refused(self):
        with self.assertRaises(fgs.GeometryError):
            self.edit(self.unknown_face, (10, 10, float("nan"), 40))

    def test_a_face_from_another_photo_is_refused(self):
        with self.assertRaises(fgs.GeometryError) as ctx:
            fgs.update_face_bbox(self.session, self.batch, self.photo, "not-a-face", (10, 10, 40, 40),
                                 storage_path=self.storage, photo_batches_dir=self.photo_batches_dir)
        self.assertEqual(ctx.exception.status, 404)


class FailClosedContainmentTests(GeometryTestCase):
    """A declined participant's mask can be enlarged or extended — never shrunk
    below, or moved off, what the detector found."""

    def test_shrinking_a_masked_face_is_refused_and_changes_nothing(self):
        before = self.live_state()
        with self.assertRaises(fgs.GeometryError):
            self.edit(self.declined_face, (15, 15, 35, 35))
        self.assertEqual(self.db_face(self.declined_face.id).bbox, "10,10,40,40")
        self.assertEqual(self.live_state(), before)

    def test_moving_a_masked_face_away_is_refused(self):
        with self.assertRaises(fgs.GeometryError):
            self.edit(self.declined_face, (50, 50, 80, 80))

    def test_enlarging_a_masked_face_is_accepted_and_freezes_the_detected_box(self):
        self.edit(self.declined_face, (5, 5, 50, 50))
        f = self.db_face(self.declined_face.id)
        self.assertEqual(f.bbox, "5.0,5.0,50.0,50.0")
        self.assertEqual(f.detected_bbox, "10,10,40,40", "the detector's box is frozen on the first edit")

    def test_repeated_edits_are_judged_against_the_DETECTED_box_not_the_last_edit(self):
        self.edit(self.declined_face, (5, 5, 50, 50))
        # back down to exactly the detected region: allowed
        self.edit(self.declined_face, DECLINED_BOX)
        # below the detected region: refused, even though it contains nothing
        # smaller than a previous edit did
        with self.assertRaises(fgs.GeometryError):
            self.edit(self.declined_face, (12, 12, 40, 40))
        f = self.db_face(self.declined_face.id)
        self.assertEqual(f.detected_bbox, "10,10,40,40", "never changes after the first edit")

    def test_a_visible_face_can_be_shrunk_or_moved(self):
        self.edit(self.unknown_face, (65, 65, 85, 85))
        self.assertEqual(self.db_face(self.unknown_face.id).bbox, "65.0,65.0,85.0,85.0")


class GateAndLockTests(GeometryTestCase):
    def assert_blocked(self):
        with self.assertRaises(fgs.GeometryError) as ctx:
            self.edit(self.unknown_face, (62, 62, 88, 88))
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.db_face(self.unknown_face.id).bbox, "60,60,90,90")

    def test_an_unfinished_batch_is_not_editable(self):
        for status in ("processing", "pending", "needs_retry", "failed"):
            self.batch.status = status
            self.assert_blocked()

    def test_a_running_pipeline_blocks_edits(self):
        pipeline_registry.register(self.batch.id)
        try:
            self.assert_blocked()
        finally:
            pipeline_registry.unregister(self.batch.id)

    def test_a_running_retry_blocks_edits(self):
        with pps._batch_lock:
            pps._active_batches.add(self.batch.id)
        try:
            self.assert_blocked()
        finally:
            with pps._batch_lock:
                pps._active_batches.discard(self.batch.id)

    def test_a_drive_upload_blocks_edits(self):
        self.assertTrue(dds.reserve_upload(self.batch.id))
        try:
            self.assert_blocked()
        finally:
            with dds._upload_lock:
                dds._uploading_batches.discard(self.batch.id)

    def test_a_zip_build_blocks_edits(self):
        self.assertTrue(batch_edit_lock.begin_download(self.batch.id))
        try:
            self.assert_blocked()
        finally:
            batch_edit_lock.end_download(self.batch.id)

    def test_an_edit_blocks_zip_builds_and_drive_uploads(self):
        self.assertTrue(batch_edit_lock.reserve_edit(self.batch.id))
        try:
            self.assertFalse(batch_edit_lock.begin_download(self.batch.id))
            self.assertFalse(batch_edit_lock.try_reserve_upload(self.batch.id))
        finally:
            batch_edit_lock.release_edit(self.batch.id)
        self.assertTrue(batch_edit_lock.begin_download(self.batch.id))
        batch_edit_lock.end_download(self.batch.id)

    def test_the_edit_lock_is_released_after_a_failure(self):
        with self.assertRaises(fgs.GeometryError):
            self.edit(self.declined_face, (15, 15, 35, 35))
        self.assertFalse(batch_edit_lock.is_editing(self.batch.id))


class TransactionTests(GeometryTestCase):
    def test_a_file_replacement_failure_leaves_db_and_files_unchanged(self):
        before = self.live_state()
        calls = {"n": 0}
        real = prs._atomic_replace

        def flaky(temp, dest):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk gave up")
            return real(temp, dest)

        with patch.object(prs, "_atomic_replace", flaky):
            with self.assertRaises(prs.RegenerationError):
                self.edit(self.unknown_face, (62, 62, 88, 88))
        f = self.db_face(self.unknown_face.id)
        self.assertEqual((f.bbox, f.detected_bbox), ("60,60,90,90", None))
        self.assertEqual(self.live_state(), before)
        self.assertFalse(batch_edit_lock.is_editing(self.batch.id))

    def test_a_commit_failure_restores_every_file_and_the_db(self):
        before = self.live_state()
        with patch.object(self.session, "commit", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(prs.RegenerationError):
                self.edit(self.unknown_face, (62, 62, 88, 88))
        self.assertEqual(self.db_face(self.unknown_face.id).bbox, "60,60,90,90")
        self.assertEqual(self.live_state(), before)

    def test_the_orm_row_is_untouched_while_files_are_being_replaced(self):
        """No autoflush can write the new box before every file has landed:
        the row is modified only inside the commit callback."""
        seen = []
        real = prs._atomic_replace

        def spy(temp, dest):
            row = self.session.get(PhotoBatchFace, self.unknown_face.id)
            seen.append((row.bbox, row in self.session.dirty, row.detected_bbox))
            return real(temp, dest)

        with patch.object(prs, "_atomic_replace", spy):
            self.edit(self.unknown_face, (62, 62, 88, 88))
        self.assertTrue(seen)
        self.assertTrue(all(s == ("60,60,90,90", False, None) for s in seen), seen)
        self.assertEqual(self.db_face(self.unknown_face.id).bbox, "62.0,62.0,88.0,88.0")


class RenderTests(GeometryTestCase):
    def test_the_re_render_is_full_resolution(self):
        self.edit(self.unknown_face, (62, 62, 88, 88))
        self.assertEqual(_decode(self.media.read_bytes()).shape, self.original().shape)

    def test_the_declined_face_is_masked_over_the_whole_new_box(self):
        self.edit(self.declined_face, (5, 5, 50, 50))
        media, orig = _decode(self.media.read_bytes()), self.original()
        self.assertFalse(np.array_equal(media[5:50, 5:50], orig[5:50, 5:50]), "declined region must be masked")

    def test_a_visible_face_stays_visible(self):
        self.edit(self.unknown_face, (62, 62, 88, 88))
        media, orig = _decode(self.media.read_bytes()), self.original()
        np.testing.assert_array_equal(media[65:85, 65:85], orig[65:85, 65:85])

    def test_the_thumbnail_is_replaced_and_its_url_changes(self):
        rel = pts.thumbnail_relpath(self.batch.storage_dir, self.photo.filename)
        url_before = pts.thumbnail_url(self.batch.storage_dir, self.photo.filename, self.storage, self.photo.media_path)
        self.edit(self.declined_face, (5, 5, 50, 50))
        thumb = self.storage / rel
        self.assertTrue(thumb.is_file(), "the thumbnail is rewritten in the same guarded operation")
        self.assertEqual(thumb.read_bytes(), pts.encode_thumbnail(self.media.read_bytes()))
        url_after = pts.thumbnail_url(self.batch.storage_dir, self.photo.filename, self.storage, self.photo.media_path)
        self.assertNotEqual(url_before, url_after, "a re-render must yield a new cache-busting URL")


class PayloadTests(GeometryTestCase):
    def test_the_payload_never_references_original(self):
        payload = fgs.faces_payload(self.session, self.batch, self.photo, self.storage)
        text = str(payload)
        self.assertNotIn("ORIGINAL", text)
        self.assertNotIn("original_path", payload)
        self.assertEqual(payload["media_path"], self.photo.media_path)
        self.assertEqual((payload["width"], payload["height"]), (120, 120))

    def test_the_payload_marks_which_faces_require_a_mask(self):
        payload = fgs.faces_payload(self.session, self.batch, self.photo, self.storage)
        by_id = {f["id"]: f for f in payload["faces"]}
        self.assertTrue(by_id[self.declined_face.id]["mask_required"])
        self.assertFalse(by_id[self.unknown_face.id]["mask_required"])

    def test_the_editor_is_marked_read_only_when_the_batch_is_not_editable(self):
        self.batch.status = "processing"
        payload = fgs.faces_payload(self.session, self.batch, self.photo, self.storage)
        self.assertFalse(payload["editable"])
        self.assertTrue(payload["blocked_reason"])


class RouteTests(unittest.TestCase):
    def test_the_face_routes_exist_and_no_review_route_came_back(self):
        from app.api.photo_batches import router

        paths = [r.path for r in router.routes]
        self.assertIn("/api/photo-batches/{batch_id}/photos/{photo_id}/faces", paths)
        self.assertIn("/api/photo-batches/{batch_id}/photos/{photo_id}/faces/{face_id}", paths)
        self.assertEqual([p for p in paths if "review" in p], [])


if __name__ == "__main__":
    unittest.main()
