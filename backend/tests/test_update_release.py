"""Web updater readiness (2026-11-26): release manifest integrity, explicit
version confirmation, active-work refusal, and the real detached updater
(tools/updater/updater.py) run end to end against LOCAL fixtures.

No real GitHub release is used: GitHub API answers and asset downloads are
mocked, and the updater's "remote" is a local bare git repository. The
end-to-end cases start a tiny fake backend (health + version) on a free
ephemeral port, so nothing touches the Main or Dummy servers, databases or
storage.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.config import APP_VERSION  # noqa: E402
from app.models.models import ImportJob, NodeRecording  # noqa: E402
from app.services import local_video_experiment, update_service  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATER = REPO_ROOT / "tools" / "updater" / "updater.py"
MANIFEST_TOOL = REPO_ROOT / "tools" / "release" / "make_release_manifest.py"
SHA = "a" * 40
TREE = "b" * 40


class Resp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code, self._data, self.text = status_code, data, text

    def json(self):
        return self._data


def manifest_bytes(tag="v9.9.9", commit=SHA, tree=TREE, **extra):
    m = {"schema": 1, "app": "Reconize", "repo": "o/r", "tag": tag, "version": tag[1:], "commit": commit,
         "tree": tree, "created_at": "2026-11-26T00:00:00+00:00"}
    m.update(extra)
    return (json.dumps(m, indent=2) + "\n").encode()


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("GITHUB_REPO", "o/r"), ("GITHUB_TOKEN", "")):
            p = patch.object(update_service, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.files: dict[str, bytes] = {}

    def release(self, data: bytes, digest: str | None = "auto", sidecar: str | None = None, draft=False):
        assets = [{"name": "release-manifest.json", "browser_download_url": "u://manifest",
                   "digest": ("sha256:" + hashlib.sha256(data).hexdigest()) if digest == "auto" else digest}]
        self.files["u://manifest"] = data
        if sidecar is not None:
            assets.append({"name": "release-manifest.json.sha256", "browser_download_url": "u://sum"})
            self.files["u://sum"] = sidecar.encode()
        rel = {"tag_name": "v9.9.9", "draft": draft, "assets": assets}
        return [patch.object(update_service, "_api", lambda path, timeout=15: Resp(200, rel)),
                patch.object(update_service, "_download", lambda url, limit=0: self.files[url])]

    def verify(self, patches, tag="v9.9.9", sha=SHA):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return update_service.verify_release_manifest(tag, sha)

    def test_valid_manifest_with_github_digest(self):
        m = self.verify(self.release(manifest_bytes()))
        self.assertEqual((m["commit"], m["tree"]), (SHA, TREE))
        self.assertEqual(len(m["sha256"]), 64)

    def test_valid_manifest_with_sha256_sidecar_when_github_has_no_digest(self):
        data = manifest_bytes()
        m = self.verify(self.release(data, digest=None, sidecar=hashlib.sha256(data).hexdigest() + "  release-manifest.json\n"))
        self.assertEqual(m["tree"], TREE)

    def test_corrupt_asset_is_refused(self):
        data = manifest_bytes()
        good = "sha256:" + hashlib.sha256(data).hexdigest()
        with self.assertRaises(update_service.UpdateError) as ctx:
            self.verify(self.release(data[:-2] + b"X\n", digest=good))
        self.assertIn("integrity check", str(ctx.exception))

    def test_corrupt_asset_against_sidecar_is_refused(self):
        with self.assertRaises(update_service.UpdateError) as ctx:
            self.verify(self.release(manifest_bytes(), digest=None, sidecar="0" * 64))
        self.assertIn("integrity check", str(ctx.exception))

    def test_manifest_for_another_commit_is_refused(self):
        with self.assertRaises(update_service.UpdateError) as ctx:
            self.verify(self.release(manifest_bytes(commit="c" * 40)))
        self.assertIn("commit", str(ctx.exception))

    def test_manifest_for_another_version_is_refused(self):
        with self.assertRaises(update_service.UpdateError):
            self.verify(self.release(manifest_bytes(version="9.9.8")))

    def test_missing_manifest_asset_is_refused(self):
        rel = {"tag_name": "v9.9.9", "draft": False, "assets": []}
        with patch.object(update_service, "_api", lambda path, timeout=15: Resp(200, rel)):
            with self.assertRaises(update_service.UpdateError) as ctx:
                update_service.verify_release_manifest("v9.9.9", SHA)
        self.assertIn("release-manifest.json", str(ctx.exception))

    def test_release_without_any_checksum_is_refused(self):
        with self.assertRaises(update_service.UpdateError) as ctx:
            self.verify(self.release(manifest_bytes(), digest=None))
        self.assertIn("no checksum", str(ctx.exception))

    def test_draft_release_is_refused(self):
        with self.assertRaises(update_service.UpdateError):
            self.verify(self.release(manifest_bytes(), draft=True))

    def test_oversized_asset_is_refused(self):
        class Big:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, n):
                while True:
                    yield b"x" * n

        with patch.object(update_service.requests, "get", lambda *a, **k: Big()):
            with self.assertRaises(update_service.UpdateError):
                update_service._download("u://big")


class UpToDateTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        for target, name, value in ((update_service, "engine", engine), (update_service, "GITHUB_REPO", "o/r")):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def latest(self, tag):
        return patch.object(update_service, "_api", lambda path, timeout=15: Resp(200, {
            "tag_name": tag, "body": "", "html_url": "", "name": tag}))

    def test_same_version_is_up_to_date(self):
        with self.latest("v" + APP_VERSION):
            info = update_service.check_for_update(force=True)
        self.assertFalse(info["update_available"])
        self.assertEqual(info["latest_version"], APP_VERSION)
        self.assertFalse(info["no_published_release"])

    def test_newer_release_is_offered_with_both_versions(self):
        with self.latest("v99.0.0"):
            info = update_service.check_for_update(force=True)
        self.assertTrue(info["update_available"])
        self.assertEqual((info["current_version"], info["latest_version"]), (APP_VERSION, "99.0.0"))
        self.assertFalse(info["no_published_release"])

    def test_no_published_release(self):
        with patch.object(update_service, "_api", lambda path, timeout=15: Resp(404, {})):
            info = update_service.check_for_update(force=True)
        self.assertFalse(info["update_available"])
        self.assertIn("No published release", info["error"])
        self.assertTrue(info["no_published_release"])
        self.assertIsNone(info["latest_version"])

    def test_no_published_release_survives_the_cache(self):
        with patch.object(update_service, "_api", lambda path, timeout=15: Resp(404, {})):
            update_service.check_for_update(force=True)
        cached = update_service.check_for_update(force=False)
        self.assertTrue(cached["no_published_release"])
        self.assertFalse(cached["update_available"])

    def test_cache_written_before_the_flag_reads_as_no_release(self):
        legacy = {"current_version": APP_VERSION, "latest_version": None, "update_available": False,
                  "github_configured": True,
                  "error": "No published release found for 'o/r'. Check GITHUB_REPO, and that the repository "
                           "is public and has a published release."}
        with Session(update_service.engine) as session:
            update_service._set_setting(session, update_service._LATEST_CACHE_KEY, json.dumps(legacy))
        self.assertTrue(update_service.check_for_update(force=False)["no_published_release"])

    def test_cached_up_to_date_is_not_no_release(self):
        with self.latest("v" + APP_VERSION):
            update_service.check_for_update(force=True)
        cached = update_service.check_for_update(force=False)
        self.assertFalse(cached["no_published_release"])
        self.assertFalse(cached["update_available"])
        self.assertFalse(cached["check_failed"])

    # --- failed checks: "check failed" with the actionable message, never "Up to Date"

    def api(self, resp=None, exc=None):
        def fake(path, timeout=15):
            if exc:
                raise exc
            return resp
        return patch.object(update_service, "_api", fake)

    def assertFailed(self, info, message):
        self.assertTrue(info["check_failed"])
        self.assertIn(message, info["error"])
        self.assertFalse(info["update_available"])
        self.assertIsNone(info["latest_version"])
        self.assertFalse(info["no_published_release"])

    def test_network_failure_is_check_failed(self):
        import requests
        with self.api(exc=requests.ConnectionError("no route to host")):
            self.assertFailed(update_service.check_for_update(force=True), "Could not reach GitHub")

    def test_rate_limit_is_check_failed(self):
        with self.api(Resp(403, {}, text="API rate limit exceeded for 1.2.3.4")):
            self.assertFailed(update_service.check_for_update(force=True), "rate limit was reached")

    def test_refused_request_is_check_failed(self):
        with self.api(Resp(401, {})):
            self.assertFailed(update_service.check_for_update(force=True), "GitHub refused the request")

    def test_http_error_is_check_failed(self):
        with self.api(Resp(502, {})):
            self.assertFailed(update_service.check_for_update(force=True), "GitHub returned HTTP 502")

    def test_unexpected_answer_is_check_failed(self):
        class BadJson(Resp):
            def json(self):
                raise ValueError("not json")
        for resp in (BadJson(200), Resp(200, ["not", "a", "release"]), Resp(200, {"tag_name": ""})):
            with self.subTest(resp=resp._data), self.api(resp):
                self.assertFailed(update_service.check_for_update(force=True), "unexpected answer")

    def test_successful_checks_are_not_check_failed(self):
        with self.latest("v99.0.0"):
            self.assertFalse(update_service.check_for_update(force=True)["check_failed"])
        with self.api(Resp(404, {})):
            self.assertFalse(update_service.check_for_update(force=True)["check_failed"])

    def test_failure_after_cached_up_to_date_is_not_up_to_date(self):
        with self.latest("v" + APP_VERSION):
            update_service.check_for_update(force=True)
        with self.api(Resp(403, {}, text="API rate limit exceeded")):
            update_service.check_for_update(force=True)
        cached = update_service.check_for_update(force=False)   # what /api/update/status shows next
        self.assertFailed(cached, "rate limit was reached")

    def test_failure_after_cached_update_available_offers_nothing(self):
        with self.latest("v99.0.0"):
            update_service.check_for_update(force=True)
        with self.api(exc=__import__("requests").Timeout("timed out")):
            update_service.check_for_update(force=True)
        self.assertFailed(update_service.check_for_update(force=False), "Could not reach GitHub")

    def test_success_after_cached_failure_clears_check_failed(self):
        with self.api(Resp(500, {})):
            update_service.check_for_update(force=True)
        with self.latest("v" + APP_VERSION):
            update_service.check_for_update(force=True)
        cached = update_service.check_for_update(force=False)
        self.assertFalse(cached["check_failed"])
        self.assertEqual(cached["latest_version"], APP_VERSION)
        self.assertEqual(cached["error"], "")

    def test_cache_written_before_the_flag_is_not_check_failed(self):
        legacy = {"current_version": APP_VERSION, "latest_version": APP_VERSION, "update_available": False,
                  "github_configured": True, "error": ""}
        with Session(update_service.engine) as session:
            update_service._set_setting(session, update_service._LATEST_CACHE_KEY, json.dumps(legacy))
        cached = update_service.check_for_update(force=False)
        self.assertFalse(cached["check_failed"])
        self.assertFalse(cached["no_published_release"])

    def test_failed_check_still_blocks_install(self):
        """Install eligibility is unchanged: preflight refuses on a failed check."""
        with self.api(Resp(403, {}, text="rate limit")), \
                patch.object(update_service, "_updater_running", lambda: False), \
                patch.object(update_service, "_clear_stale_lock", lambda: None), \
                patch.object(update_service, "UPDATE_LOCK_FILE", Path(tempfile.mkdtemp()) / "none.lock"), \
                patch.object(update_service, "verify_remote", lambda: None), \
                patch.object(update_service.shutil, "which", lambda name: "git"):
            with self.assertRaises(update_service.UpdateError) as ctx:
                update_service.preflight()
        self.assertIn("rate limit was reached", str(ctx.exception))


class ConfirmationTests(unittest.TestCase):
    """/install only proceeds for the version the admin confirmed."""

    def setUp(self):
        self.spawned = []
        pre = {"target_tag": "v9.9.9", "target_sha": SHA, "target_version": "9.9.9", "target_tree": TREE}
        for target, name, value in (
            (update_service, "preflight", lambda tag=None, allow_downgrade=False: dict(pre)),
            (update_service, "acquire_lock", lambda pid: None),
            (update_service, "release_lock", lambda: None),
            (update_service, "_spawn_updater", lambda *a: self.spawned.append(a) or {"started": True}),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_missing_confirmation_starts_nothing(self):
        with self.assertRaises(update_service.UpdateError) as ctx:
            update_service.start_update()
        self.assertIn("Confirm the version", str(ctx.exception))
        self.assertEqual(self.spawned, [])

    def test_confirmation_of_another_version_starts_nothing(self):
        with self.assertRaises(update_service.UpdateError):
            update_service.start_update(confirm_version="9.9.8")
        self.assertEqual(self.spawned, [])

    def test_confirmed_version_starts_the_updater_with_the_manifest_tree(self):
        self.assertTrue(update_service.start_update(confirm_version="v9.9.9")["started"])
        self.assertEqual(self.spawned, [("v9.9.9", SHA, "9.9.9", TREE)])

    def test_install_route_passes_the_confirmation(self):
        from app.api import update_routes
        with self.assertRaises(Exception) as ctx:
            update_routes.install(update_routes.InstallBody(), user=None)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)
        self.assertTrue(update_routes.install(update_routes.InstallBody(confirm_version="9.9.9"), user=None)["started"])


class SpawnPayloadTests(unittest.TestCase):
    """The detached updater runs from its own runtime folder, so every path it
    is given must be absolute - .env's DATABASE_URL is usually relative to
    backend/ (Main's is ./database/app.db). Found 2026-11-26."""

    def test_relative_database_url_reaches_the_updater_as_an_absolute_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="reconize-spawn-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "backend" / "database").mkdir(parents=True)
        (tmp / "backend" / "database" / "app.db").write_bytes(b"")
        (tmp / "tools" / "updater").mkdir(parents=True)
        (tmp / "tools" / "updater" / "updater.py").write_text("# stub", encoding="utf-8")
        (tmp / "runtime").mkdir()              # start_update() creates it before spawning
        launched = []

        class Git:
            stdout = SHA + "\n"

        cwd = os.getcwd()
        os.chdir(tmp / "backend")             # the server runs from backend/
        self.addCleanup(os.chdir, cwd)
        with patch.object(update_service, "DATABASE_PATH", Path("database/app.db")), \
                patch.object(update_service, "PROJECT_ROOT", tmp), \
                patch.object(update_service, "UPDATER_RUNTIME_DIR", tmp / "runtime"), \
                patch.object(update_service, "BACKUP_DIR", tmp / "backups"), \
                patch.object(update_service, "git", lambda *a, **k: Git()), \
                patch.object(update_service, "_listen_address", lambda: ("127.0.0.1", 8000)), \
                patch.object(update_service.subprocess, "Popen",
                             lambda *a, **k: launched.append(a) or type("P", (), {"pid": 1})()):
            update_service._spawn_updater("v9.9.9", SHA, "9.9.9", TREE)
        cfg = json.loads((tmp / "runtime" / "update_config.json").read_text(encoding="utf-8"))
        for key in ("database_path", "backup_dir", "runtime_dir", "repo_root"):
            self.assertTrue(Path(cfg[key]).is_absolute(), key)
        self.assertEqual(Path(cfg["database_path"]), (tmp / "backend" / "database" / "app.db").resolve())
        self.assertEqual((cfg["target_tree"], cfg["backend_host"], cfg["backend_port"]), (TREE, "127.0.0.1", 8000))


class ActiveWorkTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.jobs: list[dict] = []
        for target, name, value in ((update_service, "engine", self.engine),
                                    (local_video_experiment, "list_jobs", lambda: list(self.jobs))):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def add(self, row):
        with Session(self.engine) as s:
            s.add(row)
            s.commit()

    def refused(self):
        with self.assertRaises(update_service.UpdateError) as ctx:
            update_service.refuse_if_work_in_flight()
        return str(ctx.exception)

    def test_idle_passes(self):
        update_service.refuse_if_work_in_flight()

    def test_running_import_is_refused(self):
        self.add(ImportJob(filename="guests.xlsx", file_type="xlsx", status="processing"))
        self.assertIn("import", self.refused())

    def test_unconfirmed_import_preview_does_not_block(self):
        """Every preview creates a 'pending' job; one nobody confirmed must
        not block updates forever."""
        self.add(ImportJob(filename="guests.xlsx", file_type="xlsx", status="pending"))
        update_service.refuse_if_work_in_flight()

    def test_active_recording_is_refused(self):
        self.add(NodeRecording(node_id="n1", node_name="Gate cam", status="recording"))
        self.assertIn("Gate cam", self.refused())

    def test_running_video_job_is_refused(self):
        self.jobs = [{"id": "j1", "filename": "hall.mp4", "status": "running"}]
        self.assertIn("hall.mp4", self.refused())

    def test_finished_work_does_not_block(self):
        self.add(ImportJob(filename="guests.xlsx", file_type="xlsx", status="completed"))
        self.add(NodeRecording(node_id="n1", status="completed"))
        self.jobs = [{"id": "j1", "filename": "hall.mp4", "status": "completed"}]
        update_service.refuse_if_work_in_flight()


# ---------------------------------------------------------------------------
# End to end: the real detached updater against a local origin
# ---------------------------------------------------------------------------

FAKE_BACKEND = r'''
import json, sqlite3, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
port, repo, db = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
version = (repo / "VERSION").read_text().strip()
if (repo / "MIGRATION").exists():                 # this version "migrates" the database at startup
    c = sqlite3.connect(db); c.execute("INSERT INTO person (name) VALUES (?)", ("migrated-" + version,)); c.commit(); c.close()
broken = (repo / "BROKEN").exists()               # this version never becomes healthy
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            code, body = (500, {"ok": False}) if broken else (200, {"ok": True})
        elif self.path == "/api/update/version":
            code, body = 200, {"version": version}
        else:
            code, body = 404, {}
        raw = json.dumps(body).encode(); self.send_response(code)
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(cmd, cwd):
    out = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"{cmd}: {out.stderr}")
    return out.stdout.strip()


@unittest.skipUnless(shutil.which("git") and shutil.which("npm"), "needs git and npm")
class UpdaterEndToEndTests(unittest.TestCase):
    """The real tools/updater/updater.py, as the backend launches it, against
    a throwaway install: local bare origin, fake backend, fixture database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="reconize-upd-"))
        origin, work = cls.tmp / "origin.git", cls.tmp / "src"
        run(["git", "init", "-q", "--bare", str(origin)], cls.tmp)
        run(["git", "init", "-q", str(work)], cls.tmp)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("core.autocrlf", "false")):
            run(["git", "config", k, v], work)
        (work / "backend").mkdir()
        (work / "backend" / "fake_backend.py").write_text(FAKE_BACKEND, encoding="utf-8")
        (work / "frontend").mkdir()
        (work / "frontend" / "package.json").write_text(json.dumps({
            "name": "fixture", "version": "0.0.0", "private": True,
            "scripts": {"build": "node -e \"require('fs').writeFileSync('built.txt', 'ok')\""}}), encoding="utf-8")
        (work / ".gitignore").write_text(".env\ndatabase/\nstorage/\nfrontend/built.txt\nbackend/server_*.log\n", encoding="utf-8")
        cls.sha = {}

        def commit(version, *, migration=False, broken=False):
            (work / "VERSION").write_text(version + "\n", encoding="utf-8")
            for name, on in (("MIGRATION", migration), ("BROKEN", broken)):
                if on:
                    (work / name).write_text("1", encoding="utf-8")
                elif (work / name).exists():
                    (work / name).unlink()
            run(["git", "add", "-A"], work)
            run(["git", "commit", "-q", "-m", version], work)
            run(["git", "tag", "v" + version], work)
            cls.sha[version] = run(["git", "rev-parse", "HEAD"], work)

        commit("1.0.0")
        commit("1.0.1", migration=True)
        commit("1.0.2", migration=True, broken=True)
        run(["git", "push", "-q", str(origin), "--all"], work)
        run(["git", "push", "-q", str(origin), "--tags"], work)
        cls.origin = origin

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        self.repo = self.case / "install"
        run(["git", "clone", "-q", str(self.origin), str(self.repo)], self.case)
        run(["git", "config", "core.autocrlf", "false"], self.repo)
        run(["git", "checkout", "-q", "--detach", self.sha["1.0.0"]], self.repo)
        # Local state the update must preserve.
        (self.repo / ".env").write_text("JWT_SECRET=keep-this\n", encoding="utf-8")
        (self.repo / "storage").mkdir()
        (self.repo / "storage" / "photo.jpg").write_bytes(b"\xff\xd8keep\xff\xd9")
        (self.repo / "database").mkdir()
        self.db = self.repo / "database" / "app.db"
        c = sqlite3.connect(self.db)
        for t in ("person", "attendance", "consentrecord", "facedetection"):
            c.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE updatejob (id TEXT, from_version TEXT, to_version TEXT, target_sha TEXT, previous_sha TEXT, "
                  "status TEXT, error TEXT, started_at TEXT, finished_at TEXT)")
        c.execute("INSERT INTO person (name) VALUES ('Ann')")
        c.commit()
        c.close()
        self.port = free_port()
        self.start_cmd = [sys.executable, str(self.repo / "backend" / "fake_backend.py"), str(self.port), str(self.repo), str(self.db)]
        self.server = subprocess.Popen(self.start_cmd, cwd=str(self.repo / "backend"))
        self.addCleanup(self.kill_port)
        deadline = time.time() + 10
        while time.time() < deadline and not self.answering():
            time.sleep(0.1)

    def answering(self):
        with socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def kill_port(self):
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{self.port}") and "LISTEN" in parts[3].upper():
                subprocess.run(["taskkill", "/PID", parts[-1], "/F", "/T"], capture_output=True)

    def update(self, version, tree=None):
        runtime = self.case / "runtime"
        runtime.mkdir(exist_ok=True)
        cfg = {
            "repo_root": str(self.repo), "python": sys.executable, "target_tag": "v" + version,
            "target_sha": self.sha[version], "target_version": version, "from_version": "1.0.0",
            "previous_sha": self.sha["1.0.0"], "database_path": str(self.db), "backup_dir": str(self.case / "backups"),
            "runtime_dir": str(runtime), "github_repo": "o/r",
            "target_tree": tree or run(["git", "rev-parse", f"v{version}^{{tree}}"], self.repo.parent.parent / "src"),
            "backend_host": "127.0.0.1", "backend_port": self.port, "backend_start_cmd": self.start_cmd,
            "health_timeout": 12,
        }
        (runtime / "update_config.json").write_text(json.dumps(cfg), encoding="utf-8")
        shutil.copy2(UPDATER, runtime / "updater.py")
        proc = subprocess.run([sys.executable, str(runtime / "updater.py"), str(runtime / "update_config.json")],
                              cwd=str(runtime), capture_output=True, text=True, timeout=300)
        state = json.loads((runtime / "update_state.json").read_text(encoding="utf-8"))
        log = (runtime / "update.log").read_text(encoding="utf-8")
        return proc.returncode, state, log

    def head(self):
        return run(["git", "rev-parse", "HEAD"], self.repo)

    def people(self):
        c = sqlite3.connect(self.db)
        try:
            return [r[0] for r in c.execute("SELECT name FROM person ORDER BY id")]
        finally:
            c.close()

    def history(self):
        c = sqlite3.connect(self.db)
        try:
            return [r[0] for r in c.execute("SELECT status FROM updatejob")]
        finally:
            c.close()

    def reported_version(self):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/update/version", timeout=5) as r:
            return json.loads(r.read())["version"]

    def assert_local_state_preserved(self):
        self.assertEqual((self.repo / ".env").read_text(encoding="utf-8"), "JWT_SECRET=keep-this\n")
        self.assertEqual((self.repo / "storage" / "photo.jpg").read_bytes(), b"\xff\xd8keep\xff\xd9")

    def test_successful_update(self):
        rc, state, log = self.update("1.0.1")
        self.assertEqual(rc, 0, log)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(self.head(), self.sha["1.0.1"])
        self.assertEqual(self.reported_version(), "1.0.1")
        self.assertIn("integrity verified", log)
        self.assertIn("(manifest)", log)
        self.assertEqual(self.people(), ["Ann", "migrated-1.0.1"])
        self.assert_local_state_preserved()
        backup = Path(state["backup_path"])
        self.assertTrue(backup.exists())
        self.assertTrue((backup.parent / ".env").exists(), "the .env is backed up with the database")
        c = sqlite3.connect(backup)
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual([r[0] for r in c.execute("SELECT name FROM person")], ["Ann"])
        c.close()
        self.assertEqual(self.history(), ["completed"])

    def test_failed_startup_rolls_back_code_and_database(self):
        rc, state, log = self.update("1.0.2")          # its backend never becomes healthy
        self.assertEqual(rc, 1, log)
        self.assertEqual(state["status"], "rolled_back")
        self.assertTrue(state["database_restored"], log)
        self.assertTrue(state["previous_version_healthy"], log)
        self.assertIn("did not become healthy", state["error"])
        self.assertEqual(self.head(), self.sha["1.0.0"])
        self.assertEqual(self.reported_version(), "1.0.0")
        self.assertEqual(self.people(), ["Ann"], "the half-migrated database was replaced by the verified backup")
        self.assert_local_state_preserved()
        self.assertEqual(self.history(), ["rolled_back"])

    def test_tree_that_differs_from_the_manifest_is_refused_before_restart(self):
        rc, state, log = self.update("1.0.1", tree="0" * 40)
        self.assertEqual(rc, 1, log)
        self.assertEqual(state["status"], "rolled_back")
        self.assertIn("does not match the release manifest", state["error"])
        self.assertFalse(state.get("reached_restart"))
        self.assertEqual(self.head(), self.sha["1.0.0"])
        self.assertEqual(self.people(), ["Ann"])
        self.assert_local_state_preserved()



class ReleaseToolTests(unittest.TestCase):
    """make_release_manifest.py on a fixture repository."""

    def test_manifest_matches_tag_and_is_accepted_by_verification(self):
        tmp = Path(tempfile.mkdtemp(prefix="reconize-rel-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        (repo / "tools" / "release").mkdir(parents=True)
        shutil.copy2(MANIFEST_TOOL, repo / "tools" / "release" / "make_release_manifest.py")
        run(["git", "init", "-q", str(repo)], tmp)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            run(["git", "config", k, v], repo)
        (repo / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        run(["git", "add", "-A"], repo)
        run(["git", "commit", "-q", "-m", "r"], repo)
        run(["git", "tag", "v2.0.0"], repo)
        out = subprocess.run([sys.executable, str(repo / "tools" / "release" / "make_release_manifest.py"), "v2.0.0",
                              "--out", str(tmp / "out"), "--repo", "o/r"], cwd=str(repo), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        data = (tmp / "out" / "release-manifest.json").read_bytes()
        side = (tmp / "out" / "release-manifest.json.sha256").read_text(encoding="utf-8")
        self.assertEqual(side.split()[0], hashlib.sha256(data).hexdigest())
        m = json.loads(data)
        self.assertEqual((m["tag"], m["version"], m["commit"], m["tree"]),
                         ("v2.0.0", "2.0.0", run(["git", "rev-parse", "HEAD"], repo),
                          run(["git", "rev-parse", "HEAD^{tree}"], repo)))
        # ... and the backend's verification accepts it for that tag/commit.
        files = {"u://m": data, "u://s": side.encode()}
        rel = {"draft": False, "assets": [{"name": "release-manifest.json", "browser_download_url": "u://m"},
                                          {"name": "release-manifest.json.sha256", "browser_download_url": "u://s"}]}
        with patch.object(update_service, "GITHUB_REPO", "o/r"), \
                patch.object(update_service, "_api", lambda path, timeout=15: Resp(200, rel)), \
                patch.object(update_service, "_download", lambda url, limit=0: files[url]):
            got = update_service.verify_release_manifest("v2.0.0", m["commit"])
        self.assertEqual(got["tree"], m["tree"])

    def test_version_file_mismatch_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="reconize-rel-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        (repo / "tools" / "release").mkdir(parents=True)
        shutil.copy2(MANIFEST_TOOL, repo / "tools" / "release" / "make_release_manifest.py")
        run(["git", "init", "-q", str(repo)], tmp)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            run(["git", "config", k, v], repo)
        (repo / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        run(["git", "add", "-A"], repo)
        run(["git", "commit", "-q", "-m", "r"], repo)
        run(["git", "tag", "v2.0.1"], repo)
        out = subprocess.run([sys.executable, str(repo / "tools" / "release" / "make_release_manifest.py"), "v2.0.1",
                              "--out", str(tmp / "out"), "--repo", "o/r"], cwd=str(repo), capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("VERSION", out.stderr + out.stdout)


if __name__ == "__main__":
    unittest.main()
