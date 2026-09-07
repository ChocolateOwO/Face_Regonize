"""Isolated DB/files only. Execute real API handler and Phase 2 with fake Drive.

AST extraction avoids importing app.config/main (production directories/DB,
retention jobs and GPU initialization). No real OAuth or Drive calls possible.
"""
import ast
import asyncio
import io
import logging
import mimetypes
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlmodel import Session, SQLModel, create_engine, select
from app.models.models import Person, PhotoBatch, PhotoBatchFace, PhotoBatchParticipantFolder, PhotoBatchPhoto, User
from app.services import photo_batch_download_service as download
from test_event_photo_explicit_deny import MediaPolicyTests

BACKEND = Path(__file__).resolve().parents[1]


def load_functions(relative, names, namespace):
    path = BACKEND / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-download-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'test.db'}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        self.batch = PhotoBatch(id="a" * 32, label="isolated", drive_folder_id="fake", storage_dir="photo_batches/" + "a" * 32,
                                status="syncing_drive", total_photos=2, processed_photos=2)
        self.root = self.storage / self.batch.storage_dir
        for name in ("SORTED/person", "REVIEW", "MEDIA", "ORIGINAL", "LOGO", "AMBIENCE"):
            (self.root / name).mkdir(parents=True)
        import cv2
        policy = MediaPolicyTests()
        _, media, _ = policy.render([policy.row("deny", "declined")], logo=True)
        self.media = cv2.imencode(".png", media)[1].tobytes()
        self.photos = []
        for i in range(2):
            filename = f"image{i}.png"
            for folder in ("SORTED/person", "REVIEW", "MEDIA", "ORIGINAL"):
                (self.root / folder / filename).write_bytes(self.media)
            self.photos.append(PhotoBatchPhoto(id=str(i), batch_id=self.batch.id, filename=filename,
                drive_file_id="fake", original_path=f"{self.batch.storage_dir}/ORIGINAL/{filename}",
                media_path=f"{self.batch.storage_dir}/MEDIA/{filename}", classification="review", faces_matched=1))
        (self.root / "LOGO/logo.png").write_bytes(b"private logo")
        for name in (".env", "app.db", "credentials.json", "extra.jpg", "temp.png"):
            (self.root / "MEDIA" / name).write_bytes(b"excluded")
        with Session(self.engine) as session:
            session.add(self.batch)
            session.add_all(self.photos)
            session.commit()
            session.refresh(self.batch)
            for photo in self.photos:
                session.refresh(photo)
        self.cancelled = set()

        def get_session():
            with Session(self.engine) as session:
                yield session

        def get_user():
            return User(username="isolated", password_hash="unused")

        router = APIRouter()
        namespace = dict(router=router, Session=Session, Depends=Depends, get_session=get_session,
            get_current_user=get_user, User=User, PhotoBatch=PhotoBatch, PhotoBatchPhoto=PhotoBatchPhoto,
            HTTPException=HTTPException, select=select, STORAGE_PATH=self.storage,
            _cancelled=lambda identity: identity in self.cancelled,
            output_manifest=download.output_manifest, build_output_zip=download.build_output_zip,
            TemporaryZipResponse=download.TemporaryZipResponse)
        load_functions("app/api/photo_batches.py", {"download_photo_batch"}, namespace)
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/photo-batches")

    def request(self, identity=None):
        async def run():
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            path = f"/api/photo-batches/{identity or self.batch.id}/download"
            await self.app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
                "query_string": b"", "root_path": "", "headers": [], "http_version": "1.1",
                "client": ("127.0.0.1", 1234), "server": ("isolated", 80)}, receive, send)
            start = next(m for m in messages if m["type"] == "http.response.start")
            return SimpleNamespace(status_code=start["status"], headers={k.decode(): v.decode() for k, v in start["headers"]},
                content=b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
        return asyncio.run(run())

    def status(self, status, **values):
        with Session(self.engine) as session:
            batch = session.get(PhotoBatch, self.batch.id)
            batch.status = status
            for key, value in values.items():
                setattr(batch, key, value)
            session.add(batch)
            session.commit()

    def test_readiness_states(self):
        for state, ready in [("pending", False), ("processing", False), ("syncing_drive", True),
                             ("completed", True), ("cancelled", False), ("stopping", False), ("deleting", False)]:
            with self.subTest(state=state):
                self.status(state)
                self.assertEqual(self.request().status_code, 200 if ready else 409)
        self.status("failed", drive_error="Drive upload failed")
        self.assertEqual(self.request().status_code, 200)
        self.status("failed", drive_error=None)
        self.assertEqual(self.request().status_code, 409)

    def test_counters_not_enough_and_cancelled_worker(self):
        self.status("syncing_drive", processed_photos=1)
        self.assertEqual(self.request().status_code, 409)
        self.status("syncing_drive", processed_photos=2)
        self.cancelled.add(self.batch.id)
        self.assertEqual(self.request().status_code, 409)

    def test_zip_exact_local_bytes_and_no_artifact(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        archive_dir = Path(self.temp.name) / "zip-temp"
        archive_dir.mkdir()
        with patch.object(tempfile, "tempdir", str(archive_dir)):
            response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(set(archive.namelist()), {"SORTED/", "REVIEW/", "MEDIA/"} |
                {f"{folder}/image{i}.png" for folder in ("SORTED/person", "REVIEW", "MEDIA") for i in range(2)})
            for i in range(2):
                self.assertEqual(archive.read(f"MEDIA/image{i}.png"), self.media)
        self.assertFalse(list(archive_dir.iterdir()))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_no_review_if_not_present(self):
        import shutil
        shutil.rmtree(self.root / "REVIEW")
        with Session(self.engine) as session:
            for photo in session.exec(select(PhotoBatchPhoto)).all():
                photo.classification = "sorted"
                session.add(photo)
            session.commit()
        response = self.request()
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertNotIn("REVIEW/", archive.namelist())

    def test_missing_and_deleted_batch(self):
        self.assertEqual(self.request("b" * 32).status_code, 404)
        with Session(self.engine) as session:
            session.delete(session.get(PhotoBatch, self.batch.id))
            session.commit()
        self.assertEqual(self.request().status_code, 404)

    def test_missing_media_sorted_review_or_record(self):
        for relative in ("MEDIA/image0.png", "SORTED/person/image0.png", "REVIEW/image0.png"):
            path = self.root / relative
            value = path.read_bytes()
            path.unlink()
            self.assertEqual(self.request().status_code, 409)
            path.write_bytes(value)
        with Session(self.engine) as session:
            photo = session.get(PhotoBatchPhoto, "0")
            photo.media_path = None
            session.add(photo)
            session.commit()
        self.assertEqual(self.request().status_code, 409)

    def test_cross_batch_paths_rejected(self):
        self.status("completed", storage_dir="photo_batches/" + "b" * 32)
        self.assertEqual(self.request().status_code, 409)
        self.status("completed", storage_dir=self.batch.storage_dir)
        with Session(self.engine) as session:
            photo = session.get(PhotoBatchPhoto, "0")
            photo.media_path = "photo_batches/" + "b" * 32 + "/MEDIA/image0.png"
            session.add(photo)
            session.commit()
        self.assertEqual(self.request().status_code, 409)

    def test_linked_file_rejected(self):
        import os
        path = self.root / "MEDIA/image0.png"
        other = Path(self.temp.name) / "outside.png"
        other.write_bytes(path.read_bytes())
        path.unlink()
        os.link(other, path)
        self.assertEqual(self.request().status_code, 409)

    def test_temp_cleanup_on_read_failure_and_disconnect(self):
        root, files = download.output_manifest(self.batch, self.photos, self.storage)
        archive_dir = Path(self.temp.name) / "zip-temp"
        archive_dir.mkdir()
        with patch.object(tempfile, "tempdir", str(archive_dir)):
            with patch.object(download.shutil, "copyfileobj", side_effect=OSError("read failed")):
                with self.assertRaises(OSError):
                    download.build_output_zip(root, files)
            self.assertFalse(list(archive_dir.iterdir()))
            path = download.build_output_zip(root, files)
            async def send(_message):
                raise OSError("client disconnected")
            async def receive():
                return {"type": "http.disconnect"}
            with self.assertRaises(OSError):
                asyncio.run(download.TemporaryZipResponse(path)({"type": "http", "method": "GET", "headers": []}, receive, send))
            self.assertFalse(path.exists())

    def test_real_phase_two_continues_across_download(self):
        # Real Phase 2 and upload helper, isolated DB, network replaced entirely.
        from datetime import datetime
        started, release = threading.Event(), threading.Event()
        uploaded = []
        errors = []
        def upload(_folder, filename, data, _mime):
            uploaded.append((filename, data))
            if len(uploaded) == 1:
                started.set()
                if not release.wait(15):
                    raise RuntimeError("test synchronization timeout")
            return "fake-file"
        namespace = dict(Session=Session, engine=self.engine, select=select, PhotoBatch=PhotoBatch,
            PhotoBatchPhoto=PhotoBatchPhoto, PhotoBatchFace=PhotoBatchFace,
            PhotoBatchParticipantFolder=PhotoBatchParticipantFolder, Person=Person,
            STORAGE_PATH=self.storage, datetime=datetime, mimetypes=mimetypes,
            logger=logging.getLogger("isolated-drive"), DriveOAuthError=type("DriveOAuthError", (Exception,), {}),
            _cancelled=lambda _: False, create_root_folder=lambda _: "root",
            get_or_create_subfolder=lambda _parent, name: name, upload_bytes=upload,
            copy_file=lambda *_: "copy", _safe_folder_name=lambda *_: "person")
        load_functions("app/services/photo_processing_service.py", {"_sync_batch_to_drive", "_drive_upload_photo"}, namespace)
        def worker():
            try:
                namespace["_sync_batch_to_drive"](self.batch.id)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            self.assertEqual(self.request().status_code, 200)
            self.assertTrue(thread.is_alive())
            with Session(self.engine) as session:
                self.assertEqual(session.get(PhotoBatch, self.batch.id).status, "syncing_drive")
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(any(filename == "image1.png" for filename, _ in uploaded))
        self.assertTrue(all(data == self.media for _, data in uploaded))
        with Session(self.engine) as session:
            self.assertEqual(session.get(PhotoBatch, self.batch.id).status, "completed")
            self.assertTrue(all(p.drive_upload_status == "uploaded" for p in session.exec(select(PhotoBatchPhoto)).all()))


if __name__ == "__main__":
    unittest.main()
