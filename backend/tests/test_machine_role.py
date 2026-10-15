"""Phase 3 — machine account (robot): deny by default, explicit allowlist.

The route-table walk covers every HTTP route the app registers, so a route
added later is checked automatically: only the allowlist may accept a
machine token, and the set of routes that need no token at all is pinned."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import BackgroundTasks, HTTPException, UploadFile
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session, SQLModel, create_engine, select

from app.auth import deps
from app.auth.security import create_access_token, verify_password
from app.models.models import Setting, User

ALLOWLIST = {
    ("GET", "/api/auth/me"),
    ("POST", "/api/look"),
    ("POST", "/api/recognition/upload"),
    ("GET", "/api/history"),
    ("GET", "/api/reports/dashboard"),
    ("GET", "/api/node/status"),
    ("GET", "/api/node/{node_id}/present"),
    ("GET", "/api/settings"),
}
# Operational writes a non-admin STATION account must still perform (kiosk
# scan/consent, CCTV node traffic, the file-read cookie). Every other write is
# admin-only through get_current_user.
STATION = {
    ("POST", "/api/auth/file-session"), ("POST", "/api/pdpa/record"),
    ("POST", "/api/node/register"), ("POST", "/api/node/heartbeat"), ("POST", "/api/node/event"),
    ("POST", "/api/node/recognize-central"), ("POST", "/api/node/recording/start"),
    ("POST", "/api/node/recording/stop"), ("POST", "/api/node/observation"),
}
# Routes that need no token; pinned so a change is noticed. Login, health, the
# OAuth callback, version and the import template are public by design;
# DELETE file-session only clears the caller's own cookie.
PUBLIC = {
    ("POST", "/api/auth/login"), ("GET", "/api/health"), ("GET", "/api/import/template"),
    ("GET", "/api/photo-batches/drive-oauth/callback"), ("GET", "/api/update/version"),
    ("DELETE", "/api/auth/file-session"),
}
# Stored files: signed-in humans via the file-read cookie or a Bearer token.
FILES = {("GET", "/api/files/{path:path}")}


def _walk(routes):
    for route in routes:
        if hasattr(route, "original_router"):  # FastAPI's included-router wrapper
            yield from _walk(route.original_router.routes)
        else:
            yield route


def _calls(dependant, acc):
    for dep in dependant.dependencies:
        acc.add(dep.call)
        _calls(dep, acc)
    return acc


class RouteTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        cls.routes = list(_walk(app.routes))

    def test_every_route_has_exactly_its_intended_access_class(self):
        """Machine tokens only on ALLOWLIST; non-admin writes only on STATION
        (and ALLOWLIST); every other route goes through get_current_user,
        which makes all its writes admin-only; nothing else is public."""
        from app.api import files
        auth = {deps.get_api_client, deps.get_current_user, deps.get_station_user, files.file_reader}
        seen = {name: set() for name in ("allow", "station", "public", "files")}
        for route in self.routes:
            if not isinstance(route, APIRoute):
                continue
            calls = _calls(route.dependant, set())
            for method in route.methods:
                key = (method, route.path)
                if key in ALLOWLIST:
                    seen["allow"].add(key)
                    self.assertEqual(calls & auth, {deps.get_api_client}, key)
                elif key in STATION:
                    seen["station"].add(key)
                    self.assertEqual(calls & auth, {deps.get_station_user}, key)
                elif key in FILES:
                    seen["files"].add(key)
                    self.assertEqual(calls & auth, {files.file_reader}, key)
                elif key in PUBLIC:
                    seen["public"].add(key)
                    self.assertFalse(calls & auth, key)
                else:
                    self.assertIn(deps.get_current_user, calls, f"{key} has no auth dependency")
                    self.assertFalse({deps.get_api_client, deps.get_station_user} & calls, key)
        self.assertEqual(seen, {"allow": ALLOWLIST, "station": STATION, "public": PUBLIC, "files": FILES})

    def test_websockets_are_the_known_two(self):
        ws = {r.path for r in self.routes if isinstance(r, APIWebSocketRoute)}
        self.assertEqual(ws, {"/api/node/signal", "/api/node/events-feed"})


class MachineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-machine-")
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine(f"sqlite:///{(Path(self.temp.name) / 't.db').as_posix()}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.admin = User(username="admin", password_hash="x", role="admin")
            self.machine = User(username="robot-hub", password_hash="x", role="machine")
            s.add(self.admin)
            s.add(self.machine)
            s.add(Setting(key="google_drive_oauth_refresh_token", value="SECRET"))
            s.add(Setting(key="camera_label", value="Logitech C920"))
            s.commit()
            s.refresh(self.admin)
            s.refresh(self.machine)

    def creds(self, user: User) -> HTTPAuthorizationCredentials:
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=create_access_token(user.id, user.username))

    def session(self) -> Session:
        s = Session(self.engine)
        self.addCleanup(s.close)
        return s


def request(method: str = "GET", cookie: str | None = None):
    from starlette.requests import Request
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": method, "path": "/", "headers": headers,
                    "query_string": b"", "scheme": "http", "server": ("test", 80)})


class DependencyTests(MachineCase):
    def test_get_current_user_refuses_machine_but_keeps_admin(self):
        for method in ("GET", "POST", "DELETE"):
            self.assertEqual(deps.get_current_user(request(method), self.creds(self.admin), self.session()).id,
                             self.admin.id)
            with self.assertRaises(HTTPException) as ctx:
                deps.get_current_user(request(method), self.creds(self.machine), self.session())
            self.assertEqual(ctx.exception.status_code, 403)

    def test_get_api_client_accepts_both(self):
        for user in (self.admin, self.machine):
            self.assertEqual(deps.get_api_client(self.creds(user), self.session()).id, user.id)

    def test_bad_tokens_are_401_for_both(self):
        for dep in (lambda c, s: deps.get_current_user(request(), c, s), deps.get_api_client, deps.get_station_user):
            for creds in (None, HTTPAuthorizationCredentials(scheme="Bearer", credentials="garbage")):
                with self.assertRaises(HTTPException) as ctx:
                    dep(creds, self.session())
                self.assertEqual(ctx.exception.status_code, 401)


class AllowlistBehaviourTests(MachineCase):
    def test_settings_are_filtered_for_machine_only(self):
        from app.api import settings_routes
        machine_view = settings_routes.get_settings(session=self.session(), user=self.machine)
        self.assertEqual(machine_view, {"camera_label": "Logitech C920", "app_name": "Reconize",
                                        "event_name": "My Conference"})
        admin_view = settings_routes.get_settings(session=self.session(), user=self.admin)
        self.assertNotIn("google_drive_oauth_refresh_token", admin_view)  # secrets never returned (any role)
        self.assertEqual(admin_view["camera_label"], "Logitech C920")
        self.assertIn("storage_path", admin_view)

    def test_machine_must_use_persist_false(self):
        from app.api import recognition
        ok, buf = cv2.imencode(".jpg", np.zeros((16, 16, 3), np.uint8))
        photo = lambda: UploadFile(io.BytesIO(buf.tobytes()), filename="f.jpg")  # noqa: E731
        with patch.object(recognition, "detect_faces", side_effect=AssertionError("inferred")):
            with self.assertRaises(HTTPException) as ctx:
                recognition.recognize(BackgroundTasks(), photo=photo(), debug=None, activity_id=None,
                                      persist=True, session=self.session(), user=self.machine)
        self.assertEqual(ctx.exception.status_code, 403)
        tasks = BackgroundTasks()
        with patch.object(recognition, "detect_faces", return_value=[]):
            out = recognition.recognize(tasks, photo=photo(), debug=None, activity_id=None,
                                        persist=False, session=self.session(), user=self.machine)
        self.assertEqual((out["persisted"], tasks.tasks), (False, []))

    def test_me_reports_machine_role(self):
        from app.api import auth
        self.assertEqual(auth.me(user=self.machine)["role"], "machine")


class WebSocketAuthTests(MachineCase):
    def setUp(self):
        super().setUp()
        from app.api import nodes
        self.nodes = nodes
        p = patch.object(nodes, "db_engine", self.engine)
        p.start()
        self.addCleanup(p.stop)

    def token(self, user):
        return create_access_token(user.id, user.username)

    def test_events_feed_allows_machine_signal_does_not(self):
        self.assertTrue(self.nodes._ws_user(self.token(self.admin)))
        self.assertTrue(self.nodes._ws_user(self.token(self.admin), allow_machine=True))
        self.assertTrue(self.nodes._ws_user(self.token(self.machine), allow_machine=True))
        self.assertFalse(self.nodes._ws_user(self.token(self.machine)))

    def test_missing_invalid_or_deleted_user_is_refused(self):
        self.assertFalse(self.nodes._ws_user(None, allow_machine=True))
        self.assertFalse(self.nodes._ws_user("garbage", allow_machine=True))
        token = self.token(self.machine)
        with Session(self.engine) as s:
            s.delete(s.get(User, self.machine.id))
            s.commit()
        self.assertFalse(self.nodes._ws_user(token, allow_machine=True))


class ScriptTests(MachineCase):
    def run_script(self, *argv):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import manage_machine_user
        with patch("builtins.print"):
            return manage_machine_user.run(list(argv), engine=self.engine)

    def machine_row(self, name):
        with Session(self.engine) as s:
            return s.exec(select(User).where(User.username == name)).first()

    def test_create_reset_delete_and_admin_protection(self):
        self.assertEqual(self.run_script("create", "--username", "hub2"), 0)
        row = self.machine_row("hub2")
        self.assertEqual(row.role, "machine")
        self.assertEqual(self.run_script("create", "--username", "hub2"), 2)
        old_hash = row.password_hash
        self.assertEqual(self.run_script("reset", "--username", "hub2"), 0)
        self.assertNotEqual(self.machine_row("hub2").password_hash, old_hash)
        self.assertFalse(verify_password("x", self.machine_row("hub2").password_hash))
        self.assertEqual(self.run_script("delete", "--username", "hub2"), 0)
        self.assertIsNone(self.machine_row("hub2"))
        for action in ("create", "reset", "delete"):
            self.assertEqual(self.run_script(action, "--username", "admin"), 2)
        self.assertEqual(self.machine_row("admin").role, "admin")


if __name__ == "__main__":
    unittest.main()
