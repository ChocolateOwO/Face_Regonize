"""Follow-up Task 3 — who a bounding box belongs to (information display only).

The D1 faces payload gains a minimal participant summary from the STORED
PhotoBatchFace.person_id relation. Recognition is never re-run, no embedding
and no ORIGINAL location is ever returned, and the privacy value shown is the
processing-time snapshot.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine

from app.models.models import ConsentRecord, Person, PhotoBatch, PhotoBatchFace, PhotoBatchPhoto
from app.services import face_geometry_service as fgs

BID = "e" * 32


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-face-panel-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        original = self.storage / "photo_batches" / BID / "ORIGINAL" / "g.png"
        original.parent.mkdir(parents=True)
        original.write_bytes(cv2.imencode(".png", np.zeros((300, 400, 3), np.uint8))[1].tobytes())
        self.engine = create_engine(f"sqlite:///{(Path(self.temp.name) / 't.db').as_posix()}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            alice = Person(participant_id="0007", first_name="Alice", last_name="Anders", email="a@x.test",
                           image_path="people/a/profile.jpg", embedding=np.ones(512, np.float32).tobytes())
            bob = Person(participant_id="0008", first_name="Bob", last_name="Brown", image_path="", embedding=b"\1" * 2048)
            s.add(alice)
            s.add(bob)
            s.add(PhotoBatch(id=BID, label="panel", drive_folder_id="", storage_dir=f"photo_batches/{BID}", status="ready"))
            photo = PhotoBatchPhoto(batch_id=BID, filename="g.png", drive_file_id="",
                                    original_path=f"photo_batches/{BID}/ORIGINAL/g.png",
                                    media_path=f"photo_batches/{BID}/MEDIA/g.png")
            s.add(photo)
            s.commit()
            self.alice_id, self.bob_id, self.photo_id = alice.id, bob.id, photo.id
            rows = [
                ("alice", alice.id, 0.7134, "10,10,60,60", "consented"),
                ("bob", bob.id, 0.66, "100,10,150,60", "declined"),
                ("unknown", None, -1.0, "10,100,60,150", "no_match"),
                ("ghost", "person-deleted-since", 0.58, "100,100,150,150", "consented"),
            ]
            self.face_ids = {}
            for key, person_id, conf, bbox, consent in rows:
                f = PhotoBatchFace(photo_id=photo.id, person_id=person_id, confidence=conf, bbox=bbox,
                                   consent_status_at_processing=consent, blurred=consent == "declined")
                s.add(f)
                self.face_ids[key] = f.id
            s.commit()

    def payload(self):
        with Session(self.engine) as s:
            batch, photo = s.get(PhotoBatch, BID), s.get(PhotoBatchPhoto, self.photo_id)
            with patch("app.face_recognition.index.RecognitionIndex.match_batch",
                       side_effect=AssertionError("recognition must not re-run")):
                return fgs.faces_payload(s, batch, photo, self.storage)

    def face(self, key, payload=None):
        return next(f for f in (payload or self.payload())["faces"] if f["id"] == self.face_ids[key])


class IdentityTests(PanelCase):
    def test_a_matched_face_returns_the_correct_minimal_summary(self):
        f = self.face("alice")
        self.assertEqual(f["identity"], "matched")
        self.assertEqual(f["participant"], {"person_id": self.alice_id, "participant_id": "0007", "name": "Alice Anders"})
        self.assertEqual(f["consent_status_at_processing"], "consented")
        self.assertEqual(f["match_confidence"], 0.7134)
        self.assertFalse(f["mask_required"])

    def test_an_unknown_face_has_no_identity(self):
        f = self.face("unknown")
        self.assertEqual((f["identity"], f["participant"], f["match_confidence"]), ("unknown", None, None))

    def test_a_deleted_participant_is_handled_safely(self):
        f = self.face("ghost")
        self.assertEqual((f["identity"], f["participant"]), ("deleted", None))
        self.assertEqual(f["bbox"], [100.0, 100.0, 150.0, 150.0])
        self.assertEqual(f["consent_status_at_processing"], "consented")
        self.assertNotIn("person-deleted-since", json.dumps(self.payload()))

    def test_a_declined_face_stays_masked(self):
        f = self.face("bob")
        self.assertTrue(f["mask_required"])
        self.assertTrue(f["masked"])

    def test_the_snapshot_is_shown_not_live_consent(self):
        with Session(self.engine) as s:
            s.add(ConsentRecord(person_id=self.alice_id, choice="declined", source="admin"))
            s.commit()
        f = self.face("alice")
        self.assertEqual(f["consent_status_at_processing"], "consented")
        self.assertFalse(f["mask_required"], "masking follows the processing-time snapshot")

    def test_boxes_are_numbered_top_to_bottom_left_to_right(self):
        numbers = {k: self.face(k)["number"] for k in self.face_ids}
        self.assertEqual(numbers, {"alice": 1, "bob": 2, "unknown": 3, "ghost": 4})


class SafetyTests(PanelCase):
    def test_no_embedding_original_or_extra_participant_data_is_returned(self):
        text = json.dumps(self.payload())
        for forbidden in ("embedding", "ORIGINAL", "original_path", "a@x.test", "profile.jpg", "email", "image_path"):
            self.assertNotIn(forbidden, text)

    def test_reading_the_payload_never_changes_geometry(self):
        before = [f["bbox"] for f in self.payload()["faces"]]
        self.payload()
        self.assertEqual([f["bbox"] for f in self.payload()["faces"]], before)
        with Session(self.engine) as s:
            self.assertEqual(s.get(PhotoBatchFace, self.face_ids["alice"]).detected_bbox, None)

    def test_the_route_stays_admin_authenticated(self):
        import inspect

        from app.api.photo_batches import get_photo_faces

        self.assertIn("get_current_user", str(inspect.signature(get_photo_faces)))


if __name__ == "__main__":
    unittest.main()
