"""Step 2 — `recognized_photos` means "photos containing at least one
identified participant": a PHOTO count — not faces, not participants.

Pinned on both processing paths with the case that distinguishes the readings:
one photo holding three identified participants and one stranger must add
exactly +1 (the Review-era meaning would have added 0, a face count 3, and a
participant count 3).
"""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session

import app.services.event_pipeline_service as eps
import app.services.photo_processing_service as pps
from app.models.models import ConsentRecord, Person
from tests.test_event_photo_completeness import CompletenessTestCase, _png_bytes
from tests.test_event_pipeline_service import FakeSource, PipelineTestCase, _fake_source_item, _image


def _face(i: int):
    emb = np.zeros(512, dtype=np.float32)
    emb[i] = 1.0
    return SimpleNamespace(embedding=emb, bbox=(2.0 + 5 * i, 2.0, 12.0 + 5 * i, 12.0), det_score=0.9)


def _people(engine, n: int) -> list[str]:
    ids = []
    with Session(engine) as session:
        for i in range(n):
            p = Person(participant_id=f"9{i:03d}", first_name=f"P{i}", last_name="", image_path="",
                       embedding=np.zeros(512, dtype=np.float32).tobytes())
            session.add(p)
            session.commit()
            session.refresh(p)
            session.add(ConsentRecord(person_id=p.id, choice="consented", source="test"))
            session.commit()
            ids.append(p.id)
    return ids


def _matches(ids):
    """Three identified faces + one stranger, in detection order."""
    return [(pid, "P", "P", "9000", 0.9) for pid in ids] + [(None, None, None, None, -1.0)]


class SequentialPathTests(CompletenessTestCase):
    def test_three_identified_people_and_a_stranger_count_as_one_photo(self):
        ids = _people(self.engine, 3)
        with patch.object(pps.recognition_index, "match_batch", side_effect=lambda e, t: _matches(ids)):
            self.run_batch({"group.png": _png_bytes(7)}, detect_side_effect=lambda img: [_face(i) for i in range(4)])
        b = self.batch()
        self.assertEqual(b.recognized_photos, 1, "a photo count — not 3 faces or 3 participants")
        self.assertEqual(b.faces_recognized, 3)
        self.assertEqual(b.faces_unknown, 1)

    def test_a_photo_of_only_strangers_is_not_recognized(self):
        with patch.object(pps.recognition_index, "match_batch",
                          side_effect=lambda e, t: [(None, None, None, None, -1.0)] * 2):
            self.run_batch({"crowd.png": _png_bytes(8)}, detect_side_effect=lambda img: [_face(0), _face(1)])
        b = self.batch()
        self.assertEqual((b.recognized_photos, b.processed_photos, b.ambience_photos), (0, 1, 0))


class PipelinePathTests(PipelineTestCase):
    def test_three_identified_people_and_a_stranger_count_as_one_photo(self):
        ids = _people(self.engine, 3)
        source = FakeSource({"group.png": cv2.imencode(".png", _image(7))[1].tobytes()})
        with patch.object(eps, "detect_event_faces", side_effect=lambda img: [_face(i) for i in range(4)]), \
             patch.object(eps.recognition_index, "match_batch", side_effect=lambda e, t: _matches(ids)):
            self.make_pipeline(source).run([(1, _fake_source_item("group.png"), "group.png")],
                                           self.batch_storage_dir, self.batch_id)
        b = self.batch()
        self.assertEqual(b.recognized_photos, 1, "a photo count — not 3 faces or 3 participants")
        self.assertEqual(b.faces_recognized, 3)


if __name__ == "__main__":
    unittest.main()
