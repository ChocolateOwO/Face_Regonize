"""Hub camera relay (services/hub_camera.py, api/hub_cameras.py) against a
local MJPEG fixture that speaks the Hub's framing: valid stream, dropped
connection, busy camera, stall, invalid frames, redirect, no Hub, and the
HTTP route with human / machine tokens. No real Hub, camera or network."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import hub_camera, hub_link  # noqa: E402


def jpeg(value: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((24, 32, 3), value, np.uint8))
    assert ok
    return buf.tobytes()


FRAMES = [jpeg(v) for v in (10, 80, 160, 240)]


def part(data: bytes, length: bool = True) -> bytes:
    head = b"\r\n--mice\r\nContent-Type: image/jpeg\r\n"
    if length:
        head += b"Content-Length: " + str(len(data)).encode() + b"\r\n"
    return head + b"\r\n" + data


class FakeHub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    hits: list[str] = []

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        FakeHub.hits.append(self.path)
        if self.path.startswith("/api/cameras"):
            body = json.dumps([
                {"dev": "pc:Desk Cam", "name": "Desk Cam", "kind": "usb", "viewers": 1, "secret": "x"},
                {"dev": "cam:7", "name": "Door", "kind": "rtsp", "live": False},
                {"dev": "file:C:/x", "name": "not a hub camera id"},
            ]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        dev = self.path.split("dev=", 1)[-1]
        if dev.endswith("Busy"):
            self.send_response(503)
            self.send_header("Retry-After", "4")
            self.end_headers()
            self.wfile.write(b'{"ok": false, "busy": true}')
            return
        if dev.endswith("Redirect"):
            self.send_response(302)
            self.send_header("Location", "http://example.com/x.mjpg")
            self.end_headers()
            return
        if dev.endswith("Html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=mice")
        self.end_headers()
        try:
            if dev.endswith("Good"):
                for f in FRAMES:
                    self.wfile.write(part(f))
                    self.wfile.flush()
            elif dev.endswith("NoLength"):
                for f in FRAMES:
                    self.wfile.write(part(f, length=False))
            elif dev.endswith("Invalid"):
                self.wfile.write(part(FRAMES[0]))
                self.wfile.write(part(b"not a jpeg at all"))
                self.wfile.write(part(b"\xff\xd8" + b"\x00" * 64))   # starts like a JPEG, never ends
                self.wfile.write(part(FRAMES[1]))
            elif dev.endswith("Drop"):
                self.wfile.write(part(FRAMES[0]))
                self.wfile.write(part(FRAMES[1])[: len(FRAMES[1]) // 2])
            elif dev.endswith("Stall"):
                self.wfile.write(part(FRAMES[0]))
                self.wfile.flush()
                time.sleep(2.5)
        except OSError:
            pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HubLinkTests(unittest.TestCase):
    def test_only_loopback_http_with_port(self):
        good = {"http://127.0.0.1:8642": "http://127.0.0.1:8642", "http://localhost:8642/": "http://localhost:8642",
                "http://[::1]:8642": "http://[::1]:8642"}
        for raw, want in good.items():
            self.assertEqual(hub_link.parse(raw)[0], want, raw)
        for raw in ("", "https://127.0.0.1:8642", "http://192.168.1.5:8642", "http://127.0.0.1",
                    "http://user:pw@127.0.0.1:8642", "http://127.0.0.1:8642/api", "http://127.0.0.1:8642?x=1",
                    "ftp://127.0.0.1:21", "http://evil.example:8642", "http://127.0.0.1.nip.io:8642", "garbage"):
            self.assertIsNone(hub_link.parse(raw)[0], raw)


class ParserTests(unittest.TestCase):
    def test_byte_by_byte_with_lengths(self):
        p = hub_camera.MjpegParser()
        data = b"".join(part(f) for f in FRAMES)
        out = []
        for i in range(len(data)):
            out += p.feed(data[i:i + 1])
        self.assertEqual(out, FRAMES)

    def test_markers_without_lengths(self):
        p = hub_camera.MjpegParser()
        self.assertEqual(p.feed(b"".join(part(f, length=False) for f in FRAMES)), FRAMES)

    def test_oversize_and_garbage_are_dropped_and_bounded(self):
        p = hub_camera.MjpegParser(max_frame=len(FRAMES[0]) + 10, max_buffer=4096)
        big = b"\xff\xd8" + b"\x01" * 5000 + b"\xff\xd9"
        out = p.feed(part(big) + part(FRAMES[0]))
        self.assertEqual(out, [FRAMES[0]])
        self.assertGreaterEqual(p.invalid, 1)
        p.feed(b"\x00" * 10000)  # no frame anywhere
        self.assertLessEqual(len(p.buf), 4096)


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), FakeHub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        p = patch.dict(os.environ, {"MICE_HUB_URL": f"http://127.0.0.1:{self.port}"})
        p.start()
        self.addCleanup(p.stop)
        FakeHub.hits.clear()

    def read(self, dev):
        return list(hub_camera.frames(hub_camera.open_stream(dev)))

    def test_valid_stream(self):
        self.assertEqual(self.read("pc:Good"), FRAMES)
        self.assertTrue(FakeHub.hits[0].startswith("/api/dev/cam.stream?dev=pc%3AGood"))

    def test_plain_mjpeg_without_lengths(self):
        self.assertEqual(self.read("pc:NoLength"), FRAMES)

    def test_invalid_frames_are_skipped(self):
        self.assertEqual(self.read("pc:Invalid"), [FRAMES[0], FRAMES[1]])

    def test_dropped_connection_ends_after_the_last_complete_frame(self):
        self.assertEqual(self.read("pc:Drop"), [FRAMES[0]])

    def test_stalled_hub_ends_within_the_read_timeout(self):
        with patch.object(hub_camera, "READ_TIMEOUT", 0.5):
            began = time.monotonic()
            self.assertEqual(self.read("pc:Stall"), [FRAMES[0]])
            self.assertLess(time.monotonic() - began, 2.0)

    def test_busy_camera_is_503_with_retry_after(self):
        with self.assertRaises(hub_camera.HubCameraError) as ctx:
            hub_camera.open_stream("pc:Busy")
        self.assertEqual((ctx.exception.status, ctx.exception.retry_after), (503, 4))

    def test_redirect_is_not_followed(self):
        with self.assertRaises(hub_camera.HubCameraError) as ctx:
            hub_camera.open_stream("pc:Redirect")
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(len(FakeHub.hits), 1)

    def test_not_a_stream(self):
        with self.assertRaises(hub_camera.HubCameraError):
            hub_camera.open_stream("pc:Html")

    def test_bad_camera_id_never_reaches_the_hub(self):
        for dev in ("", "http://evil/x", "pc:", "pc:a\nb", "file:C:/x", "../api/dev/cmd"):
            with self.assertRaises(hub_camera.HubCameraError) as ctx:
                hub_camera.open_stream(dev)
            self.assertEqual(ctx.exception.status, 400, dev)
        self.assertEqual(FakeHub.hits, [])

    def test_unavailable_hub(self):
        with patch.dict(os.environ, {"MICE_HUB_URL": f"http://127.0.0.1:{free_port()}"}):
            with self.assertRaises(hub_camera.HubCameraError) as ctx:
                hub_camera.open_stream("pc:Good")
            self.assertEqual(ctx.exception.status, 503)
            with self.assertRaises(hub_camera.HubCameraError):
                hub_camera.list_cameras()

    def test_no_hub_configured(self):
        with patch.dict(os.environ, {"MICE_HUB_URL": ""}):
            with self.assertRaises(hub_camera.HubCameraError) as ctx:
                hub_camera.open_stream("pc:Good")
            self.assertEqual(ctx.exception.status, 404)

    def test_camera_list_keeps_only_hub_ids_and_known_fields(self):
        cams = hub_camera.list_cameras()
        self.assertEqual(cams, [{"dev": "pc:Desk Cam", "name": "Desk Cam", "kind": "usb", "live": True},
                                {"dev": "cam:7", "name": "Door", "kind": "rtsp", "live": False}])


class RouteTests(unittest.TestCase):
    """The HTTP relay over ASGI: a signed-in human gets the frames; a machine
    account, no token and no Hub are refused."""

    @classmethod
    def setUpClass(cls):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import Session, SQLModel, create_engine

        from app.auth.security import create_access_token, hash_password
        from app.database.db import get_session
        from app.main import app
        from app.models.models import User

        cls.port = free_port()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), FakeHub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            station = User(username="station-a", password_hash=hash_password("x" * 12), role="station")
            robot = User(username="robot-hub", password_hash=hash_password("y" * 12), role="machine")
            s.add(station)
            s.add(robot)
            s.commit()
            cls.human = create_access_token(station.id, station.username)
            cls.machine = create_access_token(robot.id, robot.username)

        def session_override():
            with Session(engine) as s:
                yield s

        app.dependency_overrides[get_session] = session_override
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        from app.database.db import get_session

        cls.app.dependency_overrides.pop(get_session, None)
        cls.server.shutdown()

    def get(self, path, token):
        """One GET over ASGI. Like a real server, receive() blocks after the
        (empty) request body until the response is done, so a streaming
        response's disconnect watcher sees no spurious messages."""
        import asyncio

        async def run():
            sent, first = [], [True]
            done = asyncio.Event()

            async def receive():
                if first[0]:
                    first[0] = False
                    return {"type": "http.request", "body": b"", "more_body": False}
                await done.wait()
                return {"type": "http.disconnect"}

            async def send(event):
                sent.append(event)
                if event["type"] == "http.response.body" and not event.get("more_body"):
                    done.set()

            route, _, query = path.partition("?")
            headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                     "method": "GET", "scheme": "http", "path": route, "raw_path": route.encode(),
                     "query_string": query.encode(), "headers": headers, "client": ("127.0.0.1", 1),
                     "server": ("test", 80), "root_path": ""}
            await self.app(scope, receive, send)
            start = next(e for e in sent if e["type"] == "http.response.start")
            body = b"".join(e.get("body", b"") for e in sent if e["type"] == "http.response.body")
            return start["status"], body, {k.decode().lower(): v.decode() for k, v in start["headers"]}

        return asyncio.run(run())

    def test_human_gets_relayed_frames(self):
        with patch.dict(os.environ, {"MICE_HUB_URL": f"http://127.0.0.1:{self.port}"}):
            status, body, head = self.get("/api/hub/cam.stream?dev=pc:Good", self.human)
            self.assertEqual(status, 200)
            self.assertTrue(head["content-type"].startswith("multipart/x-mixed-replace"))
            self.assertEqual(hub_camera.MjpegParser().feed(body), FRAMES)
            status, body, _ = self.get("/api/hub/cameras", self.human)
            cams = json.loads(body)
            self.assertEqual((status, cams["enabled"], len(cams["cameras"])), (200, True, 2))

    def test_machine_and_anonymous_are_refused(self):
        with patch.dict(os.environ, {"MICE_HUB_URL": f"http://127.0.0.1:{self.port}"}):
            self.assertEqual(self.get("/api/hub/cam.stream?dev=pc:Good", self.machine)[0], 403)
            self.assertEqual(self.get("/api/hub/cameras", self.machine)[0], 403)
            self.assertEqual(self.get("/api/hub/cam.stream?dev=pc:Good", None)[0], 401)

    def test_busy_and_unconfigured_answers(self):
        with patch.dict(os.environ, {"MICE_HUB_URL": f"http://127.0.0.1:{self.port}"}):
            status, _, head = self.get("/api/hub/cam.stream?dev=pc:Busy", self.human)
            self.assertEqual((status, head.get("retry-after")), (503, "4"))
        with patch.dict(os.environ, {"MICE_HUB_URL": ""}):
            self.assertEqual(self.get("/api/hub/cam.stream?dev=pc:Good", self.human)[0], 404)
            self.assertEqual(json.loads(self.get("/api/hub/cameras", self.human)[1])["enabled"], False)


if __name__ == "__main__":
    unittest.main()
