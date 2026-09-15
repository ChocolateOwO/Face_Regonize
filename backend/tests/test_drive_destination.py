"""Manual Google Drive destination feature — isolated DB/files, fake Drive
throughout. AST extraction avoids importing app.config/app.database.db
(production directories/DB) or app.face_recognition (GPU/model init), the
same technique test_photo_batch_download.py uses. No real OAuth or network
call is possible from this file.
"""
import ast
import asyncio
import json
import logging
import mimetypes
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlmodel import Session, SQLModel, create_engine
from app.models.models import PhotoBatch, User
from app.services import photo_batch_download_service as download

BACKEND = Path(__file__).resolve().parents[1]


def load_from_source(relative, names, namespace):
    """Executes only the named top-level defs/classes/assignments from a
    source file into `namespace`, without importing the module (and whatever
    heavy app.* dependencies its real imports would pull in)."""
    path = BACKEND / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [
        n for n in tree.body
        if getattr(n, "name", None) in names
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets))
        or (isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) in names)
    ]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


class ParserTests(unittest.TestCase):
    """Strict destination-URL parser — never treats input as a filesystem path."""

    def setUp(self):
        self.ns = dict(re=__import__("re"), urlparse=urlparse)
        load_from_source(
            "app/services/drive_destination_service.py",
            {"DriveDestinationError", "_FOLDER_PATH_RE", "_RAW_ID_RE", "_ALLOWED_HOSTS", "parse_destination_folder_url"},
            self.ns,
        )
        self.parse = self.ns["parse_destination_folder_url"]
        self.Error = self.ns["DriveDestinationError"]

    def test_valid_full_url(self):
        self.assertEqual(
            self.parse("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0"),
            "1AbCdEfGhIjKlMnOpQrStUvWxYz0",
        )

    def test_valid_with_account_index_and_query_string(self):
        self.assertEqual(
            self.parse("https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0?usp=sharing"),
            "1AbCdEfGhIjKlMnOpQrStUvWxYz0",
        )

    def test_raw_id_accepted(self):
        self.assertEqual(self.parse("1AbCdEfGhIjKlMnOpQrStUvWxYz0"), "1AbCdEfGhIjKlMnOpQrStUvWxYz0")

    def test_empty_rejected(self):
        with self.assertRaises(self.Error):
            self.parse("")

    def test_malformed_url_rejected(self):
        with self.assertRaises(self.Error):
            self.parse("https://[not-valid")

    def test_no_host_rejected(self):
        with self.assertRaises(self.Error):
            self.parse("https:///drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0")

    def test_non_drive_host_rejected(self):
        with self.assertRaises(self.Error):
            self.parse("https://evil.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0")

    def test_lookalike_host_rejected(self):
        with self.assertRaises(self.Error):
            self.parse("https://drive.google.com.evil.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0")

    def test_userinfo_host_confusion_rejected(self):
        # "drive.google.com" here is USERINFO, not the host — must resolve to evil.com and be rejected.
        with self.assertRaises(self.Error):
            self.parse("https://drive.google.com@evil.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0")

    def test_file_link_rejected(self):
        with self.assertRaises(self.Error):
            self.parse("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0/view")

    def test_missing_folder_id_rejected(self):
        with self.assertRaises(self.Error):
            self.parse("https://drive.google.com/drive/folders/")

    def test_local_paths_never_accepted(self):
        for value in (r"C:\Users\foo\Documents", "/etc/passwd", "../../secret", "relative/path/here"):
            with self.subTest(value=value):
                with self.assertRaises(self.Error):
                    self.parse(value)

    def test_random_text_rejected(self):
        with self.assertRaises(self.Error):
            self.parse("not a url at all")


