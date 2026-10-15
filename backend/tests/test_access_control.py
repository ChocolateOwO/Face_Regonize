"""Authorization regressions for the three pre-existing access-control gaps:

1. GET /api/files/{path} served stored (personal) images with no sign-in.
   Now: file-read cookie (set at login / file-session) or a Bearer token.
2. Data-changing routes accepted any signed-in role. Now: writes are
   admin-only unless a route is an explicit station operation.
3. GET /api/settings returned every stored Setting row, including the Google
   Drive OAuth refresh token. Now: only the user-editable keys.

Preview behaviour (paths, cache headers, thumbnail generation) is pinned too.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session, SQLModel, create_engine
from starlette.requests import Request

from app.api import auth as auth_api
from app.api import files, pdpa, settings_routes
from app.auth import deps
from app.auth.security import FILE_COOKIE, create_access_token, create_file_token
from app.models.models import Person, Setting, User


def make_request(method="GET", cookies: dict | None = None, scheme="http"):
    headers = []
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "method": method, "path": "/", "headers": headers,
                    "query_string": b"", "scheme": scheme, "server": ("test", 80)})


def bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-access-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.engine = create_engine(f"sqlite:///{(root / 't.db').as_posix()}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        self.users = {}
        for role in ("admin", "station", "machine"):
            with Session(self.engine) as s:
                u = User(username=role, password_hash="x", role=role)
                s.add(u)
                s.commit()
                s.refresh(u)
                s.expunge(u)  # a detached snapshot: id/username/role stay readable
            self.users[role] = u
        self.storage = root / "storage"
        (self.storage / "people" / "0001").mkdir(parents=True)
        (self.storage / "people" / "0001" / "profile.jpg").write_bytes(b"\xff\xd8\xffJPEG")
        p = patch.object(files, "STORAGE_PATH", self.storage)
        p.start()
        self.addCleanup(p.stop)

    def session(self):
        s = Session(self.engine)
        self.addCleanup(s.close)
        return s

    def token(self, role):
        u = self.users[role]
        return create_access_token(u.id, u.username)

    def file_token(self, role, minutes=60):
        return create_file_token(self.users[role].id, datetime.now(timezone.utc) + timedelta(minutes=minutes))

    def read(self, path="people/0001/profile.jpg", cookies=None, creds=None):
        s = self.session()
        user = files.file_reader(make_request(cookies=cookies), creds, s)
        return files.get_file(path, user=user)


class StoredFileTests(Case):
    def test_anonymous_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            self.read()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_file_cookie_reads_with_unchanged_cache_headers(self):
        for role in ("admin", "station"):
            res = self.read(cookies={FILE_COOKIE: self.file_token(role)})
            self.assertEqual(Path(res.path).read_bytes(), b"\xff\xd8\xffJPEG")
            self.assertEqual(res.headers["cache-control"], "no-cache, must-revalidate")

    def test_bearer_token_also_reads(self):
        res = self.read(creds=bearer(self.token("admin")))
        self.assertTrue(Path(res.path).is_file())

    def test_rejected_cookies(self):
        cases = {
            "expired": self.file_token("admin", minutes=-1),
            "session token in cookie (wrong scope)": self.token("admin"),
            "garbage": "not-a-token",
        }
        for label, value in cases.items():
            with self.assertRaises(HTTPException, msg=label) as ctx:
                self.read(cookies={FILE_COOKIE: value})
            self.assertEqual(ctx.exception.status_code, 401, label)

    def test_machine_cannot_read_files(self):
        for kwargs in ({"cookies": {FILE_COOKIE: self.file_token("machine")}},
                       {"creds": bearer(self.token("machine"))}):
            with self.assertRaises(HTTPException) as ctx:
                self.read(**kwargs)
            self.assertEqual(ctx.exception.status_code, 403)

    def test_deleted_user_cookie_is_refused(self):
        cookie = self.file_token("station")
        with Session(self.engine) as s:
            s.delete(s.get(User, self.users["station"].id))
            s.commit()
        with self.assertRaises(HTTPException) as ctx:
            self.read(cookies={FILE_COOKIE: cookie})
        self.assertEqual(ctx.exception.status_code, 401)

    def test_file_token_is_not_an_api_token(self):
        ft = self.file_token("admin")
        for dep in (lambda c, s: deps.get_current_user(make_request(), c, s), deps.get_api_client,
                    deps.get_station_user):
            with self.assertRaises(HTTPException) as ctx:
                dep(bearer(ft), self.session())
            self.assertEqual(ctx.exception.status_code, 401)
        self.assertIsNone(deps.token_user(ft, self.session()))  # WebSockets too

    def test_path_confinement_and_missing_file_unchanged(self):
        cookie = {FILE_COOKIE: self.file_token("admin")}
        for path, code in (("../outside.txt", 400), ("people/0001/missing.jpg", 404)):
            with self.assertRaises(HTTPException) as ctx:
                self.read(path, cookies=cookie)
            self.assertEqual(ctx.exception.status_code, code)

    def test_thumbnail_keeps_immutable_cache(self):
        with patch.object(files, "is_thumbnail_path", return_value=True):
            res = self.read(cookies={FILE_COOKIE: self.file_token("admin")})
        self.assertEqual(res.headers["cache-control"], "private, max-age=31536000, immutable")


class FileCookieIssueTests(Case):
    def cookie_header(self, response: Response) -> str:
        return response.headers.get("set-cookie", "")

    def test_login_sets_scoped_httponly_cookie_for_humans_only(self):
        from app.auth.security import hash_password
        with Session(self.engine) as s:
            for role in ("admin", "machine"):
                u = s.get(User, self.users[role].id)
                u.password_hash = hash_password("pw")
                s.add(u)
            s.commit()
        response = Response()
        auth_api.login(auth_api.LoginRequest(username="admin", password="pw"), make_request("POST"), response,
                       session=self.session())
        header = self.cookie_header(response)
        self.assertIn(f"{FILE_COOKIE}=", header)
        for part in ("HttpOnly", "Path=/api/files", "SameSite=strict"):
            self.assertIn(part.lower(), header.lower())
        self.assertNotIn("secure", header.lower())  # http localhost
        max_age = int(header.lower().split("max-age=")[1].split(";")[0])
        self.assertTrue(480 * 60 - 60 <= max_age <= 480 * 60)

        machine = Response()
        auth_api.login(auth_api.LoginRequest(username="machine", password="pw"), make_request("POST"), machine,
                       session=self.session())
        self.assertEqual(self.cookie_header(machine), "")

    def test_file_session_reissues_and_https_marks_secure(self):
        response = Response()
        token = self.token("station")
        auth_api.start_file_session(make_request("POST", scheme="https"), response, bearer(token),
                                    user=self.users["station"])
        header = self.cookie_header(response).lower()
        self.assertIn(FILE_COOKIE, header)
        self.assertIn("secure", header)
        cookie = self.cookie_header(response).split(";")[0].split("=", 1)[1]
        self.assertTrue(Path(self.read(cookies={FILE_COOKIE: cookie}).path).is_file())

    def test_sign_out_clears_cookie(self):
        response = Response()
        auth_api.end_file_session(response)
        header = self.cookie_header(response).lower()
        self.assertIn(f"{FILE_COOKIE}=", header)
        self.assertIn("max-age=0", header)
        self.assertIn("path=/api/files", header)


class WriteAuthorizationTests(Case):
    def test_non_admin_reads_but_cannot_write_through_get_current_user(self):
        station = bearer(self.token("station"))
        self.assertEqual(deps.get_current_user(make_request("GET"), station, self.session()).role, "station")
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(HTTPException) as ctx:
                deps.get_current_user(make_request(method), station, self.session())
            self.assertEqual(ctx.exception.status_code, 403, method)
            self.assertEqual(deps.get_current_user(make_request(method), bearer(self.token("admin")),
                                                   self.session()).role, "admin")

    def test_station_dependency_allows_humans_not_machines(self):
        self.assertEqual(deps.get_station_user(bearer(self.token("station")), self.session()).role, "station")
        with self.assertRaises(HTTPException) as ctx:
            deps.get_station_user(bearer(self.token("machine")), self.session())
        self.assertEqual(ctx.exception.status_code, 403)

    def test_station_may_record_kiosk_consent_but_not_admin_override(self):
        with Session(self.engine) as s:
            p = Person(participant_id="0001", first_name="Ann", image_path="", embedding=b"")
            s.add(p)
            s.commit()
            s.refresh(p)
        station = self.users["station"]
        out = pdpa.record_consent(pdpa.RecordConsentBody(person_ids=[p.id], choice="declined", source="kiosk"),
                                  session=self.session(), user=station)
        self.assertEqual(out, {"recorded": 1})
        with self.assertRaises(HTTPException) as ctx:
            pdpa.record_consent(pdpa.RecordConsentBody(person_ids=[p.id], choice="consented", source="admin"),
                                session=self.session(), user=station)
        self.assertEqual(ctx.exception.status_code, 403)
        out = pdpa.record_consent(pdpa.RecordConsentBody(person_ids=[p.id], choice="consented", source="admin"),
                                  session=self.session(), user=self.users["admin"])
        self.assertEqual(out, {"recorded": 1})


class SettingsExposureTests(Case):
    def test_secrets_and_internal_state_never_returned(self):
        with Session(self.engine) as s:
            for key, value in {"google_drive_oauth_refresh_token": "SECRET", "google_drive_oauth_pending_state": "S2",
                               "google_drive_oauth_email": "x@y", "participant_id_high_water": "42",
                               "event_name": "Stored Event", "camera_label": "C920"}.items():
                s.add(Setting(key=key, value=value))
            s.commit()
        for role in ("admin", "station"):
            out = settings_routes.get_settings(session=self.session(), user=self.users[role])
            self.assertEqual(set(out), set(settings_routes.DEFAULTS) | {"storage_path"}, role)
            self.assertEqual((out["event_name"], out["camera_label"]), ("Stored Event", "C920"))
            self.assertNotIn("SECRET", str(out))


if __name__ == "__main__":
    unittest.main()
