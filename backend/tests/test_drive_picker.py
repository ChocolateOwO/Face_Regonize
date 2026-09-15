"""Phase H — Google Picker for the Drive DESTINATION folder.

Pins the token endpoint's protections (Bearer JWT, custom header, origin
allow-list, no-store, token-only body), the non-secret config, route order,
folder-id resolution, the tightened canAddChildren check and Shared Drive
flags. No real Google call is made: every Drive/OAuth call is faked.
"""
import asyncio
from datetime import datetime, timedelta
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI

from app.api import photo_batches as pb
from app.auth.deps import get_current_user
from app.models.models import User
from app.services import drive_destination_service as dds
from app.services import google_drive_oauth_service as oauth


def asgi(app, method, path, headers=None):
    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": method,
                   "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                   "headers": raw, "http_version": "1.1", "client": ("127.0.0.1", 1234),
                   "server": ("isolated", 80)}, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return SimpleNamespace(status=start["status"], json=json.loads(body) if body else None,
                               headers={k.decode().lower(): v.decode() for k, v in start["headers"]})

    return asyncio.run(run())


class PickerCase(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(pb.router)
        self.app.dependency_overrides[get_current_user] = lambda: User(username="admin", password_hash="x")
        for name, value in (("FRONTEND_URL", "http://localhost:5180"), ("BACKEND_BASE_URL", "http://127.0.0.1:8010"),
                            ("PICKER_ALLOWED_ORIGINS", []), ("GOOGLE_DRIVE_CLIENT_ID", "client.apps.example"),
                            ("GOOGLE_PICKER_API_KEY", "public-api-key"), ("GOOGLE_CLOUD_PROJECT_NUMBER", "123456789"),
                            ("is_connected", lambda: True)):
            p = patch.object(pb, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.minted = []

        def fake_mint():
            self.minted.append(1)
            return {"access_token": "ya29.short-lived", "expires_in": 3599, "scope": oauth.SCOPES}

        p = patch.object(oauth, "mint_picker_access_token", side_effect=fake_mint)
        p.start()
        self.addCleanup(p.stop)

    def token(self, headers):
        return asgi(self.app, "POST", "/api/photo-batches/drive-picker/token", headers)


class TokenTests(PickerCase):
    GOOD = {"x-reconize-picker": "1", "origin": "http://localhost:5180", "host": "127.0.0.1:8010"}

    def test_the_custom_header_is_required(self):
        headers = dict(self.GOOD)
        del headers["x-reconize-picker"]
        self.assertEqual(self.token(headers).status, 403)
        self.assertEqual(self.minted, [])

    def test_a_foreign_origin_is_refused_before_minting(self):
        r = self.token({**self.GOOD, "origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.minted, [])

    def test_no_origin_and_no_referer_is_refused(self):
        headers = {"x-reconize-picker": "1", "host": "127.0.0.1:8010"}
        self.assertEqual(self.token(headers).status, 403)

    def test_an_allowed_origin_gets_only_a_short_lived_token_with_no_store(self):
        r = self.token(self.GOOD)
        self.assertEqual(r.status, 200)
        self.assertEqual(set(r.json), {"access_token", "expires_in"})
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.headers["pragma"], "no-cache")

    def test_same_origin_on_a_lan_address_is_allowed(self):
        r = self.token({"x-reconize-picker": "1", "origin": "http://192.168.1.3:8000", "host": "192.168.1.3:8000"})
        self.assertEqual(r.status, 200)

    def test_referer_is_the_fallback_when_origin_is_absent(self):
        r = self.token({"x-reconize-picker": "1", "referer": "http://localhost:5180/photo-batches/x",
                        "host": "127.0.0.1:8010"})
        self.assertEqual(r.status, 200)

    def test_configured_extra_origins_are_honoured(self):
        with patch.object(pb, "PICKER_ALLOWED_ORIGINS", ["https://reconize.example.org"]):
            r = self.token({**self.GOOD, "origin": "https://reconize.example.org"})
        self.assertEqual(r.status, 200)

    def test_login_is_required(self):
        self.app.dependency_overrides.clear()
        self.assertEqual(self.token(self.GOOD).status, 401)
        self.assertEqual(self.minted, [])

    def test_an_oauth_failure_is_a_clear_409(self):
        with patch.object(oauth, "mint_picker_access_token", side_effect=oauth.DriveOAuthError("Google Drive is not connected")):
            r = self.token(self.GOOD)
        self.assertEqual(r.status, 409)
        self.assertIn("not connected", r.json["detail"])

    def test_the_token_route_logs_nothing(self):
        self.assertNotIn("logger", inspect.getsource(pb.drive_picker_token))


class MintTests(unittest.TestCase):
    def test_minting_returns_the_access_token_and_lifetime_only(self):
        creds = SimpleNamespace(token="ya29.x", expiry=datetime.utcnow() + timedelta(seconds=1800),
                                refresh_token="1//refresh-must-not-leak", client_secret="secret-must-not-leak")
        with patch.object(oauth, "_get_write_credentials", return_value=creds):
            minted = oauth.mint_picker_access_token()
        self.assertEqual(set(minted), {"access_token", "expires_in", "scope"})
        self.assertTrue(1700 <= minted["expires_in"] <= 1800)
        self.assertNotIn("must-not-leak", json.dumps(minted))


class ConfigTests(PickerCase):
    def config(self):
        return asgi(self.app, "GET", "/api/photo-batches/drive-picker/config")

    def test_only_public_values_are_exposed(self):
        with patch.object(oauth, "GOOGLE_DRIVE_CLIENT_SECRET", "CLIENT-SECRET-VALUE"):
            r = self.config()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.json["enabled"])
        self.assertEqual(set(r.json), {"enabled", "connected", "missing", "client_id", "api_key", "app_id"})
        self.assertNotIn("CLIENT-SECRET-VALUE", json.dumps(r.json))
        self.assertFalse([k for k in r.json if "secret" in k or "refresh" in k or "token" in k])

    def test_missing_values_are_named_and_disable_the_picker(self):
        with patch.object(pb, "GOOGLE_PICKER_API_KEY", ""):
            r = self.config()
        self.assertFalse(r.json["enabled"])
        self.assertEqual(r.json["missing"], ["GOOGLE_PICKER_API_KEY"])

    def test_not_connected_disables_the_picker(self):
        with patch.object(pb, "is_connected", lambda: False):
            r = self.config()
        self.assertFalse(r.json["enabled"])
        self.assertFalse(r.json["connected"])


class RouteOrderTests(unittest.TestCase):
    def test_picker_routes_are_registered_before_the_batch_id_routes(self):
        paths = [r.path for r in pb.router.routes]
        first_batch_route = min(i for i, p in enumerate(paths) if p.startswith("/api/photo-batches/{batch_id}"))
        for literal in ("/api/photo-batches/drive-picker/config", "/api/photo-batches/drive-picker/token"):
            self.assertLess(paths.index(literal), first_batch_route, literal)


class FolderRefTests(unittest.TestCase):
    def test_a_picker_folder_id_is_accepted_only_in_id_shape(self):
        self.assertEqual(dds.resolve_folder_ref(None, "1AbCdEfGhIjKlMnOpQrStUvWxYz0"), "1AbCdEfGhIjKlMnOpQrStUvWxYz0")
        for bad in ("../../etc", "short", "has space in it here", "https://drive.google.com/drive/folders/x"):
            with self.assertRaises(dds.DriveDestinationError, msg=bad):
                dds.resolve_folder_ref(None, bad)

    def test_a_url_still_goes_through_the_strict_parser(self):
        self.assertEqual(dds.resolve_folder_ref("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0"),
                         "1AbCdEfGhIjKlMnOpQrStUvWxYz0")
        with self.assertRaises(dds.DriveDestinationError):
            dds.resolve_folder_ref(None, None)


class FakeDrive:
    def __init__(self, meta):
        self.meta, self.calls = meta, []

    def files(self):
        return self

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return self

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self

    def execute(self):
        return self.meta


class ValidateTests(unittest.TestCase):
    def validate(self, meta):
        drive = FakeDrive(meta)
        with patch.object(dds, "get_write_service", return_value=drive):
            return dds.validate_destination("1AbCdEfGhIjKlMnOpQrStUvWxYz0"), drive

    FOLDER = {"id": "F", "name": "Delivery", "mimeType": "application/vnd.google-apps.folder", "trashed": False}

    def test_a_missing_capability_is_now_refused(self):
        for caps in ({}, {"canAddChildren": None}, None):
            with self.subTest(caps=caps), self.assertRaises(dds.DriveDestinationError):
                self.validate({**self.FOLDER, "capabilities": caps})

    def test_an_explicit_true_passes_and_shared_drives_are_supported(self):
        result, drive = self.validate({**self.FOLDER, "driveId": "0AShared", "capabilities": {"canAddChildren": True}})
        self.assertTrue(result["can_upload"])
        self.assertTrue(drive.calls[0][1]["supportsAllDrives"])

    def test_writes_into_a_shared_drive_pass_the_flag(self):
        drive = FakeDrive({"id": "NEW"})
        with patch.object(oauth, "get_write_service", return_value=drive):
            self.assertEqual(oauth.create_subfolder("0AShared", "run"), "NEW")
        self.assertTrue(drive.calls[0][1]["supportsAllDrives"])


class SourceIndependenceTests(unittest.TestCase):
    def test_the_picker_code_never_touches_the_source_service_account(self):
        source = inspect.getsource(pb.drive_picker_token) + inspect.getsource(pb.drive_picker_config)
        self.assertNotIn("service_account", source)
        self.assertNotIn("google_drive_folder_service", source)


if __name__ == "__main__":
    unittest.main()
