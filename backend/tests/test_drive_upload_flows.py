"""Google Drive destination: Picker-first, upload into an existing folder, and
a Retry that never duplicates anything.

drive.file only exposes folders the user selected with the Google Picker, so a
folder never selected returns 404 and the message points to the Picker (never to
reconnecting). Any selected folder the account can add files to is valid —
owned, another user's with edit permission, or a Shared Drive folder. Uploads go
directly into that folder (no extra top-level folder), the folder is revalidated
right before every upload, each file carries a stable appProperties key so a
retry reconciles instead of re-uploading, and ORIGINAL, THUMBNAILS and REVIEW are
never uploaded. No real Google call is made.
"""
import asyncio
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from fastapi import FastAPI
from googleapiclient.errors import HttpError
from sqlmodel import Session, SQLModel, create_engine

from app.api import photo_batches as pb
from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import Person, PhotoBatch, PhotoBatchPhoto, User
from app.services import drive_destination_service as dds
from app.services.google_drive_oauth_service import DriveOAuthError
from app.services.photo_processing_service import _safe_folder_name

BID = "d" * 32
GRANTED = "1WbOg1FSM0j6FIWuLY4aLrrPGVOaM3eX6"
FOLDER = "application/vnd.google-apps.folder"
PICKER_MESSAGE = "Reconize cannot access this folder yet. Select it with Google Picker first."


def http_error(status: int, reason: str = "", body: dict | None = None) -> HttpError:
    content = json.dumps(body or {"error": {"code": status, "message": reason or "error"}}).encode()
    return HttpError(SimpleNamespace(status=status, reason=reason or "error"), content)


def folder_meta(fid=GRANTED, name="Dest", can_add=True, **extra):
    return {"id": fid, "name": name, "mimeType": FOLDER, "trashed": False,
            "capabilities": {"canAddChildren": can_add}, **extra}


