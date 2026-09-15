"""Phase B — workflow_version is decided ONCE, at batch creation, from the
use_concurrent_pipeline Setting (default OFF). Never changed afterward,
even if the setting flips mid-run for some other batch."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine

import app.services.photo_processing_service as pps
from app.models.models import PhotoBatch


class WorkflowVersionFlagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-workflow-version-test-")
        self.addCleanup(self.temp.cleanup)
        storage = Path(self.temp.name) / "storage"
        photo_batches_dir = storage / "photo_batches"
        db_path = Path(self.temp.name) / "test.db"

        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        for target, value in (("PHOTO_BATCHES_DIR", photo_batches_dir),):
            patch.object(pps, target, value).start()
        self.addCleanup(patch.stopall)

        patch.object(pps, "get_folder_name", return_value="Test Folder").start()

    def test_default_off_creates_workflow_version_1(self):
        with patch.object(pps.settings_cache, "get_use_concurrent_pipeline", return_value=False):
            with Session(self.engine) as session:
                batch = pps.create_batch(session, "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0", 7, "user-1")
        self.assertEqual(batch.workflow_version, 1)

    def test_flag_on_creates_workflow_version_2(self):
        with patch.object(pps.settings_cache, "get_use_concurrent_pipeline", return_value=True):
            with Session(self.engine) as session:
                batch = pps.create_batch(session, "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0", 7, "user-1")
        self.assertEqual(batch.workflow_version, 2)

    def test_existing_batch_keeps_its_version_when_flag_later_flips(self):
        with patch.object(pps.settings_cache, "get_use_concurrent_pipeline", return_value=False):
            with Session(self.engine) as session:
                old_batch = pps.create_batch(session, "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0", 7, "user-1")
        with patch.object(pps.settings_cache, "get_use_concurrent_pipeline", return_value=True):
            with Session(self.engine) as session:
                new_batch = pps.create_batch(session, "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0", 7, "user-1")
        with Session(self.engine) as session:
            refreshed_old = session.get(PhotoBatch, old_batch.id)
        self.assertEqual(refreshed_old.workflow_version, 1)
        self.assertEqual(new_batch.workflow_version, 2)


if __name__ == "__main__":
    unittest.main()
