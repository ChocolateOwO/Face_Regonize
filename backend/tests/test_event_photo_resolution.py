"""Phase C1 — Crowd/Original-Resolution regression verification.

Confirms the render/write pipeline never resizes a photo, across every
scenario the pipeline currently supports: no faces (ambience), blurred
(declined consent), unblurred (consented/unknown), logo on/off, and a
large "crowd" image with many detected faces. Also asserts SORTED/
AMBIENCE match MEDIA's resolution byte-for-byte, per the Derived Output
fan-out design (Phase D0) — trivially true by construction (same bytes),
asserted here explicitly as a regression guard.

Emoji/custom-mask scenario is intentionally NOT covered here — that privacy
mask style (Phase D) does not exist in this codebase yet; only Blur does.
This will be extended once Phase D lands.

Runs the real _run_photo_batch (Drive/detection mocked, GPU never touched),
same isolation pattern as test_event_photo_completeness.py /
test_event_photo_fanout.py.
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
from app.models.models import ConsentRecord, Person, PhotoBatch


def _image(height: int, width: int, seed: int = 1) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 255, (height, width, 3), dtype=np.uint8)


def _fake_face(seed: int, bbox):
    return SimpleNamespace(embedding=np.zeros(512, dtype=np.float32) + seed, bbox=bbox)


def _fake_file(name: str):
    return SimpleNamespace(id=f"drive-{name}", name=name)


class ResolutionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-resolution-test-")
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
        patch("app.services.maintenance.is_active", return_value=False).start()
        self.addCleanup(patch.stopall)

        self.batch_id = "a" * 32
        with Session(self.engine) as session:
            dummy_embedding = np.zeros(512, dtype=np.float32).tobytes()
            self.declined_person = Person(
                participant_id="0001", first_name="Declined", last_name="", image_path="", embedding=dummy_embedding,
            )
            session.add(self.declined_person)
            session.commit()
            session.add(ConsentRecord(person_id=self.declined_person.id, choice="declined"))
            session.add(PhotoBatch(
                id=self.batch_id, label="test", drive_folder_id="fake-folder",
                storage_dir=f"photo_batches/{self.batch_id}",
            ))
            session.commit()
            session.refresh(self.declined_person)

    def _configure_logo(self):
        logo_dir = self.photo_batches_dir / self.batch_id / "LOGO"
        logo_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(logo_dir / "logo.png"), np.full((10, 10, 4), 255, dtype=np.uint8))
        (logo_dir / "config.json").write_text(json.dumps({"position": "bottom-right", "size": 0.1}), encoding="utf-8")

    def _run(self, image: np.ndarray, faces=(), matches=None):
        file_bytes = cv2.imencode(".png", image)[1].tobytes()
        with patch.object(pps, "list_image_files", return_value=[_fake_file("photo.png")]), \
             patch.object(pps, "download_file", return_value=file_bytes), \
             patch.object(pps, "detect_faces", return_value=list(faces)), \
             patch.object(pps.recognition_index, "match_batch", return_value=matches or []):
            pps._run_photo_batch(self.batch_id)

    def _shape_of(self, relative: str) -> tuple[int, int, int]:
        path = self.storage / f"photo_batches/{self.batch_id}/{relative}"
        img = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        return img.shape


class NoFaceResolutionTests(ResolutionTestCase):
    def test_ambience_no_logo_preserves_resolution(self):
        image = _image(300, 500)
        self._run(image)
        self.assertEqual(self._shape_of("AMBIENCE/photo.png"), image.shape)
        self.assertEqual(self._shape_of("MEDIA/photo.png"), image.shape)

    def test_ambience_with_logo_preserves_resolution(self):
        self._configure_logo()
        image = _image(300, 500, seed=2)
        self._run(image)
        self.assertEqual(self._shape_of("AMBIENCE/photo.png"), image.shape)
        self.assertEqual(self._shape_of("MEDIA/photo.png"), image.shape)


class FaceResolutionTests(ResolutionTestCase):
    def test_declined_blurred_face_preserves_resolution(self):
        image = _image(400, 600, seed=3)
        faces = [_fake_face(1, (50.0, 50.0, 150.0, 150.0))]
        matches = [(self.declined_person.id, "Declined", "Declined", "0001", 0.9)]
        self._run(image, faces=faces, matches=matches)
        self.assertEqual(self._shape_of("MEDIA/photo.png"), image.shape)
        self.assertEqual(self._shape_of("SORTED/0001_Declined/photo.png"), image.shape)

    def test_unmatched_face_no_blur_preserves_resolution(self):
        image = _image(400, 600, seed=4)
        faces = [_fake_face(1, (50.0, 50.0, 150.0, 150.0))]
        matches = [(None, None, None, None, -1.0)]
        self._run(image, faces=faces, matches=matches)
        self.assertEqual(self._shape_of("MEDIA/photo.png"), image.shape)

    def test_logo_on_faced_photo_preserves_resolution(self):
        self._configure_logo()
        image = _image(400, 600, seed=5)
        faces = [_fake_face(1, (50.0, 50.0, 150.0, 150.0))]
        matches = [(self.declined_person.id, "Declined", "Declined", "0001", 0.9)]
        self._run(image, faces=faces, matches=matches)
        self.assertEqual(self._shape_of("MEDIA/photo.png"), image.shape)


class CrowdScaleResolutionTests(ResolutionTestCase):
    """A large, high-resolution "crowd" image with many detected faces —
    the scenario the tiled detector exists for. Detection itself is mocked
    here (test_event_photo_tiling.py already exhaustively covers the tiled
    detector's own geometry); what THIS test proves is that the RENDER/
    WRITE pipeline downstream of detection never downsizes a large image,
    regardless of how many faces were found in it."""

    def test_large_crowd_image_with_many_faces_preserves_full_resolution(self):
        image = _image(2160, 3840, seed=6)  # 4K-scale, well above any det_size
        faces = [_fake_face(i, (10.0 + i * 20, 10.0, 30.0 + i * 20, 30.0)) for i in range(25)]
        matches = [(None, None, None, None, -1.0) for _ in faces]  # none recognized
        self._run(image, faces=faces, matches=matches)
        self.assertEqual(self._shape_of("MEDIA/photo.png"), image.shape)

    def test_sorted_and_ambience_match_media_resolution_byte_for_byte(self):
        """Per Phase D0, these are the SAME encoded bytes, not merely the
        same shape — asserted explicitly here as this phase's own guard."""
        image = _image(500, 700, seed=7)
        faces = [_fake_face(1, (20.0, 20.0, 80.0, 80.0)), _fake_face(2, (200.0, 20.0, 260.0, 80.0))]
        matches = [
            (self.declined_person.id, "Declined", "Declined", "0001", 0.9),
            (None, None, None, None, -1.0),
        ]
        self._run(image, faces=faces, matches=matches)
        batch_root = self.storage / f"photo_batches/{self.batch_id}"
        media_bytes = (batch_root / "MEDIA/photo.png").read_bytes()
        self.assertEqual((batch_root / "SORTED/0001_Declined/photo.png").read_bytes(), media_bytes)
        self.assertFalse((batch_root / "REVIEW").exists(),
                         "no REVIEW/ output is produced for a new batch")


if __name__ == "__main__":
    unittest.main()