class _Resp:
    def __init__(self, value):
        self._value = value

    def execute(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


class FakeDrive:
    """A tiny Drive: folders and files with parents and appProperties,
    answering the exact queries drive_destination_service issues."""

    def __init__(self, metas=None, error=None):
        self.metas = dict(metas or {})   # destination metadata for validate_destination
        self.error = error               # raised by files().get, for validation tests
        self.calls = []                  # kwargs of every files().get
        self.items = {}                  # id -> {name, parents, appProperties, mimeType, trashed}
        self.uploads = []                # (parent_id, filename) per real file upload
        self.fail_upload_after = None    # nth upload onwards raises before Drive stores it
        self.lose_answer_after = None    # nth upload onwards: Drive stores it, the answer is lost
        self._n = 0

    # --- helpers for tests ---------------------------------------------------
    def add_item(self, name, parent, props=None, mime="image/png", trashed=False):
        self._n += 1
        item_id = f"drv{self._n}"
        self.items[item_id] = {"id": item_id, "name": name, "parents": [parent], "mimeType": mime,
                               "trashed": trashed, "appProperties": dict(props or {})}
        return item_id

    def children(self, parent_id):
        return sorted(i["name"] for i in self.items.values() if parent_id in i["parents"] and not i["trashed"])

    def folder_named(self, name, parent=GRANTED):
        for item in self.items.values():
            if item["mimeType"] == FOLDER and item["name"] == name and parent in item["parents"]:
                return item["id"]
        return None

    def uploaded_paths(self):
        """Every uploaded file as "<folder name>/<file name>"."""
        names = []
        for parent, filename in self.uploads:
            folder = self.items.get(parent, {}).get("name", parent)
            names.append(f"{folder}/{filename}")
        return sorted(names)

    def live_paths(self):
        """What actually exists in Drive now — duplicates included."""
        paths = []
        for item in self.items.values():
            if item["mimeType"] == FOLDER or item["trashed"]:
                continue
            for parent in item["parents"]:
                paths.append(f"{self.items.get(parent, {}).get('name', parent)}/{item['name']}")
        return sorted(paths)

    # --- google-api-python-client surface ------------------------------------
    def files(self):
        return self

    def get(self, **kw):
        self.calls.append(kw)
        file_id = kw["fileId"]
        if self.error:
            return _Resp(self.error)
        if file_id in self.items:
            return _Resp(self.items[file_id])
        if file_id in self.metas:
            return _Resp(self.metas[file_id])
        return _Resp(http_error(404, "File not found"))

    def list(self, **kw):
        query = kw["q"]
        parent = re.search(r"'([^']+)' in parents", query).group(1)
        key = re.search(r"value='([^']+)'", query)
        name = re.search(r"name = '([^']+)'", query)
        wants_folder = f"mimeType = '{FOLDER}'" in query
        found = []
        for item in self.items.values():
            if item["trashed"] or parent not in item["parents"]:
                continue
            if wants_folder and item["mimeType"] != FOLDER:
                continue
            if key and item["appProperties"].get("reconizeUploadKey") != key.group(1):
                continue
            if name and item["name"] != name.group(1):
                continue
            found.append({"id": item["id"], "name": item["name"]})
        return _Resp({"files": found})

    def create(self, **kw):
        body, media = kw["body"], kw.get("media_body")
        if media is not None:
            self.uploads.append((body["parents"][0], body["name"]))
            if self.fail_upload_after is not None and len(self.uploads) > self.fail_upload_after:
                return _Resp(RuntimeError("simulated Drive failure"))
        item_id = self.add_item(body["name"], body["parents"][0], body.get("appProperties"),
                                mime=body.get("mimeType", "image/png"))
        if media is not None and self.lose_answer_after is not None and len(self.uploads) > self.lose_answer_after:
            # Google stored the file; the caller never heard back.
            return _Resp(TimeoutError("timed out waiting for Google Drive"))
        return _Resp({"id": item_id})

    def update(self, **kw):
        item = self.items[kw["fileId"]]
        item["appProperties"].update(kw["body"].get("appProperties") or {})
        return _Resp({"id": item["id"]})


class ExplainTests(unittest.TestCase):
    def test_a_not_granted_folder_points_to_the_picker_never_to_reconnecting(self):
        msg = dds.explain_drive_error(http_error(404, "File not found"))
        self.assertEqual(msg, PICKER_MESSAGE)
        self.assertNotIn("reconnect", msg.lower())

    def test_the_cause_is_found_through_a_wrapping_error(self):
        try:
            try:
                raise http_error(404)
            except HttpError as inner:
                raise DriveOAuthError("Could not create the Drive output folder 'x': boom") from inner
        except DriveOAuthError as e:
            self.assertEqual(dds.explain_drive_error(e), dds.NOT_GRANTED_MESSAGE)

    def test_oauth_states(self):
        self.assertEqual(dds.explain_drive_error(DriveOAuthError("Google Drive is not connected — click …")),
                         dds.NOT_CONNECTED_MESSAGE)
        self.assertEqual(dds.explain_drive_error(
            DriveOAuthError("Could not refresh the Google Drive connection — reconnect it: invalid_grant")),
            dds.EXPIRED_MESSAGE)
        self.assertEqual(dds.explain_drive_error(http_error(401)), dds.EXPIRED_MESSAGE)

    def test_picker_not_configured_is_passed_through(self):
        msg = dds.explain_drive_error(DriveOAuthError("Google Picker is not configured: GOOGLE_PICKER_API_KEY"))
        self.assertIn("Picker is not configured", msg)

    def test_permission_quota_rate_limit_and_server_errors(self):
        perm = dds.explain_drive_error(http_error(403, "insufficientPermissions"))
        self.assertIn("does not have permission", perm)
        self.assertIn("Google Picker", perm)
        self.assertIn("full", dds.explain_drive_error(http_error(403, "storageQuotaExceeded",
                                                                 {"error": {"message": "The user's Drive storage quota has been exceeded."}})))
        self.assertIn("limiting", dds.explain_drive_error(http_error(403, "userRateLimitExceeded",
                                                                    {"error": {"message": "User rate limit exceeded."}})))
        self.assertIn("temporary problem", dds.explain_drive_error(http_error(503)))
        self.assertIn("internet connection", dds.explain_drive_error(TimeoutError("timed out")))

    def test_no_message_claims_reconnecting_grants_folder_access(self):
        for msg in (dds.NOT_GRANTED_MESSAGE, dds.VIEW_ONLY_MESSAGE):
            self.assertNotIn("reconnect", msg.lower())
            self.assertIn("Google Picker", msg)

    def test_no_token_can_ever_appear_in_a_message(self):
        leaky = RuntimeError("POST https://x?access_token=ya29.SECRET-1&key=AIzaXYZ Bearer ya29.OTHER refresh 1//0gSECRET")
        msg = dds.explain_drive_error(leaky)
        for secret in ("ya29", "SECRET", "AIzaXYZ", "OTHER", "1//0g"):
            self.assertNotIn(secret, msg)


class ValidateTests(unittest.TestCase):
    def validate(self, folder=GRANTED, *, metas=None, error=None, oauth_error=None):
        self.drive = FakeDrive(metas, error)

        def service():
            if oauth_error:
                raise oauth_error
            return self.drive
        with patch.object(dds, "get_write_service", side_effect=service):
            return dds.validate_destination(folder)

    def assertRefused(self, contains, *args, **kw):
        with self.assertRaises(dds.DriveDestinationError) as ctx:
            self.validate(*args, **kw)
        self.assertIn(contains, str(ctx.exception))
        return str(ctx.exception)

    def test_picker_selected_owned_folder(self):
        out = self.validate(metas={GRANTED: folder_meta(name="My delivery")})
        self.assertEqual(out, {"folder_id": GRANTED, "folder_name": "My delivery", "can_upload": True,
                               "folder_url": f"https://drive.google.com/drive/folders/{GRANTED}"})

    def test_another_users_folder_with_edit_permission(self):
        meta = folder_meta(name="Client folder", owners=[{"emailAddress": "someone-else@example.com"}])
        self.assertTrue(self.validate(metas={GRANTED: meta})["can_upload"], "ownership is never required")

    def test_shared_drive_folder_uses_supports_all_drives(self):
        out = self.validate(metas={GRANTED: folder_meta(name="Team", driveId="0AShared")})
        self.assertEqual(out["folder_name"], "Team")
        self.assertTrue(all(c.get("supportsAllDrives") is True for c in self.drive.calls))

    def test_viewer_folder_is_rejected(self):
        msg = self.assertRefused("cannot add files", metas={GRANTED: folder_meta(can_add=False)})
        self.assertIn("Google Picker", msg)

    def test_missing_capability_is_rejected(self):
        meta = folder_meta()
        meta.pop("capabilities")
        self.assertRefused("cannot add files", metas={GRANTED: meta})

    def test_not_found_trashed_and_not_a_folder(self):
        self.assertEqual(self.assertRefused("Select it with Google Picker", metas={}), PICKER_MESSAGE)
        self.assertRefused("trash", metas={GRANTED: {**folder_meta(), "trashed": True}})
        self.assertRefused("not a folder", metas={GRANTED: {**folder_meta(), "mimeType": "image/jpeg"}})

    def test_oauth_disconnected(self):
        self.assertRefused("not connected", oauth_error=DriveOAuthError("Google Drive is not connected — click"))

    def test_a_shortcut_resolves_to_its_validated_target_folder(self):
        target = "1TargetFolder000000000000"
        metas = {GRANTED: {"id": GRANTED, "name": "link", "mimeType": "application/vnd.google-apps.shortcut",
                           "trashed": False, "shortcutDetails": {"targetId": target, "targetMimeType": FOLDER}},
                 target: folder_meta(target, "Real folder")}
        out = self.validate(metas=metas)
        self.assertEqual((out["folder_id"], out["folder_name"]), (target, "Real folder"))

    def test_a_shortcut_to_a_file_or_an_unusable_target_is_rejected(self):
        shortcut = {"id": GRANTED, "mimeType": "application/vnd.google-apps.shortcut", "trashed": False,
                    "shortcutDetails": {"targetId": "1TargetFile0000000000000", "targetMimeType": "image/jpeg"}}
        self.assertRefused("does not point to a folder", metas={GRANTED: shortcut})
        target = "1TargetFolder000000000000"
        to_viewer = {**shortcut, "shortcutDetails": {"targetId": target, "targetMimeType": FOLDER}}
        self.assertRefused("cannot add files", metas={GRANTED: to_viewer, target: folder_meta(target, can_add=False)})

    def test_invalid_folder_links(self):
        for bad in ("", "not a link", "https://example.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0",
                    "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0/view"):
            with self.subTest(bad=bad), self.assertRaises(dds.DriveDestinationError):
                dds.resolve_folder_ref(bad, None)
        with self.assertRaises(dds.DriveDestinationError):
            dds.resolve_folder_ref(None, "../etc/passwd")

    def test_a_folder_never_granted_asks_for_a_picker_grant(self):
        with self.assertRaises(dds.PickerGrantRequired) as ctx:
            self.validate(metas={})
        self.assertEqual(ctx.exception.folder_id, GRANTED, "the pasted id survives, so nothing is retyped")
        self.assertIsInstance(ctx.exception, dds.DriveDestinationError)
        self.assertEqual(str(ctx.exception), PICKER_MESSAGE)

    def test_a_viewer_folder_is_a_permission_problem_not_a_grant_problem(self):
        with self.assertRaises(dds.DriveDestinationError) as ctx:
            self.validate(metas={GRANTED: folder_meta(can_add=False)})
        self.assertNotIsInstance(ctx.exception, dds.PickerGrantRequired)

    def test_there_is_no_create_folder_workflow(self):
        self.assertFalse(hasattr(dds, "create_destination_folder"))
        self.assertFalse(hasattr(dds, "create_subfolder"), "no top-level folder is ever created")


class FakeAbout:
    """Only what about.get needs: the connected user's own identity."""

    def __init__(self, user=None, error=None):
        self.user, self.error, self.calls, self.fields = user, error, 0, None

    def about(self):
        return self

    def get(self, fields):
        self.fields = fields
        return self

    def execute(self):
        self.calls += 1
        if self.error:
            raise self.error
        return {"user": self.user}


USER = {"displayName": "Dest Owner", "emailAddress": "dest@example.com",
        "photoLink": "https://lh3.example/p.jpg", "permissionId": "11223344"}


class ConnectedAccountTests(unittest.TestCase):
    """Which Google account the upload runs as — the DESTINATION OAuth account,
    read from Drive's own about.get under the existing drive.file scope."""

    def setUp(self):
        dds.invalidate_connected_account()
        self.addCleanup(dds.invalidate_connected_account)

    def account(self, force_refresh=False, **kw):
        self.drive = FakeAbout(**kw)
        with patch.object(dds, "get_write_service", lambda: self.drive):
            return dds.connected_account(force_refresh=force_refresh)

    def test_returns_the_connected_identity(self):
        out = self.account(user=USER)
        self.assertEqual(out, {"connected": True, "reason": None, "account": {
            "display_name": "Dest Owner", "email": "dest@example.com",
            "photo_url": "https://lh3.example/p.jpg", "account_id": "11223344"}})

    def test_asks_drive_for_the_authenticated_user_only(self):
        self.account(user=USER)
        self.assertEqual(self.drive.fields, "user(displayName,emailAddress,photoLink,permissionId)")

    def test_never_returns_a_token_a_secret_or_the_source_service_account(self):
        blob = json.dumps(self.account(user=USER)).lower()
        for forbidden in ("token", "secret", "refresh", "credential", "gserviceaccount", "client_id"):
            self.assertNotIn(forbidden, blob)

    def test_no_connection(self):
        out = self.account(error=DriveOAuthError("Google Drive is not connected"))
        self.assertEqual((out["connected"], out["account"], out["reason"]), (False, None, "not_connected"))

    def test_revoked_or_expired_authorization(self):
        self.assertEqual(self.account(error=http_error(401))["reason"], "reauthorization_required")
        expired = DriveOAuthError("Could not refresh the connection: invalid_grant")
        self.assertEqual(self.account(error=expired)["reason"], "reauthorization_required")

    def test_temporary_google_or_network_failure(self):
        self.assertEqual(self.account(error=TimeoutError("timed out"))["reason"], "verification_unavailable")
        self.assertEqual(self.account(error=http_error(503))["reason"], "verification_unavailable")

    def test_an_account_without_an_email_is_not_reported_as_connected(self):
        out = self.account(user={"displayName": "No Email"})
        self.assertEqual((out["connected"], out["reason"]), (False, "verification_unavailable"))

    def test_the_lookup_is_cached_and_a_reconnect_invalidates_it(self):
        self.account(user=USER)
        first = self.drive.calls
        with patch.object(dds, "get_write_service", lambda: self.drive):
            dds.connected_account()
        self.assertEqual(self.drive.calls, first, "opening the panel twice must not hit Google twice")
        with patch.object(dds, "get_write_service", lambda: self.drive):
            dds.connected_account(force_refresh=True)
        self.assertEqual(self.drive.calls, first + 1, "a refresh asks Google again")
        dds.invalidate_connected_account()
        with patch.object(dds, "get_write_service", lambda: self.drive):
            dds.connected_account()
        self.assertEqual(self.drive.calls, first + 2, "a reconnect must never show the previous account")

    def test_a_failed_lookup_is_never_cached_as_connected(self):
        self.assertFalse(self.account(error=http_error(503))["connected"])
        self.assertTrue(self.account(user=USER)["connected"])


def _img(seed):
    return cv2.imencode(".jpg", np.full((16, 16, 3), seed, np.uint8))[1].tobytes()


class UploadCase(unittest.TestCase):
    """A ready batch: p1 (Alice), p2 (Bob), p3 (ambience), plus a legacy
    REVIEW file, ORIGINAL and THUMBNAILS that must never be uploaded."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-drive-flows-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.root = self.storage / "photo_batches" / BID
        self.engine = create_engine(f"sqlite:///{(Path(self.temp.name) / 't.db').as_posix()}",
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            alice = Person(participant_id="0001", first_name="Alice", last_name="A", image_path="", embedding=b"")
            bob = Person(participant_id="0002", first_name="Bob", last_name="B", image_path="", embedding=b"")
            s.add(alice)
            s.add(bob)
            s.add(PhotoBatch(id=BID, label="flows", drive_folder_id="", storage_dir=f"photo_batches/{BID}",
                             status="ready", total_photos=3, processed_photos=3, workflow_version=2))
            for name, cls in (("p1.jpg", "sorted"), ("p2.jpg", "sorted"), ("p3.jpg", "ambience")):
                s.add(PhotoBatchPhoto(batch_id=BID, filename=name, drive_file_id="", original_path="",
                                      media_path=f"photo_batches/{BID}/MEDIA/{name}", classification=cls))
            s.commit()
            self.alice_id = alice.id
        self.alice_folder = _safe_folder_name("0001", "Alice", "A")
        self.bob_folder = _safe_folder_name("0002", "Bob", "B")

        def put(rel, seed):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_img(seed))

        for i, n in enumerate(("p1.jpg", "p2.jpg", "p3.jpg"), start=1):
            put(f"MEDIA/{n}", i)
            put(f"ORIGINAL/{n}", 100 + i)
            put(f"THUMBNAILS/{n}.jpg", 200 + i)
        put(f"SORTED/{self.alice_folder}/p1.jpg", 1)
        put(f"SORTED/{self.bob_folder}/p2.jpg", 2)
        put("AMBIENCE/p3.jpg", 3)
        put("REVIEW/p1.jpg", 9)

        self.drive = FakeDrive({GRANTED: folder_meta()})
        for name, value in (("engine", self.engine), ("STORAGE_PATH", self.storage),
                            ("get_write_service", lambda: self.drive)):
            p = patch.object(dds, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(dds._release_upload, BID)

    def upload(self, people, media, ambience=False, folders=None, folder=GRANTED):
        self.assertTrue(dds.reserve_upload(BID))
        dds.run_drive_upload(BID, folder, people, media, ambience, folders)
        with Session(self.engine) as s:
            return s.get(PhotoBatch, BID)

    def subfolder_names(self):
        return sorted(i["name"] for i in self.drive.items.values() if i["mimeType"] == FOLDER)

    def subfolder_parents(self):
        return {parent for i in self.drive.items.values() if i["mimeType"] == FOLDER for parent in i["parents"]}

    def state(self):
        return json.loads((self.root / "DRIVE_UPLOAD" / "state.json").read_text(encoding="utf-8"))


class UploadSelectionTests(UploadCase):
    def test_media_only(self):
        batch = self.upload(False, True)
        self.assertEqual(self.drive.uploaded_paths(), ["MEDIA/p1.jpg", "MEDIA/p2.jpg", "MEDIA/p3.jpg"])
        self.assertEqual((batch.status, batch.drive_status, batch.processed_folder_id), ("completed", "UPLOADED", GRANTED))

    def test_people_only(self):
        self.upload(True, False)
        self.assertEqual(self.drive.uploaded_paths(),
                         sorted([f"{self.alice_folder}/p1.jpg", f"{self.bob_folder}/p2.jpg"]))

    def test_ambience_only(self):
        self.upload(False, False, True)
        self.assertEqual(self.drive.uploaded_paths(), ["AMBIENCE/p3.jpg"])

    def test_mixed(self):
        self.upload(True, True, True)
        self.assertEqual(len(self.drive.uploads), 6)

    def test_selected_person_only(self):
        self.upload(True, False, False, {self.alice_folder})
        self.assertEqual(self.drive.uploaded_paths(), [f"{self.alice_folder}/p1.jpg"])

    def test_uploads_go_directly_into_the_selected_folder(self):
        self.upload(True, True, True)
        self.assertEqual(self.subfolder_parents(), {GRANTED}, "no extra top-level folder")
        self.assertEqual(self.subfolder_names(), sorted(["MEDIA", "AMBIENCE", self.alice_folder, self.bob_folder]))

    def test_shared_drive_and_another_users_folder_upload_directly(self):
        for meta in (folder_meta(driveId="0AShared"), folder_meta(owners=[{"emailAddress": "x@example.com"}])):
            with self.subTest(meta=meta):
                self.drive = FakeDrive({GRANTED: meta})
                with patch.object(dds, "get_write_service", lambda: self.drive):
                    batch = self.upload(False, True)
                self.assertEqual(batch.status, "completed")
                self.assertEqual(self.subfolder_parents(), {GRANTED})
                self.assertEqual(len(self.drive.uploads), 3)
                dds._release_upload(BID)

    def test_the_folder_is_revalidated_before_uploading(self):
        self.upload(False, True)
        self.assertEqual(self.drive.calls[0]["fileId"], GRANTED)
        self.assertTrue(all(c.get("supportsAllDrives") is True for c in self.drive.calls))

    def test_permission_removed_after_selection_fails_closed(self):
        self.drive.metas[GRANTED] = folder_meta(can_add=False)
        batch = self.upload(True, True, True)
        self.assertEqual(self.drive.uploads, [])
        self.assertEqual(self.subfolder_names(), [])
        self.assertEqual((batch.status, batch.drive_status), ("upload_failed", "UPLOAD_FAILED"))
        self.assertIn("Select a folder you can edit with Google Picker", batch.drive_error)

    def test_access_removed_after_selection_asks_to_reselect_with_the_picker(self):
        self.drive.metas.clear()
        batch = self.upload(False, True)
        self.assertEqual((self.drive.uploads, batch.status, batch.drive_error), ([], "upload_failed", PICKER_MESSAGE))

    def test_original_thumbnails_and_review_are_never_uploaded(self):
        self.upload(True, True, True)
        self.assertFalse({"ORIGINAL", "THUMBNAILS", "REVIEW"} & set(self.subfolder_names()))
        self.assertFalse([f for _, f in self.drive.uploads if f.endswith(".jpg.jpg")])

    def test_the_reservation_is_always_released(self):
        self.upload(False, True)
        self.assertTrue(dds.reserve_upload(BID), "a finished upload must free the batch")
        dds._release_upload(BID)
        self.drive.metas.clear()
        self.upload(False, True)
        self.assertTrue(dds.reserve_upload(BID), "a refused upload must free the batch too")

    def test_an_upload_time_failure_is_recorded_as_a_specific_message(self):
        def quota(*_a, **_kw):
            try:
                raise http_error(403, "storageQuotaExceeded", {"error": {"message": "Drive storage quota exceeded. access_token=ya29.LEAK"}})
            except HttpError as inner:
                raise DriveOAuthError(f"Could not upload: {inner}") from inner

        with patch.object(dds, "_upload_file", side_effect=quota):
            batch = self.upload(False, True)
        self.assertEqual((batch.status, batch.drive_status), ("upload_failed", "UPLOAD_FAILED"))
        self.assertIn("full", batch.drive_error)
        self.assertNotIn("ya29", batch.drive_error)
        self.assertTrue((self.root / "MEDIA" / "p1.jpg").exists(), "local output survives a failed upload")


class RetryIdempotencyTests(UploadCase):
    """A Retry reconciles with what is already in the destination, by a stable
    per-output key — never by filename, and never by uploading everything again."""

    def test_a_partial_upload_then_retry_leaves_one_copy_of_each_output(self):
        self.drive.fail_upload_after = 2
        first = self.upload(True, True, True)
        self.assertEqual(first.status, "upload_failed")
        self.assertEqual(len(self.drive.uploads), 3, "the third upload failed")
        dds._release_upload(BID)

        self.drive.fail_upload_after = None
        second = self.upload(True, True, True)

        self.assertEqual(second.status, "completed")
        self.assertEqual(len(self.drive.uploads), 3 + 4, "only the files that never landed are uploaded again")
        self.assertEqual(len(self.drive.live_paths()), 6)
        self.assertEqual(len(set(self.drive.live_paths())), 6, "no duplicate file in the destination")

    def test_a_lost_answer_is_reconciled_instead_of_uploaded_again(self):
        # Google stored the file; the response never came back, so nothing was
        # recorded locally. The retry must find that file by its key.
        self.drive.lose_answer_after = 0
        self.assertEqual(self.upload(False, True).status, "upload_failed")
        stored_after_first = len(self.drive.live_paths())
        self.assertEqual(stored_after_first, 1, "Drive kept the file the caller never heard about")
        self.assertEqual(self.state()["files"], {}, "nothing was recorded locally for it")
        dds._release_upload(BID)

        self.drive.lose_answer_after = None
        batch = self.upload(False, True)

        self.assertEqual(batch.status, "completed")
        self.assertEqual(self.drive.live_paths(), ["MEDIA/p1.jpg", "MEDIA/p2.jpg", "MEDIA/p3.jpg"])
        self.assertEqual(len(self.drive.uploads), 1 + 2, "the reconciled file is not uploaded again")

    def test_a_completed_upload_repeated_uploads_nothing(self):
        self.upload(True, True, True)
        uploads = len(self.drive.uploads)
        dds._release_upload(BID)
        batch = self.upload(True, True, True)
        self.assertEqual(batch.status, "completed")
        self.assertEqual(len(self.drive.uploads), uploads, "everything was already there")
        self.assertEqual(len(self.drive.live_paths()), 6)

    def test_the_same_filename_in_another_folder_is_never_treated_as_the_same_output(self):
        self.upload(True, True, True)
        # MEDIA/p1.jpg and SORTED/<alice>/p1.jpg share a filename and are both
        # uploaded, into different folders, under different keys.
        self.assertIn("MEDIA/p1.jpg", self.drive.live_paths())
        self.assertIn(f"{self.alice_folder}/p1.jpg", self.drive.live_paths())
        keys = {v["file_id"]: k for k, v in self.state()["files"].items()}
        self.assertEqual(len(keys), 6, "one distinct key per output")

    def test_a_same_named_foreign_file_in_the_destination_is_not_mistaken_for_ours(self):
        media_id = self.drive.add_item("MEDIA", GRANTED, mime=FOLDER)
        self.drive.add_item("p1.jpg", media_id)  # somebody else's file, no Reconize key
        self.upload(False, True)
        self.assertEqual(len(self.drive.uploads), 3, "our own copy is still uploaded")
        self.assertEqual(self.drive.live_paths().count("MEDIA/p1.jpg"), 2)

    def test_an_existing_subfolder_is_adopted_once_and_stamped(self):
        media_id = self.drive.add_item("MEDIA", GRANTED, mime=FOLDER)
        self.upload(False, True)
        self.assertEqual([i["id"] for i in self.drive.items.values() if i["mimeType"] == FOLDER], [media_id],
                         "the existing MEDIA folder is reused, not duplicated")
        self.assertIn("reconizeUploadKey", self.drive.items[media_id]["appProperties"])
        self.assertEqual(self.state()["folders"]["MEDIA"], media_id)

    def test_a_trashed_or_moved_file_is_uploaded_again(self):
        self.upload(False, True)
        dds._release_upload(BID)
        first_key = sorted(self.state()["files"])[0]
        gone = self.state()["files"][first_key]["file_id"]
        self.drive.items[gone]["trashed"] = True

        self.upload(False, True)

        self.assertEqual(len(self.drive.uploads), 4, "the missing file is replaced, the others are not")

    def test_duplicate_keys_in_the_destination_stop_the_upload(self):
        self.upload(False, True)
        dds._release_upload(BID)
        key, entry = next(iter(self.state()["files"].items()))
        original = self.drive.items[entry["file_id"]]
        self.drive.add_item(original["name"], entry["parent"], {"reconizeUploadKey": key})
        # The recorded id is still valid, so force the key lookup path.
        state = self.state()
        state["files"][key]["file_id"] = "missing-id"
        (self.root / "DRIVE_UPLOAD" / "state.json").write_text(json.dumps(state), encoding="utf-8")

        batch = self.upload(False, True)

        self.assertEqual(batch.status, "upload_failed")
        self.assertIn("2 copies", batch.drive_error)
        self.assertIn("will not add another copy", batch.drive_error)

    def test_a_changed_destination_never_reuses_the_previous_folders_ids(self):
        self.upload(False, True)
        dds._release_upload(BID)
        other = "1OtherDestination0000000"
        self.drive.metas[other] = folder_meta(other, "Another folder")

        batch = self.upload(False, True, folder=other)

        self.assertEqual(batch.status, "completed")
        self.assertEqual(batch.processed_folder_id, other)
        self.assertEqual(self.state()["destination"], other)
        self.assertEqual(len(self.drive.uploads), 6, "the new destination gets its own copies")
        media_in_other = self.drive.folder_named("MEDIA", parent=other)
        self.assertIsNotNone(media_in_other)
        self.assertEqual(self.state()["folders"]["MEDIA"], media_in_other)

    def test_permission_removed_before_retry_fails_closed_without_touching_drive(self):
        self.drive.fail_upload_after = 1
        self.assertEqual(self.upload(False, True).status, "upload_failed")
        dds._release_upload(BID)
        uploads = len(self.drive.uploads)
        self.drive.metas[GRANTED] = folder_meta(can_add=False)

        batch = self.upload(False, True)

        self.assertEqual(len(self.drive.uploads), uploads, "no file is touched when revalidation fails")
        self.assertIn("cannot add files", batch.drive_error)

    def test_the_upload_key_is_stable_and_specific(self):
        key = dds.upload_key(BID, "MEDIA/p1.jpg")
        self.assertEqual(key, dds.upload_key(BID, "MEDIA/p1.jpg"))
        self.assertNotEqual(key, dds.upload_key(BID, f"SORTED/{self.alice_folder}/p1.jpg"))
        self.assertNotEqual(key, dds.upload_key("e" * 32, "MEDIA/p1.jpg"))

    def test_the_upload_state_is_never_exportable(self):
        from app.services.export_selection_service import ExportSelection, eligible

        self.upload(False, True)
        rel = (self.root / "DRIVE_UPLOAD" / "state.json").relative_to(self.root).parts
        selection = ExportSelection(media=True, sorted=True, ambience=True)
        self.assertFalse(eligible(rel, selection, None))
        self.assertEqual(dds._iter_upload_files(self.root, True, True, True),
                         [f for f in dds._iter_upload_files(self.root, True, True, True)
                          if "DRIVE_UPLOAD" not in f.path.parts])


def asgi(app, method, path, body=None):
    path, _, query = path.partition("?")

    async def run():
        messages, raw = [], json.dumps(body).encode() if body is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(m):
            messages.append(m)

        await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": method,
                   "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                   "root_path": "",
                   "headers": [(b"content-type", b"application/json")], "http_version": "1.1",
                   "client": ("127.0.0.1", 1), "server": ("t", 80)}, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        data = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return SimpleNamespace(status=start["status"], json=json.loads(data) if data else None)

    return asyncio.run(run())


class RouteFlowTests(UploadCase):
    """Both destination flows through the real routes: validation, the
    ExportSelection shape, the reservation, and the background upload call."""

    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.include_router(pb.router)

        def session_override():
            with Session(self.engine) as s:
                yield s

        self.app.dependency_overrides[get_current_user] = lambda: User(username="admin", password_hash="x")
        self.app.dependency_overrides[get_session] = session_override
        self.started = []
        for name, value in (("STORAGE_PATH", self.storage),
                            ("run_drive_upload", lambda *a: self.started.append(a))):
            p = patch.object(pb, name, value)
            p.start()
            self.addCleanup(p.stop)

    def validate(self, **body):
        return asgi(self.app, "POST", f"/api/photo-batches/{BID}/drive-destination/validate", body)

    def post_upload(self, body):
        r = asgi(self.app, "POST", f"/api/photo-batches/{BID}/drive-upload", body)
        dds._release_upload(BID)
        return r

    def test_picker_folder_id_flow(self):
        v = self.validate(folder_id=GRANTED)
        self.assertEqual((v.status, v.json["folder_name"], v.json["folder_url"]),
                         (200, "Dest", f"https://drive.google.com/drive/folders/{GRANTED}"))
        r = self.post_upload({"folder_id": GRANTED, "upload_people": False, "upload_media": True})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.started[0][:5], (BID, GRANTED, False, True, False))

    def test_a_pasted_folder_before_and_after_the_picker_grant(self):
        other = "1OtherUsersFolder00000000"
        link = f"https://drive.google.com/drive/folders/{other}"
        before = self.validate(folder_url=link)
        # Not an error page: the pasted id comes back, awaiting one confirmation.
        self.assertEqual((before.status, before.json["requires_picker_grant"], before.json["pasted_folder_id"]),
                         (200, True, other))
        self.drive.metas[other] = folder_meta(other, "Shared by a colleague")  # selected with the Picker
        after = self.validate(folder_url=link)
        self.assertEqual((after.status, after.json["folder_id"]), (200, other))
        self.assertEqual(self.post_upload({"folder_url": link, "upload_people": True, "upload_media": True}).status, 200)

    def test_every_selection_shape_reaches_the_worker(self):
        shapes = [
            ({"upload_people": False, "upload_media": True}, (False, True, False, None)),
            ({"upload_people": True, "upload_media": False}, (True, False, False, None)),
            ({"upload_people": False, "upload_media": False, "upload_ambience": True}, (False, False, True, None)),
            ({"upload_people": True, "upload_media": True, "upload_ambience": True}, (True, True, True, None)),
            ({"upload_people": True, "upload_media": False, "person_ids": None}, (True, False, False, None)),
        ]
        for body, expected in shapes:
            with self.subTest(body=body):
                self.started.clear()
                self.assertEqual(self.post_upload({"folder_id": GRANTED, **body}).status, 200)
                self.assertEqual(self.started[0][2:], expected)

    def test_a_selected_person_only_upload_passes_their_folder(self):
        r = self.post_upload({"folder_id": GRANTED, "upload_people": True, "upload_media": False,
                              "person_ids": [self.alice_id]})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.started[0][5], {self.alice_folder})

    def test_nothing_selected_is_refused(self):
        r = self.post_upload({"folder_id": GRANTED, "upload_people": False, "upload_media": False})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.started, [])

    def test_a_viewer_folder_is_refused_before_any_upload(self):
        self.drive.metas[GRANTED] = folder_meta(can_add=False)
        r = self.post_upload({"folder_id": GRANTED, "upload_people": True, "upload_media": True})
        self.assertEqual(r.status, 400)
        self.assertIn("cannot add files", r.json["detail"])
        self.assertEqual(self.started, [])

    def test_an_invalid_link_is_a_400_with_the_parser_message(self):
        r = self.validate(folder_url="https://drive.google.com/file/d/abcdefghijklmnop/view")
        self.assertEqual(r.status, 400)
        self.assertIn("file, not a folder", r.json["detail"])

    def status(self, query=""):
        return asgi(self.app, "GET", f"/api/photo-batches/drive-oauth/status{query}")

    def test_a_folder_never_granted_returns_the_picker_grant_state(self):
        never = "1NeverGrantedFolder00000"
        r = self.validate(folder_url=f"https://drive.google.com/drive/folders/{never}")
        self.assertEqual(r.status, 200)
        self.assertEqual((r.json["requires_picker_grant"], r.json["pasted_folder_id"]), (True, never))
        self.assertIn("one confirmation", r.json["message"])
        self.assertNotIn("reconnect", r.json["message"].lower())

    def test_an_already_granted_folder_is_accepted_without_the_picker(self):
        r = self.validate(folder_url=f"https://drive.google.com/drive/folders/{GRANTED}")
        self.assertEqual((r.status, r.json["folder_id"]), (200, GRANTED))
        self.assertNotIn("requires_picker_grant", r.json)

    def test_after_the_one_time_grant_the_same_pasted_link_keeps_working(self):
        never = "1NeedsOneConfirmation000"
        link = f"https://drive.google.com/drive/folders/{never}"
        self.assertTrue(self.validate(folder_url=link).json["requires_picker_grant"])
        # The Picker confirmation is what makes the folder visible to drive.file.
        self.drive.metas[never] = folder_meta(never, "Shared by a colleague",
                                              owners=[{"emailAddress": "someone@example.com"}])
        picked = self.validate(folder_id=never)
        self.assertEqual((picked.status, picked.json["folder_name"]), (200, "Shared by a colleague"))
        again = self.validate(folder_url=link)
        self.assertEqual((again.status, again.json["folder_id"]), (200, never))
        self.assertNotIn("requires_picker_grant", again.json)

    def test_a_revoked_grant_asks_for_the_picker_again(self):
        self.assertEqual(self.validate(folder_id=GRANTED).status, 200)
        del self.drive.metas[GRANTED]
        r = self.validate(folder_url=f"https://drive.google.com/drive/folders/{GRANTED}")
        self.assertTrue(r.json["requires_picker_grant"])

    def test_a_shared_drive_folder_is_accepted_once_granted(self):
        shared = "1SharedDriveFolder00000"
        self.assertTrue(self.validate(folder_id=shared).json["requires_picker_grant"])
        self.drive.metas[shared] = folder_meta(shared, "Team drive folder", driveId="0AShared")
        r = self.validate(folder_id=shared)
        self.assertEqual((r.status, r.json["folder_name"]), (200, "Team drive folder"))
        self.assertTrue(all(c.get("supportsAllDrives") is True for c in self.drive.calls))

    def test_uploading_to_a_folder_that_still_needs_the_grant_is_refused(self):
        r = self.post_upload({"folder_id": "1NeverGrantedFolder00000", "upload_people": True, "upload_media": True})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.started, [])

    def test_the_status_route_reports_the_destination_account(self):
        state = {"connected": True, "reason": None, "account": {
            "display_name": "Dest Owner", "email": "dest@example.com", "photo_url": None, "account_id": "11223344"}}
        with patch.object(pb, "is_connected", return_value=True), \
                patch.object(pb, "connected_account", return_value=state) as lookup:
            r = self.status()
            refreshed = self.status("?refresh=true")
        self.assertEqual((r.status, r.json["connected"], r.json["account"]["email"]), (200, True, "dest@example.com"))
        self.assertEqual(r.json["email"], "dest@example.com", "kept for the batch-list page")
        self.assertIsNone(r.json["reason"])
        self.assertNotIn("service_account_email", r.json)
        self.assertEqual(refreshed.status, 200)
        self.assertEqual([c.kwargs.get("force_refresh") for c in lookup.call_args_list], [False, True])

    def test_the_status_route_never_calls_google_without_a_connection(self):
        with patch.object(pb, "is_connected", return_value=False), patch.object(pb, "connected_account") as lookup:
            r = self.status()
        lookup.assert_not_called()
        self.assertEqual((r.json["connected"], r.json["reason"], r.json["account"]), (False, "not_connected", None))

    def test_batch_progress_polling_never_looks_up_the_account(self):
        with patch.object(pb, "connected_account") as lookup:
            r = asgi(self.app, "GET", f"/api/photo-batches/{BID}")
        self.assertEqual(r.status, 200)
        lookup.assert_not_called()

    def test_the_create_folder_route_is_gone(self):
        paths = [r.path for r in pb.router.routes]
        self.assertNotIn("/api/photo-batches/drive-destination/create-folder", paths)
        first = min(i for i, p in enumerate(paths) if p.startswith("/api/photo-batches/{batch_id}"))
        self.assertLess(paths.index("/api/photo-batches/drive-picker/config"), first)


class SourceIndependenceTests(unittest.TestCase):
    def test_the_destination_module_never_touches_the_source_service_account(self):
        import inspect

        source = inspect.getsource(dds)
        self.assertNotIn("google_drive_folder_service", source)
        self.assertNotIn("service_account", source)


if __name__ == "__main__":
    unittest.main()
