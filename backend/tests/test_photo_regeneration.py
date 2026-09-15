"""Targeted single-photo regeneration: privacy invariants and every rollback path.

Ported from the (removed) Review decision tests — the transactional core they
covered survives as `photo_regeneration_service`, and the guarantee being
tested is precisely what happens when things go wrong partway, so each failure
mode is injected for real and the resulting on-disk + DB state is asserted.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine

from app.models.models import (
    ConsentRecord,
    Person,
    PhotoBatch,
    PhotoBatchFace,
    PhotoBatchPhoto,
)
from app.services import photo_regeneration_service as prs


def _png(seed: int, size: int = 120) -> bytes:
    img = np.random.default_rng(seed).integers(0, 255, (size, size, 3), dtype=np.uint8)
    return cv2.imencode(".png", img)[1].tobytes()


class RegenerationTestCase(unittest.TestCase):
    """One batch, one photo, two faces (one declined participant, one unknown),
    already fanned out to MEDIA and the declined participant's SORTED folder."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-regen-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.photo_batches_dir = self.storage / "photo_batches"

        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)

        self.declined = self._person("0001", "Dana", "Declined", "declined")
        self.consented = self._person("0002", "Cody", "Consented", "consented")

        self.batch = PhotoBatch(label="B", drive_folder_id="", storage_dir="")
        self.session.add(self.batch)
        self.session.commit()
        self.session.refresh(self.batch)
        self.batch.storage_dir = f"photo_batches/{self.batch.id}"
        self.session.add(self.batch)
        self.session.commit()

        root = self.storage / self.batch.storage_dir
        for sub in ("ORIGINAL", "MEDIA", "SORTED", "AMBIENCE"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "ORIGINAL" / "p.png").write_bytes(_png(1))

        self.photo = PhotoBatchPhoto(
            batch_id=self.batch.id, filename="p.png", drive_file_id="",
            original_path=f"{self.batch.storage_dir}/ORIGINAL/p.png",
            media_path=f"{self.batch.storage_dir}/MEDIA/p.png",
            classification="sorted",
        )
        self.session.add(self.photo)
        self.session.commit()
        self.session.refresh(self.photo)

        self.declined_face = self._face(self.declined.id, "declined", "10,10,40,40")
        self.unknown_face = self._face(None, "no_match", "60,60,90,90")

        self.media = root / "MEDIA" / "p.png"
        self.legacy_review = root / "REVIEW" / "p.png"
        self.sorted_declined = self._sorted_path(self.declined)
        for path in (self.media, self.sorted_declined):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"OLD-CONTENT")

    def _person(self, participant_id, first, last, choice):
        p = Person(participant_id=participant_id, first_name=first, last_name=last,
                   image_path="", embedding=b"")
        self.session.add(p)
        self.session.commit()
        self.session.refresh(p)
        self.session.add(ConsentRecord(person_id=p.id, choice=choice, source="test"))
        self.session.commit()
        return p

    def _face(self, person_id, consent, bbox):
        f = PhotoBatchFace(photo_id=self.photo.id, person_id=person_id, confidence=0.9,
                           bbox=bbox, consent_status_at_processing=consent)
        self.session.add(f)
        self.session.commit()
        self.session.refresh(f)
        return f

    def _sorted_path(self, person):
        from app.services.photo_processing_service import _safe_folder_name
        folder = _safe_folder_name(person.participant_id, person.first_name, person.last_name)
        return self.photo_batches_dir / self.batch.id / "SORTED" / folder / "p.png"

    def _add_legacy_review_file(self):
        self.legacy_review.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_review.write_bytes(b"OLD-CONTENT")

    def faces(self):
        return [self.declined_face, self.unknown_face]

    def regenerate(self, **kwargs):
        return prs.regenerate_photo(
            self.session, self.batch, self.photo, self.faces(),
            storage_path=self.storage, photo_batches_dir=self.photo_batches_dir, **kwargs,
        )

    def live_state(self):
        return {p: (p.read_bytes() if p.is_file() else None)
                for p in (self.media, self.legacy_review, self.sorted_declined,
                          self._sorted_path(self.consented))}


class HardPrivacyRuleTests(RegenerationTestCase):
    def test_declined_participant_always_requires_a_mask(self):
        self.assertTrue(prs.privacy_requires_mask(self.declined_face))
        self.assertFalse(prs.privacy_requires_mask(self.unknown_face),
                         "an unknown face is visible; denial is never inferred")

    def test_declined_face_is_masked_regardless_of_a_stored_override(self):
        self.declined_face.manual_mask = False
        self.assertTrue(prs.face_is_masked(self.declined_face),
                        "no stored override may reveal a declined participant")

    def test_a_pending_participant_is_visible(self):
        self.unknown_face.person_id = self.consented.id
        self.unknown_face.consent_status_at_processing = "pending"
        self.assertFalse(prs.face_is_masked(self.unknown_face))

    def test_consent_comes_from_the_stored_snapshot_not_live_pdpa(self):
        """The participant later withdraws consent; the frozen snapshot on the
        face is what the render must keep using."""
        self.unknown_face.person_id = self.consented.id
        self.unknown_face.consent_status_at_processing = "consented"
        self.session.add(ConsentRecord(person_id=self.consented.id, choice="declined", source="test"))
        self.session.commit()
        self.assertFalse(prs.face_is_masked(self.unknown_face),
                         "regeneration must not re-read live consent")