class ValidateDestinationTests(unittest.TestCase):
    """validate_destination() — uses only the EXISTING write client, never
    expands scope, never leaks credentials."""

    def setUp(self):
        self.ns = dict(DriveOAuthError=type("DriveOAuthError", (Exception,), {}), re=__import__("re"))
        # Follow-up Task 1 — validation now explains Drive errors specifically.
        load_from_source("app/services/drive_destination_service.py",
                         {"DriveDestinationError", "PickerGrantRequired", "validate_destination", "NOT_GRANTED_MESSAGE", "EXPIRED_MESSAGE", "NOT_CONNECTED_MESSAGE", "VIEW_ONLY_MESSAGE", "_SECRET_RE", "_redact", "_http_status", "explain_drive_error",
                          "_RAW_ID_RE", "_FOLDER_MIME", "_SHORTCUT_MIME", "_META_FIELDS", "_drive_folder_url", "_get_meta"}, self.ns)
        self.validate = self.ns["validate_destination"]
        self.Error = self.ns["DriveDestinationError"]

    def _install_service(self, meta=None, get_error=None, write_service_error=None):
        class FakeGet:
            def __init__(self, meta, error):
                self._meta, self._error = meta, error

            def execute(self):
                if self._error:
                    raise self._error
                return self._meta

        class FakeFiles:
            def __init__(self, meta, error):
                self._meta, self._error = meta, error

            def get(self, fileId, fields, **kwargs):  # noqa: A002 - matches google-api-python-client's kwarg name
                return FakeGet(self._meta, self._error)

        class FakeService:
            def __init__(self, meta, error):
                self._meta, self._error = meta, error

            def files(self):
                return FakeFiles(self._meta, self._error)

        def fake_get_write_service():
            if write_service_error:
                raise write_service_error
            return FakeService(meta, get_error)

        self.ns["get_write_service"] = fake_get_write_service

    def test_valid_folder(self):
        self._install_service(meta={
            "id": "F1", "name": "Client Delivery", "mimeType": "application/vnd.google-apps.folder",
            "trashed": False, "capabilities": {"canAddChildren": True},
        })
        self.assertEqual(self.validate("F1"), {"folder_id": "F1", "folder_name": "Client Delivery", "can_upload": True,
                                               "folder_url": "https://drive.google.com/drive/folders/F1"})

    def test_inaccessible_folder_returns_clear_error(self):
        self._install_service(get_error=Exception("404 File not found: F1"))
        with self.assertRaises(self.Error):
            self.validate("F1")

    def test_trashed_folder_rejected(self):
        self._install_service(meta={"mimeType": "application/vnd.google-apps.folder", "trashed": True, "capabilities": {}})
        with self.assertRaises(self.Error):
            self.validate("F1")

    def test_file_not_folder_rejected(self):
        self._install_service(meta={"mimeType": "application/pdf", "trashed": False, "capabilities": {}})
        with self.assertRaises(self.Error):
            self.validate("F1")

    def test_no_upload_permission_rejected(self):
        self._install_service(meta={
            "mimeType": "application/vnd.google-apps.folder", "trashed": False, "capabilities": {"canAddChildren": False},
        })
        with self.assertRaises(self.Error):
            self.validate("F1")

    def test_not_connected_surfaces_clear_message(self):
        self._install_service(write_service_error=self.ns["DriveOAuthError"]("Google Drive is not connected"))
        with self.assertRaises(self.Error):
            self.validate("F1")

    def test_never_returns_credentials_or_extra_fields(self):
        self._install_service(meta={
            "id": "F1", "name": "X", "mimeType": "application/vnd.google-apps.folder", "trashed": False,
            "capabilities": {"canAddChildren": True}, "refreshToken": "should-never-leak",
        })
        result = self.validate("F1")
        self.assertEqual(set(result), {"folder_id", "folder_name", "can_upload", "folder_url"})


