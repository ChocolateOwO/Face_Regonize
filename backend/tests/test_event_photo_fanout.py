"""Phase D0 — Derived Output Fan-Out.

Confirms the live privacy gap is closed: MEDIA/SORTED/AMBIENCE are
now the SAME finalized (privacy-rendered + logo'd) bytes, fanned out by
plain copy — never a raw ORIGINAL copy in any derived folder, and never a
second render. Runs the real _run_photo_batch with only Drive/detection/
matching faked, same isolation approach as test_event_photo_completeness.py.
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine, select

import app.services.photo_processing_service as pps
from app.models.models import ConsentRecord, Person, PhotoBatch, PhotoBatchFace, PhotoBatchPhoto


def _test_image() -> np.ndarray:
    return np.random.default_rng(7).integers(40, 200, (120, 160, 3), dtype=np.uint8)


def _fake_face(seed: int, bbox):
    return SimpleNamespace(embedding=np.zeros(512, dtype=np.float32) + seed, bbox=bbox)


def _fake_file(name: str):
    return SimpleNamespace(id=f"drive-{name}", name=name)


class FanOutTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-fanout-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.photo_batches_dir = self.storage / "photo_batches"
        db_path = Path(self.temp.name) / "test.db"

        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        for target, value in (
            ("engine", self.engine), ("STORAGE_PATH", self.storage), ("PHOTO_BATCHES_DIR", self.photo_batches_dir),
        ):
            p = patch.object(pps, target, value)
            p.start()
            self.addCleanup(p.stop)

        patch.object(pps.settings_cache, "get_threshold", return_value=0.45).start()
        self.addCleanup(patch.stopall)
        patch("app.services.maintenance.is_active", return_value=False).start()

        self.batch_id = "a" * 32
        with Session(self.engine) as session:
            dummy_embedding = np.zeros(512, dtype=np.float32).tobytes()
            self.person_a = Person(participant_id="0001", first_name="Aiko", last_name="", image_path="", embedding=dummy_embedding)
            self.person_b = Person(participant_id="0002", first_name="Beam", last_name="", image_path="", embedding=dummy_embedding)
            session.add(self.person_a)
            session.add(self.person_b)
            session.commit()
            session.add(ConsentRecord(person_id=self.person_a.id, choice="consented"))
            session.add(ConsentRecord(person_id=self.person_b.id, choice="declined"))
            session.add(PhotoBatch(
                id=self.batch_id, label="test", drive_folder_id="fake-folder",
                storage_dir=f"photo_batches/{self.batch_id}",
            ))
            session.commit()
            session.refresh(self.person_a)
            session.refresh(self.person_b)

        # A real LOGO/config.json + logo.png, loaded via the actual
        # _load_logo_config() call inside _run_photo_batch — not faked.
        logo_dir = self.photo_batches_dir / self.batch_id / "LOGO"
        logo_dir.mkdir(parents=True)
        logo_img = np.full((10, 10, 4), 255, dtype=np.uint8)  # opaque white square
        cv2.imwrite(str(logo_dir / "logo.png"), logo_img)
        (logo_dir / "config.json").write_text(json.dumps({"position": "bottom-right", "size": 0.1}), encoding="utf-8")

    def batch(self) -> PhotoBatch:
        with Session(self.engine) as session:
            return session.get(PhotoBatch, self.batch_id)

    def faces(self) -> list[PhotoBatchFace]:
        with Session(self.engine) as session:
            photo_ids = {p.id for p in session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == self.batch_id)).all()}
            return [f for f in session.exec(select(PhotoBatchFace)).all() if f.photo_id in photo_ids]


class MultiPersonFanOutTests(FanOutTestCase):
    """One photo: consented A, declined B, one unmatched face — the exact
    scenario the live gap affected."""

    BBOX_A = (10.0, 10.0, 30.0, 30.0)
    BBOX_B = (60.0, 60.0, 80.0, 80.0)
    BBOX_UNKNOWN = (110.0, 10.0, 130.0, 30.0)

    def _run(self):
        image = _test_image()
        file_bytes = cv2.imencode(".png", image)[1].tobytes()
        files = [_fake_file("group.png")]

        detect_mock = MagicMock(return_value=[
            _fake_face(1, self.BBOX_A), _fake_face(2, self.BBOX_B), _fake_face(3, self.BBOX_UNKNOWN),
        ])
        match_mock = MagicMock(return_value=[
            (self.person_a.id, "Aiko", "Aiko", "0001", 0.91),
            (self.person_b.id, "Beam", "Beam", "0002", 0.88),
            (None, None, None, None, -1.0),
        ])

        with patch.object(pps, "list_image_files", return_value=files), \
             patch.object(pps, "download_file", return_value=file_bytes), \
             patch.object(pps, "detect_faces", detect_mock), \
             patch.object(pps.recognition_index, "match_batch", match_mock):
            pps._run_photo_batch(self.batch_id)

        return detect_mock, match_mock

    def test_sorted_copies_carry_the_blurred_declined_face_not_raw_bytes(self):
        self._run()
        batch = self.batch()
        self.assertEqual(batch.status, "ready")

        folder_a = self.storage / f"{batch.storage_dir}/SORTED/0001_Aiko"
        folder_b = self.storage / f"{batch.storage_dir}/SORTED/0002_Beam"
        media_path = self.storage / f"{batch.storage_dir}/MEDIA/group.png"

        media_bytes = media_path.read_bytes()
        # Every fan-out destination is byte-IDENTICAL — one render, not several.
        self.assertEqual((folder_a / "group.png").read_bytes(), media_bytes)
        self.assertEqual((folder_b / "group.png").read_bytes(), media_bytes)
        # An unrecognized face produces no REVIEW/ output: that workflow is
        # removed, and the photo is delivered normally instead.
        self.assertFalse((self.storage / f"{batch.storage_dir}/REVIEW").exists())

        # The declined participant's face is genuinely blurred in the copy
        # that lives inside the CONSENTED participant's own SORTED folder —
        # the exact live gap this phase closes.
        rendered = cv2.imdecode(np.frombuffer((folder_a / "group.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        original = cv2.imdecode(np.frombuffer(cv2.imencode(".png", _test_image())[1].tobytes(), np.uint8), cv2.IMREAD_COLOR)
        x1, y1, x2, y2 = (int(v) for v in self.BBOX_B)
        self.assertFalse(np.array_equal(original[y1:y2, x1:x2], rendered[y1:y2, x1:x2]))
        # The consented participant's OWN face region is untouched.
        ax1, ay1, ax2, ay2 = (int(v) for v in self.BBOX_A)
        np.testing.assert_array_equal(original[ay1:ay2, ax1:ax2], rendered[ay1:ay2, ax1:ax2])

    def test_logo_present_in_every_fan_out_destination(self):
        self._run()
        batch = self.batch()
        # image is 120x160, logo config size=0.1 -> 16x16, pad=4, bottom-right:
        # x=160-16-4=140, y=120-16-4=100 (see _apply_logo's own placement math).
        logo_corner = (slice(100, 116), slice(140, 156))

        for relative in ("MEDIA/group.png", "SORTED/0001_Aiko/group.png", "SORTED/0002_Beam/group.png"):
            img = cv2.imdecode(np.frombuffer((self.storage / f"{batch.storage_dir}/{relative}").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            region = img[logo_corner]
            self.assertTrue(np.all(region >= 250), f"{relative} missing the opaque white logo in its corner")

    def test_detection_and_matching_each_called_exactly_once(self):
        detect_mock, match_mock = self._run()
        self.assertEqual(detect_mock.call_count, 1)
        self.assertEqual(match_mock.call_count, 1)


class AmbienceFanOutTests(FanOutTestCase):
    def test_ambience_and_media_are_byte_identical_with_logo(self):
        image = _test_image()
        file_bytes = cv2.imencode(".png", image)[1].tobytes()
        with patch.object(pps, "list_image_files", return_value=[_fake_file("empty.png")]), \
             patch.object(pps, "download_file", return_value=file_bytes), \
             patch.object(pps, "detect_faces", return_value=[]):
            pps._run_photo_batch(self.batch_id)

        batch = self.batch()
        ambience_bytes = (self.storage / f"{batch.storage_dir}/AMBIENCE/empty.png").read_bytes()
        media_bytes = (self.storage / f"{batch.storage_dir}/MEDIA/empty.png").read_bytes()
        self.assertEqual(ambience_bytes, media_bytes)
        # Not a raw copy of the original — the logo was actually composited.
        self.assertNotEqual(ambience_bytes, file_bytes)


if __name__ == "__main__":
    unittest.main()