class DestinationTests(RegenerationTestCase):
    def test_media_and_each_matched_participant_get_the_same_bytes(self):
        self.unknown_face.person_id = self.consented.id
        self.unknown_face.consent_status_at_processing = "consented"
        finalized = self.regenerate()
        state = self.live_state()
        self.assertEqual(state[self.media], finalized)
        self.assertNotEqual(state[self.media], b"OLD-CONTENT", "MEDIA was regenerated")
        self.assertEqual(state[self.media], state[self.sorted_declined], "one render, fanned out")
        self.assertEqual(state[self.media], state[self._sorted_path(self.consented)])

    def test_no_review_output_is_created(self):
        self.regenerate()
        self.assertFalse(self.legacy_review.exists(),
                         "the Review workflow is removed; no REVIEW/ output is generated")

    def test_an_existing_legacy_review_file_is_corrected_not_left_stale(self):
        """A pre-removal batch may still have one on disk. Leaving it behind
        would keep an out-of-date render in the delivered tree."""
        self._add_legacy_review_file()
        finalized = self.regenerate()
        self.assertEqual(self.legacy_review.read_bytes(), finalized)

    def test_ambience_is_written_only_when_the_photo_has_no_faces(self):
        ambience = self.storage / self.batch.storage_dir / "AMBIENCE" / "p.png"
        self.regenerate()
        self.assertFalse(ambience.exists())
        finalized = prs.regenerate_photo(
            self.session, self.batch, self.photo, [],
            storage_path=self.storage, photo_batches_dir=self.photo_batches_dir)
        self.assertEqual(ambience.read_bytes(), finalized)

    def test_a_stale_participant_folder_copy_is_removed(self):
        self.declined_face.person_id = self.consented.id
        self.declined_face.consent_status_at_processing = "consented"
        self.regenerate(stale_person_ids=[self.declined.id])
        self.assertFalse(self.sorted_declined.is_file(),
                         "the previous participant's SORTED copy is removed")
        self.assertTrue(self._sorted_path(self.consented).is_file())

    def test_a_still_matched_participant_is_never_treated_as_stale(self):
        self.regenerate(stale_person_ids=[self.declined.id])
        self.assertTrue(self.sorted_declined.is_file(),
                        "the participant still has a face in this photo")


class RollbackTests(RegenerationTestCase):
    """Every failure path, injected for real."""

    def setUp(self):
        super().setUp()
        self._add_legacy_review_file()
        # a second matched participant, so several destinations are in play
        self.unknown_face.person_id = self.consented.id
        self.unknown_face.consent_status_at_processing = "consented"

    def test_failure_on_the_SECOND_destination_restores_the_first(self):
        calls = {"n": 0}
        real = prs._atomic_replace

        def flaky(temp, dest):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk gave up on destination 2")
            return real(temp, dest)

        before = self.live_state()
        with patch.object(prs, "_atomic_replace", flaky):
            with self.assertRaises(prs.RegenerationError):
                self.regenerate()

        self.assertEqual(self.live_state(), before,
                         "the already-replaced destination must be restored to pre-change content")

    def test_commit_failure_after_every_file_landed_restores_all_files(self):
        """The one remaining window: files are new, the DB is not. All-old is
        the required outcome — never new files paired with a rolled-back DB."""
        before = self.live_state()

        def commit_that_fails():
            raise RuntimeError("database is locked")

        with self.assertRaises(prs.RegenerationError):
            self.regenerate(commit=commit_that_fails)

        self.assertEqual(self.live_state(), before,
                         "a commit failure must restore ALL files to pre-change state")

    def test_a_successful_commit_keeps_the_new_files(self):
        calls = {"n": 0}

        def commit():
            calls["n"] += 1

        finalized = self.regenerate(commit=commit)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.media.read_bytes(), finalized)

    def test_missing_original_is_refused_without_touching_anything(self):
        (self.storage / self.photo.original_path).unlink()
        before = self.live_state()
        with self.assertRaises(prs.RegenerationError):
            self.regenerate()
        self.assertEqual(self.live_state(), before)

    def test_no_temp_files_are_left_behind_after_a_failure(self):
        def always_fails(temp, dest):
            raise OSError("nope")

        with patch.object(prs, "_atomic_replace", always_fails):
            with self.assertRaises(prs.RegenerationError):
                self.regenerate()
        leftovers = list((self.storage / self.batch.storage_dir).rglob(f"*{prs.TEMP_SUFFIX}"))
        self.assertEqual(leftovers, [], "temp files must be discarded on failure")

    def test_no_temp_files_are_left_behind_after_success(self):
        self.regenerate()
        leftovers = list((self.storage / self.batch.storage_dir).rglob(f"*{prs.TEMP_SUFFIX}"))
        self.assertEqual(leftovers, [])

    def test_no_partial_visible_state_after_failure(self):
        """Whatever fails, the delivered set is internally consistent: every
        destination holds the SAME content as before the attempt."""
        calls = {"n": 0}
        real = prs._atomic_replace

        def flaky(temp, dest):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("boom on the third")
            return real(temp, dest)

        with patch.object(prs, "_atomic_replace", flaky):
            with self.assertRaises(prs.RegenerationError):
                self.regenerate()

        contents = {v for v in self.live_state().values() if v is not None}
        self.assertEqual(contents, {b"OLD-CONTENT"},
                         "no destination may hold post-change content after a rollback")

    def test_a_removal_failure_also_rolls_the_replacements_back(self):
        self.declined_face.person_id = self.consented.id
        self.declined_face.consent_status_at_processing = "consented"
        before = self.live_state()

        def refuse(path):
            raise OSError("cannot unlink")

        with patch.object(prs, "_remove_file", refuse):
            with self.assertRaises(prs.RegenerationError):
                self.regenerate(stale_person_ids=[self.declined.id])

        # restore_all uses _remove_file for paths that did not exist before;
        # the pre-existing ones must at least be back to their old content.
        state = self.live_state()
        self.assertEqual(state[self.media], before[self.media])
        self.assertEqual(state[self.sorted_declined], before[self.sorted_declined])


if __name__ == "__main__":
    unittest.main()
