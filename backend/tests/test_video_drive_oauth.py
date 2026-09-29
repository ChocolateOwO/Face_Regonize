"""Video-only incremental read permission; fake tokens/settings, no Google."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
from fastapi import FastAPI
from app.api import local_video_drive as api, photo_batches
from app.auth.deps import get_current_user
from app.models.models import User, Setting
from app.services import google_drive_oauth_service as oauth
from test_local_video_experiment import request


class VideoDriveOAuthTests(unittest.TestCase):
    def setUp(self):
        self.rows = {}
        rows = self.rows
        class Session:
            def __init__(self, engine): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def get(self, model, key): return rows.get(key)
            def add(self, row): rows[row.key] = row
            def commit(self): pass
        for name, value in (("Session", Session), ("GOOGLE_DRIVE_CLIENT_ID", "synthetic-client"),
            ("GOOGLE_DRIVE_CLIENT_SECRET", "synthetic-secret")):
            p = patch.object(oauth, name, value); p.start(); self.addCleanup(p.stop)
        self.app = FastAPI()
        self.app.include_router(api.router)
        self.app.dependency_overrides[get_current_user] = lambda: User(username="synthetic", password_hash="x", role="admin")

    def call(self, path):
        code, body, _ = asyncio.run(request(self.app, "/api/local-video-experiment/drive" + path))
        return code, json.loads(body)

    def exchange(self, scopes=None):
        token = {"refresh_token": "synthetic-refresh", "access_token": "synthetic-access"}
        if scopes is not None: token["scope"] = scopes
        with patch.object(oauth.requests, "post", return_value=SimpleNamespace(status_code=200, json=lambda: token)), \
            patch.object(oauth.requests, "get", return_value=SimpleNamespace(status_code=200, json=lambda: {"email": "fake@example.test"})):
            self.assertEqual(oauth.exchange_code_and_store("synthetic-code"), "fake@example.test")

    def test_default_legacy_consent_remains_drive_file_only(self):
        params = parse_qs(urlparse(oauth.get_auth_url("fake-state")).query)
        self.assertEqual(params["scope"], [oauth.SCOPES])
        self.assertNotIn("include_granted_scopes", params)

    def test_video_start_adds_readonly_and_retains_file_with_same_callback_project(self):
        code, response = self.call("/oauth/start")
        self.assertEqual(code, 200)
        params = parse_qs(urlparse(response["auth_url"]).query)
        self.assertEqual(set(params["scope"][0].split()), set(oauth.VIDEO_SCOPES))
        self.assertNotIn("https://www.googleapis.com/auth/drive", params["scope"][0].split())
        self.assertEqual(params["include_granted_scopes"], ["true"])
        self.assertEqual(params["prompt"], ["consent"])
        self.assertEqual(params["redirect_uri"], [oauth.OAUTH_REDIRECT_URI])
        self.assertEqual(params["client_id"], ["synthetic-client"])
        self.assertTrue(oauth.verify_and_clear_pending_state(params["state"][0]))
        self.assertFalse(oauth.verify_and_clear_pending_state(params["state"][0]), "No callback replay")

    def test_unknown_old_grant_and_partial_consent_require_reconnection(self):
        self.assertFalse(oauth.has_video_read_scope())
        self.exchange(oauth.SCOPES)
        self.assertFalse(oauth.has_video_read_scope())
        self.exchange()
        self.assertFalse(oauth.has_video_read_scope(), "Missing grant metadata cannot imply readonly")
        with patch.object(oauth, "_get_credentials", side_effect=AssertionError("No token/network before consent")):
            with self.assertRaises(oauth.DriveScopeRequired): oauth.get_video_read_service()

    def test_video_service_uses_readonly_token_and_does_not_expand_writer_or_picker(self):
        self.exchange(" ".join(oauth.VIDEO_SCOPES))
        self.assertTrue(oauth.has_video_read_scope())
        creds = SimpleNamespace(granted_scopes=[oauth.VIDEO_READ_SCOPE], token="synthetic", expiry=None)
        with patch.object(oauth, "_get_credentials", return_value=creds) as refresh, \
            patch("googleapiclient.discovery.build", return_value="fake-service"):
            self.assertEqual(oauth.get_video_read_service(), "fake-service")
            refresh.assert_called_once_with([oauth.VIDEO_READ_SCOPE])
        with patch.object(oauth, "_get_credentials", return_value=creds) as refresh:
            oauth._get_write_credentials()
            refresh.assert_called_once_with([oauth.SCOPES])
        with patch.object(oauth, "_get_write_credentials", return_value=creds):
            self.assertEqual(oauth.mint_picker_access_token()["scope"], oauth.SCOPES)

    def test_refreshed_scope_reduction_is_not_silently_accepted(self):
        self.exchange(" ".join(oauth.VIDEO_SCOPES))
        with patch.object(oauth, "_get_credentials", return_value=SimpleNamespace(granted_scopes=[oauth.SCOPES])):
            with self.assertRaises(oauth.DriveScopeRequired): oauth.get_video_read_service()

    def test_granular_readonly_only_consent_preserves_existing_file_connection(self):
        self.exchange(" ".join(oauth.VIDEO_SCOPES))
        before = {key: row.value for key, row in self.rows.items()}
        with self.assertRaisesRegex(oauth.DriveOAuthError, "previous connection was preserved"):
            self.exchange(oauth.VIDEO_READ_SCOPE)
        self.assertEqual({key: row.value for key, row in self.rows.items()}, before)

    def test_status_identifies_reconnect_without_exposing_tokens(self):
        account = {"email": "fake@example.test", "account_id": "fake"}
        with patch.object(photo_batches, "drive_oauth_status", return_value={"connected": True, "account": account}):
            code, state = self.call("/status")
            self.assertEqual(code, 200)
            self.assertTrue(state["requires_reconnect"]); self.assertFalse(state["read_access"])
            self.exchange(" ".join(oauth.VIDEO_SCOPES))
            _, state = self.call("/status")
            self.assertTrue(state["read_access"]); self.assertFalse(state["requires_reconnect"])
        self.assertNotIn("synthetic-refresh", json.dumps(state))
        self.assertNotIn("synthetic-access", json.dumps(state))

if __name__ == "__main__": unittest.main()