class UploadFlowTests(unittest.TestCase):
    """The explicit upload itself: only local SORTED+MEDIA, never REVIEW/
    ORIGINAL/LOGO, no reprocessing, safe on failure, safe to retry."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-drive-dest-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'test.db'}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        self.batch = PhotoBatch(
            id="a" * 32, label="Test Event", drive_folder_id="fake",
            storage_dir="photo_batches/" + "a" * 32, status="ready", total_photos=2, processed_photos=2,
        )
        self.root = self.storage / self.batch.storage_dir
        for name in ("SORTED/0001_Jane_Doe", "REVIEW", "MEDIA", "ORIGINAL", "LOGO"):
            (self.root / name).mkdir(parents=True)
        (self.root / "SORTED/0001_Jane_Doe/photo1.jpg").write_bytes(b"person-copy-1")
        (self.root / "MEDIA/photo1.jpg").write_bytes(b"media-1")
        (self.root / "MEDIA/photo2.jpg").write_bytes(b"media-2")
        (self.root / "REVIEW/photo2.jpg").write_bytes(b"review-should-never-upload")
        (self.root / "ORIGINAL/photo1.jpg").write_bytes(b"original-should-never-upload")
        (self.root / "ORIGINAL/photo2.jpg").write_bytes(b"original-should-never-upload")
        (self.root / "LOGO/logo.png").write_bytes(b"logo-should-never-upload")
        with Session(self.engine) as session:
            session.add(self.batch)
            session.commit()
            session.refresh(self.batch)

        from tests.test_drive_upload_flows import FakeDrive, folder_meta

        self.dest = "1AbCdEfGhIjKlMnOpQrStUvWxYz0"
        self.drive = FakeDrive({self.dest: folder_meta(self.dest, "Dest")})
        self.uploaded: list[tuple[str, str, bytes]] = []
        self.fail_on_upload_number = None

        def fake_upload_file(service, parent_id, name, data, mime_type, props):
            if self.fail_on_upload_number and len(self.uploaded) + 1 == self.fail_on_upload_number:
                raise RuntimeError("simulated Drive failure")
            self.uploaded.append((parent_id, name, data))
            return self.drive.add_item(name, parent_id, props)

        self.ns = dict(
            Session=Session, engine=self.engine, PhotoBatch=PhotoBatch, STORAGE_PATH=self.storage,
            mimetypes=mimetypes, threading=threading, datetime=datetime, Path=Path, dataclass=dataclass,
            re=__import__("re"), urlparse=urlparse, logging=logging, logger=logging.getLogger("isolated-drive-destination"),
            DriveOAuthError=type("DriveOAuthError", (Exception,), {}),
            hashlib=__import__("hashlib"), io=__import__("io"), json=__import__("json"),
            get_write_service=lambda: self.drive,
            _safe_path=download._safe_path,
            batch_output_root=download.batch_output_root,
            local_output_ready=download.local_output_ready,
        )
        load_from_source(
            "app/services/drive_destination_service.py",
            {
                "DriveDestinationError", "PickerGrantRequired", "_FOLDER_PATH_RE", "_RAW_ID_RE", "_ALLOWED_HOSTS",
                "parse_destination_folder_url", "_UploadFile", "_iter_upload_files",
                "_upload_lock", "_uploading_batches", "reserve_upload", "_release_upload",
                "_fail", "_run_upload", "run_drive_upload",
                # Follow-up Task 1 — upload failures are explained specifically.
                "NOT_GRANTED_MESSAGE", "EXPIRED_MESSAGE", "NOT_CONNECTED_MESSAGE", "VIEW_ONLY_MESSAGE", "_SECRET_RE", "_redact", "_http_status", "explain_drive_error",
                # Revalidation before every upload.
                "validate_destination", "_FOLDER_MIME", "_SHORTCUT_MIME", "_META_FIELDS", "_drive_folder_url", "_get_meta",
                # Idempotent upload/retry: stable key, Drive lookup, local manifest.
                "_UPLOAD_KEY_PROP", "_BATCH_PROP", "_STATE_DIRNAME", "upload_key", "_state_path", "_load_state",
                "_save_state", "_escape_query_value", "_find_by_key", "_still_in_place", "_ensure_subfolder",
            },
            self.ns,
        )
        self.ns["_upload_file"] = fake_upload_file

    def current(self):
        with Session(self.engine) as session:
            return session.get(PhotoBatch, self.batch.id)

    def test_module_never_imports_the_processing_pipeline(self):
        # Structural guarantee, not a mock: this module cannot call detection,
        # recognition, blur or logo compositing because it never imports them.
        source = (BACKEND / "app/services/drive_destination_service.py").read_text(encoding="utf-8")
        for forbidden in ("detect_faces", "recognition_index", "_blur_region", "_apply_logo", "event_photo_detection_service", "face_recognition"):
            self.assertNotIn(forbidden, source)

    def test_full_upload_excludes_review_original_logo(self):
        self.ns["run_drive_upload"](self.batch.id, "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0", True, True)
        self.assertEqual(len(self.uploaded), 3)
        by_name_data = {(name, data) for (_parent, name, data) in self.uploaded}
        self.assertIn(("photo1.jpg", b"person-copy-1"), by_name_data)
        self.assertIn(("photo1.jpg", b"media-1"), by_name_data)
        self.assertIn(("photo2.jpg", b"media-2"), by_name_data)
        uploaded_bytes = {data for (_p, _n, data) in self.uploaded}
        self.assertNotIn(b"review-should-never-upload", uploaded_bytes)
        self.assertNotIn(b"original-should-never-upload", uploaded_bytes)
        self.assertNotIn(b"logo-should-never-upload", uploaded_bytes)
        batch = self.current()
        self.assertEqual(batch.status, "completed")
        self.assertEqual(batch.processed_folder_id, "1AbCdEfGhIjKlMnOpQrStUvWxYz0")

    def test_media_only(self):
        self.ns["run_drive_upload"](self.batch.id, "1AbCdEfGhIjKlMnOpQrStUvWxYz0", False, True)
        self.assertEqual({n for _p, n, _d in self.uploaded}, {"photo1.jpg", "photo2.jpg"})
        self.assertEqual({d for _p, _n, d in self.uploaded}, {b"media-1", b"media-2"})

    def test_people_only(self):
        self.ns["run_drive_upload"](self.batch.id, "1AbCdEfGhIjKlMnOpQrStUvWxYz0", True, False)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(self.uploaded[0][1:], ("photo1.jpg", b"person-copy-1"))

    def test_destination_folder_becomes_parent(self):
        self.ns["run_drive_upload"](self.batch.id, "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0", True, True)
        # Directly inside the selected folder: no extra top-level folder.
        folders = [i for i in self.drive.items.values() if i["mimeType"] == "application/vnd.google-apps.folder"]
        self.assertEqual({p for f in folders for p in f["parents"]}, {self.dest})
        self.assertEqual({f["name"] for f in folders}, {"MEDIA", "0001_Jane_Doe"})

    def test_not_ready_batch_rejected_before_any_upload(self):
        with Session(self.engine) as session:
            batch = session.get(PhotoBatch, self.batch.id)
            batch.status = "processing"
            session.add(batch)
            session.commit()
        with self.assertRaises(self.ns["DriveDestinationError"]):
            self.ns["_run_upload"](self.batch.id, "1AbCdEfGhIjKlMnOpQrStUvWxYz0", True, True)
        self.assertEqual(self.uploaded, [])

    def test_nothing_selected_raises(self):
        with self.assertRaises(self.ns["DriveDestinationError"]):
            self.ns["_run_upload"](self.batch.id, "1AbCdEfGhIjKlMnOpQrStUvWxYz0", False, False)

    def test_failure_preserves_local_files_and_marks_upload_failed(self):
        outputs = lambda: {p: p.read_bytes() for p in self.root.rglob("*")  # noqa: E731
                           if p.is_file() and "DRIVE_UPLOAD" not in p.parts}
        before = outputs()
        self.fail_on_upload_number = 2
        self.ns["run_drive_upload"](self.batch.id, "1AbCdEfGhIjKlMnOpQrStUvWxYz0", True, True)
        batch = self.current()
        self.assertEqual(batch.status, "upload_failed")
        self.assertIn("simulated Drive failure", batch.drive_error)
        self.assertEqual(before, outputs())
        self.assertTrue(download.local_output_ready(batch, self.storage))  # download still works

    def test_retry_after_failure_restarts_from_same_local_output(self):
        self.fail_on_upload_number = 2
        self.ns["run_drive_upload"](self.batch.id, "1AbCdEfGhIjKlMnOpQrStUvWxYz0", True, True)
        self.assertEqual(self.current().status, "upload_failed")
        self.fail_on_upload_number = None
        self.uploaded.clear()
        self.ns["run_drive_upload"](self.batch.id, "1AbCdEfGhIjKlMnOpQrStUvWxYz0", True, True)
        self.assertEqual(self.current().status, "completed")
        # Only what never landed is uploaded again: the file that already
        # reached Drive before the failure is reconciled, not copied twice.
        self.assertEqual(len(self.uploaded), 2)

    def test_duplicate_concurrent_upload_rejected(self):
        reserve, release = self.ns["reserve_upload"], self.ns["_release_upload"]
        self.assertTrue(reserve(self.batch.id))
        self.assertFalse(reserve(self.batch.id))
        release(self.batch.id)
        self.assertTrue(reserve(self.batch.id))
        release(self.batch.id)

    def test_download_ready_across_all_drive_states(self):
        for status in ("ready", "uploading", "upload_failed", "completed"):
            with self.subTest(status=status):
                with Session(self.engine) as session:
                    batch = session.get(PhotoBatch, self.batch.id)
                    batch.status = status
                    session.add(batch)
                    session.commit()
                    session.refresh(batch)
                self.assertTrue(download.local_output_ready(batch, self.storage))


class ApiEndpointTests(unittest.TestCase):
    """The two new endpoints, wired exactly like the production router."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-drive-dest-api-test-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'test.db'}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

        self.batch = PhotoBatch(
            id="b" * 32, label="API Test", drive_folder_id="fake",
            storage_dir="photo_batches/" + "b" * 32, status="ready", total_photos=1, processed_photos=1,
        )
        self.root = self.storage / self.batch.storage_dir
        for name in ("SORTED", "MEDIA"):
            (self.root / name).mkdir(parents=True)
        (self.root / "MEDIA/photo1.jpg").write_bytes(b"media-1")
        with Session(self.engine) as session:
            session.add(self.batch)
            session.commit()
            session.refresh(self.batch)

        self.validate_calls: list[str] = []
        self.upload_calls: list[tuple] = []
        self.reserved: set[str] = set()

        from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException
        from pydantic import BaseModel

        class DriveDestinationValidateBody(BaseModel):
            folder_url: str

        class DriveUploadBody(BaseModel):
            folder_url: str
            upload_people: bool = True
            upload_media: bool = True

        DriveDestinationError = type("DriveDestinationError", (Exception,), {})

        def fake_parse(url):
            if url == "bad-url":
                raise DriveDestinationError("bad url")
            return url  # pass through — the real parser has its own dedicated tests

        def fake_validate(folder_id):
            self.validate_calls.append(folder_id)
            if folder_id == "INACCESSIBLE":
                raise DriveDestinationError("not reachable")
            return {"folder_id": folder_id, "folder_name": "Dest", "can_upload": True}

        def fake_reserve(batch_id):
            if batch_id in self.reserved:
                return False
            self.reserved.add(batch_id)
            return True

        def fake_run_drive_upload(batch_id, folder_url, upload_people, upload_media, *_selection):
            self.upload_calls.append((batch_id, folder_url, upload_people, upload_media))

        def get_session():
            with Session(self.engine) as session:
                yield session

        def get_user():
            return User(username="isolated", password_hash="unused")

        router = APIRouter()
        namespace = dict(
            router=router, Session=Session, Depends=Depends, get_session=get_session,
            get_current_user=get_user, User=User, PhotoBatch=PhotoBatch,
            HTTPException=HTTPException, BackgroundTasks=BackgroundTasks,
            STORAGE_PATH=self.storage, _cancelled=lambda _id: False,
            local_output_ready=download.local_output_ready,
            DriveDestinationError=DriveDestinationError,
            # A never-granted pasted folder is answered with the Picker-grant
            # state instead of an error, so the route needs both names.
            PickerGrantRequired=type("PickerGrantRequired", (DriveDestinationError,), {}),
            PICKER_GRANT_PROMPT="Google requires one confirmation before Reconize can upload to this folder.",
            parse_destination_folder_url=fake_parse,
            # Phase H — the routes resolve a URL or a Picker folder id first.
            resolve_folder_ref=lambda url, fid=None: fid if fid else fake_parse(url or ""),
            validate_destination=fake_validate,
            reserve_upload=fake_reserve,
            # Phase D1 — the route reserves through the edit/activity lock;
            # the fake keeps this test's reservation semantics.
            batch_edit_lock=SimpleNamespace(try_reserve_upload=fake_reserve),
            run_drive_upload=fake_run_drive_upload,
            # Phase O — the shared ExportSelection (real module).
            export_selection_service=__import__('app.services.export_selection_service',
                                                fromlist=['sorted_folder_names']),
            DriveDestinationValidateBody=DriveDestinationValidateBody,
            DriveUploadBody=DriveUploadBody,
        )
        load_from_source("app/api/photo_batches.py", {"validate_drive_destination", "start_drive_upload"}, namespace)
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/photo-batches")

    def request(self, method, path, body=None):
        async def run():
            messages = []
            content = json.dumps(body).encode() if body is not None else b""
            sent = False

            async def receive():
                nonlocal sent
                if sent:
                    return {"type": "http.request", "body": b"", "more_body": False}
                sent = True
                return {"type": "http.request", "body": content, "more_body": False}

            async def send(message):
                messages.append(message)

            headers = [(b"content-type", b"application/json")] if body is not None else []
            await self.app({
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                "query_string": b"", "root_path": "", "headers": headers, "http_version": "1.1",
                "client": ("127.0.0.1", 1234), "server": ("isolated", 80),
            }, receive, send)
            start = next(m for m in messages if m["type"] == "http.response.start")
            body_bytes = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return SimpleNamespace(
                status_code=start["status"],
                json=json.loads(body_bytes) if body_bytes else None,
            )

        return asyncio.run(run())

    def test_validate_rejects_malformed_url(self):
        response = self.request(
            "POST", f"/api/photo-batches/{self.batch.id}/drive-destination/validate", {"folder_url": "bad-url"}
        )
        self.assertEqual(response.status_code, 400)

    def test_validate_success_returns_safe_fields_only(self):
        response = self.request(
            "POST", f"/api/photo-batches/{self.batch.id}/drive-destination/validate",
            {"folder_url": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json), {"folder_id", "folder_name", "can_upload"})

    def test_validate_missing_batch_404(self):
        response = self.request(
            "POST", "/api/photo-batches/" + "z" * 32 + "/drive-destination/validate",
            {"folder_url": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0"},
        )
        self.assertEqual(response.status_code, 404)

    def test_upload_requires_local_output_ready(self):
        with Session(self.engine) as session:
            batch = session.get(PhotoBatch, self.batch.id)
            batch.status = "processing"
            session.add(batch)
            session.commit()
        response = self.request(
            "POST", f"/api/photo-batches/{self.batch.id}/drive-upload",
            {"folder_url": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.upload_calls, [])

    def test_upload_revalidates_destination_server_side(self):
        response = self.request("POST", f"/api/photo-batches/{self.batch.id}/drive-upload", {"folder_url": "INACCESSIBLE"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("INACCESSIBLE", self.validate_calls)
        self.assertEqual(self.upload_calls, [])

    def test_upload_requires_at_least_one_content_type(self):
        response = self.request(
            "POST", f"/api/photo-batches/{self.batch.id}/drive-upload",
            {
                "folder_url": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0",
                "upload_people": False, "upload_media": False,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.upload_calls, [])

    def test_upload_success_schedules_background_task(self):
        response = self.request(
            "POST", f"/api/photo-batches/{self.batch.id}/drive-upload",
            {"folder_url": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "uploading"})
        self.assertEqual(len(self.upload_calls), 1)

    def test_duplicate_concurrent_upload_rejected_at_api(self):
        self.reserved.add(self.batch.id)
        response = self.request(
            "POST", f"/api/photo-batches/{self.batch.id}/drive-upload",
            {"folder_url": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.upload_calls, [])


class AutomaticSyncRemovedTests(unittest.TestCase):
    """The exact regression this task targets: local completion must reach
    status="ready" and stop — no automatic Drive network call. Static AST
    inspection rather than running the real pipeline, which needs the GPU/
    model stack (see test_event_photo_tiling.py for that coverage)."""

    def setUp(self):
        source = (BACKEND / "app/services/photo_processing_service.py").read_text(encoding="utf-8")
        self.source = source
        self.tree = ast.parse(source)
        self.run_batch = next(n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_photo_batch")

    def test_never_calls_sync_or_finish_automatically(self):
        calls = {
            node.func.id for node in ast.walk(self.run_batch)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("_sync_batch_to_drive", calls)
        self.assertNotIn("_finish_batch", calls)

    def test_sets_ready_status_on_completion(self):
        # Phase B extracted the phase-end status decision into
        # _finalize_batch_status(), shared verbatim by the sequential path
        # AND the pipeline path — so the "ready" assignment itself now
        # lives there, not inline in _run_photo_batch. Confirm both halves:
        # _run_photo_batch actually calls it, and that function actually
        # sets status = "ready".
        calls = {
            node.func.id for node in ast.walk(self.run_batch)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("_finalize_batch_status", calls)

        finalize_fn = next(
            n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef) and n.name == "_finalize_batch_status"
        )
        assigns_ready = any(
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets)
            and isinstance(node.value, ast.Constant) and node.value.value == "ready"
            for node in ast.walk(finalize_fn)
        )
        self.assertTrue(assigns_ready, '_finalize_batch_status must set batch.status = "ready" after local processing')

    def test_legacy_sync_function_still_defined(self):
        # Kept (unused by the automatic flow) only because
        # test_photo_batch_download.py exercises it directly.
        self.assertIn("def _sync_batch_to_drive(", self.source)


if __name__ == "__main__":
    unittest.main()
