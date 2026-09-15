"""Amendment A1 — retry idempotency for counters and PhotoBatchFace rows.

Runs the REAL sequential _run_photo_batch / _process_accepted_photo /
retry_unresolved_photos against an isolated temp DB + temp storage, faking only
the external boundaries (Drive, detection, matching). Failures are injected at
the MEDIA write (the last file step) and inside the finalize commit, which are
exactly the windows the confirmed bug lived in.
"""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from sqlmodel import Session, select

import app.services.photo_processing_service as pps
from app.models.models import (
    ConsentRecord,
    Person,
    PhotoBatch,
    PhotoBatchFace,
    PhotoBatchParticipantFolder,
    PhotoBatchPhoto,
)
from tests.test_event_photo_completeness import CompletenessTestCase, _png_bytes

COUNTERS = ("processed_photos", "failed_photos", "recognized_photos", "ambience_photos", "faces_detected",
            "faces_recognized", "faces_unknown", "blurred_faces", "consented_faces", "not_consented_faces")


def _face(i: int):
    emb = np.zeros(512, dtype=np.float32)
    emb[i] = 1.0
    return SimpleNamespace(embedding=emb, bbox=(2.0 + i, 2.0, 20.0 + i, 20.0), det_score=0.9)


class RetryIdempotencyTestCase(CompletenessTestCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as session:
            person = Person(participant_id="0001", first_name="Aiko", last_name="A", image_path="", embedding=b"")
            session.add(person)
            session.commit()
            session.refresh(person)
            session.add(ConsentRecord(person_id=person.id, choice="consented", source="test"))
            session.commit()
            self.person_id = person.id
        # one recognized (consented) face + one stranger in every photo
        self.detect = lambda img: [_face(0), _face(1)]
        match = patch.object(pps.recognition_index, "match_batch",
                             side_effect=lambda emb, thr: [(self.person_id, "Aiko", "Aiko A", "0001", 0.9),
                                                           (None, None, None, None, -1.0)])
        match.start()
        self.addCleanup(match.stop)

    # ---- fault injection -----------------------------------------------------
    def fail_media_for(self, filename: str, times: int = 1):
        """Make the MEDIA write for `filename` raise `times` times, then work."""
        real = pps._write_bytes
        state = {"left": times}

        def flaky(path, data):
            if f"/MEDIA/{filename}" in Path(path).as_posix() and state["left"] > 0:
                state["left"] -= 1
                raise OSError("simulated disk failure on MEDIA write")
            return real(path, data)

        return patch.object(pps, "_write_bytes", side_effect=flaky)

    def fail_inside_finalize_commit(self, times: int = 1):
        """Fail AFTER the face rows were replaced but before the commit lands —
        the DB-side failure window."""
        real = pps._replace_face_rows
        state = {"left": times}

        def flaky(session, photo, rows):
            real(session, photo, rows)
            if state["left"] > 0:
                state["left"] -= 1
                raise RuntimeError("simulated database failure inside the finalize commit")

        return patch.object(pps, "_replace_face_rows", side_effect=flaky)

    # ---- helpers -------------------------------------------------------------
    def retry(self):
        with patch.object(pps, "detect_faces", side_effect=self.detect):
            pps.retry_unresolved_photos(self.batch_id)

    def counters(self) -> dict:
        b = self.batch()
        return {k: getattr(b, k) for k in COUNTERS}

    def faces_for(self, filename: str) -> list[PhotoBatchFace]:
        with Session(self.engine) as session:
            photo = session.exec(select(PhotoBatchPhoto).where(
                PhotoBatchPhoto.batch_id == self.batch_id, PhotoBatchPhoto.filename == filename)).one()
            return session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all()

    def folder_count(self) -> int:
        with Session(self.engine) as session:
            pf = session.exec(select(PhotoBatchParticipantFolder).where(
                PhotoBatchParticipantFolder.batch_id == self.batch_id,
                PhotoBatchParticipantFolder.person_id == self.person_id)).first()
            return pf.photo_count if pf else 0

    def expected_for(self, n_photos: int) -> dict:
        """Exact counters for n finalized photos of (1 consented match + 1 stranger)."""
        return dict(processed_photos=n_photos, failed_photos=0, recognized_photos=n_photos, ambience_photos=0,
                    faces_detected=2 * n_photos, faces_recognized=n_photos, faces_unknown=n_photos,
                    blurred_faces=0, consented_faces=n_photos, not_consented_faces=n_photos)


class ConfirmTheBugTests(RetryIdempotencyTestCase):
    """Written first, before the fix, stating the CORRECT behaviour. Both
    failed against the old code (recognized 3 != 2, face rows 4 != 2)."""

    def test_a_retried_photo_is_counted_recognized_exactly_once(self):
        with self.fail_media_for("p1.png"):
            self.run_batch({"p1.png": _png_bytes(1), "p2.png": _png_bytes(2)}, detect_side_effect=self.detect)
        self.assertEqual(self.batch().failed_photos, 1)
        self.retry()
        self.assertEqual(self.counters(), self.expected_for(2))

    def test_a_retried_photo_does_not_get_duplicate_face_rows(self):
        with self.fail_media_for("p1.png"):
            self.run_batch({"p1.png": _png_bytes(1)}, detect_side_effect=self.detect)
        self.retry()
        self.assertEqual(len(self.faces_for("p1.png")), 2, "2 detected faces -> exactly 2 rows")


class FailureLeavesStateUnchangedTests(RetryIdempotencyTestCase):
    def test_a_failed_first_attempt_leaves_no_face_rows_and_no_counters(self):
        with self.fail_media_for("p1.png"):
            self.run_batch({"p1.png": _png_bytes(1)}, detect_side_effect=self.detect)
        self.assertEqual(self.faces_for("p1.png"), [], "nothing is recorded for a photo that never finalized")
        c = self.counters()
        self.assertEqual((c["processed_photos"], c["failed_photos"], c["recognized_photos"], c["faces_detected"]),
                         (0, 1, 0, 0))
        self.assertEqual(self.folder_count(), 0)

    def test_a_failed_retry_changes_nothing(self):
        with self.fail_media_for("p1.png"):
            self.run_batch({"p1.png": _png_bytes(1)}, detect_side_effect=self.detect)
        before_counters, before_rows = self.counters(), len(self.faces_for("p1.png"))
        with self.fail_media_for("p1.png"):
            self.retry()
        self.assertEqual(self.counters(), before_counters)
        self.assertEqual(len(self.faces_for("p1.png")), before_rows)
        self.assertEqual(self.folder_count(), 0)

    def test_a_db_failure_inside_the_finalize_commit_rolls_everything_back(self):
        """Face rows had already been replaced in the transaction when it
        failed — the rollback must undo them along with every counter."""
        with self.fail_inside_finalize_commit():
            self.run_batch({"p1.png": _png_bytes(1)}, detect_side_effect=self.detect)
        self.assertEqual(self.faces_for("p1.png"), [])
        c = self.counters()
        self.assertEqual((c["processed_photos"], c["failed_photos"], c["recognized_photos"]), (0, 1, 0))
        # and the photo is genuinely retriable afterwards
        self.retry()
        self.assertEqual(self.counters(), self.expected_for(1))
        self.assertEqual(len(self.faces_for("p1.png")), 2)

    def test_a_db_failure_during_retry_changes_nothing(self):
        with self.fail_media_for("p1.png"):
            self.run_batch({"p1.png": _png_bytes(1)}, detect_side_effect=self.detect)
        before = self.counters()
        with self.fail_inside_finalize_commit():
            self.retry()
        self.assertEqual(self.counters(), before)
        self.assertEqual(self.faces_for("p1.png"), [])


class RepeatedRetryTests(RetryIdempotencyTestCase):
    def test_two_failed_retries_then_success_counts_exactly_once(self):
        with self.fail_media_for("p1.png"):
            self.run_batch({"p1.png": _png_bytes(1), "p2.png": _png_bytes(2)}, detect_side_effect=self.detect)
        with self.fail_media_for("p1.png"):
            self.retry()
        with self.fail_inside_finalize_commit():
            self.retry()
        self.retry()
        self.assertEqual(self.counters(), self.expected_for(2))
        self.assertEqual(len(self.faces_for("p1.png")), 2)
        self.assertEqual(len(self.faces_for("p2.png")), 2)
        self.assertEqual(self.folder_count(), 2, "one SORTED membership per finalized photo")

    def test_retrying_a_fully_finalized_batch_is_a_no_op(self):
        self.run_batch({"p1.png": _png_bytes(1)}, detect_side_effect=self.detect)
        before, rows = self.counters(), len(self.faces_for("p1.png"))
        self.retry()
        self.assertEqual(self.counters(), before)
        self.assertEqual(len(self.faces_for("p1.png")), rows)


class LegacyLeftoverTests(RetryIdempotencyTestCase):
    def test_stale_rows_from_an_old_failed_attempt_are_replaced_not_duplicated(self):
        """Batches processed by the pre-fix code can carry face rows from a
        failed attempt. The successful finalize replaces them in the same
        commit instead of adding a second set."""
        with self.fail_media_for("p1.png"):
            self.run_batch({"p1.png": _png_bytes(1)}, detect_side_effect=self.detect)
        with Session(self.engine) as session:
            photo = session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.filename == "p1.png")).one()
            for _ in range(3):
                session.add(PhotoBatchFace(photo_id=photo.id, person_id=None, confidence=0.1,
                                           bbox="0,0,1,1", consent_status_at_processing="no_match"))
            session.commit()
        self.retry()
        rows = self.faces_for("p1.png")
        self.assertEqual(len(rows), 2)
        self.assertNotIn("0,0,1,1", [r.bbox for r in rows])


class AmbienceTests(RetryIdempotencyTestCase):
    def test_an_ambience_retry_is_counted_once(self):
        no_faces = lambda img: []
        with self.fail_media_for("a.png"):
            self.run_batch({"a.png": _png_bytes(3)}, detect_side_effect=no_faces)
        with patch.object(pps, "detect_faces", side_effect=no_faces):
            pps.retry_unresolved_photos(self.batch_id)
        c = self.counters()
        self.assertEqual((c["processed_photos"], c["failed_photos"], c["ambience_photos"], c["recognized_photos"]),
                         (1, 0, 1, 0))


if __name__ == "__main__":
    unittest.main()
