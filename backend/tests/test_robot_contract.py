"""The robot hub's contract, end to end through the real application.

Every API the robot hub reads (the PDF "API ที่ขอจาก Face_Regonize", 2026-10-09)
is called over ASGI with a MACHINE account's bearer token, and the exact
fields the hub reads are checked. Also: a machine cannot write or record,
persist=false saves nothing, central recognition publishes one event with the
real node id, and a machine on the opt-in feed sees an unknown person at its
camera without any identity being stored.

Temporary database and storage; detection is faked, everything else is real.
"""
from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import WebSocketDisconnect
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import look as look_api, nodes, recognition
from app.auth.security import hash_password
from app.database.db import get_session
from app.face_recognition.engine import DetectedFace
from app.face_recognition.index import RecognitionIndex
from app.main import app
from app.models.models import Attendance, CameraNode, FaceDetection, Person, Setting, Upload, User
from app.services import events_feed, look_service, node_registry, presence_service, settings_cache, storage_service
from test_local_video_experiment import request

ROBOT_PASSWORD = "robot-test-password"
STATION_PASSWORD = "station-test-password"
FEED_FIELDS = {"node_id", "participant_id", "name", "activity_id", "checkin", "at"}   # the six the hub reads (PDF 1.2)


def unit(i: int) -> np.ndarray:
    v = np.zeros(512, np.float32)
    v[i] = 1.0
    return v


def jpeg(w: int = 640, h: int = 480) -> bytes:
    return cv2.imencode(".jpg", np.full((h, w, 3), 100, np.uint8))[1].tobytes()


def multipart(fields: dict) -> tuple[bytes, str]:
    boundary = "----robotcontract"
    parts = []
    for name, value in fields.items():
        if isinstance(value, bytes):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="f.jpg"\r\n'
                         f"Content-Type: image/jpeg\r\n\r\n".encode() + value + b"\r\n")
        else:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class ContractCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-robot-contract-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db_path = root / "t.db"
        self.engine = create_engine(f"sqlite:///{self.db_path.as_posix()}", connect_args={"check_same_thread": False, "timeout": 30})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            s.add(User(username="robot-hub", password_hash=hash_password(ROBOT_PASSWORD), role="machine"))
            s.add(User(username="station", password_hash=hash_password(STATION_PASSWORD), role="admin"))
            ann = Person(participant_id="9001", first_name="Ann", last_name="Alpha", image_path="", embedding=unit(0).tobytes(), call_as="พี่แอน")
            ben = Person(participant_id="9002", first_name="สมชาย", last_name="ใจดี", image_path="", embedding=unit(1).tobytes())
            s.add(ann)
            s.add(ben)
            s.add(Setting(key="camera_label", value="Logitech C920"))
            s.add(Setting(key="google_drive_oauth_refresh_token", value="SECRET-TOKEN"))
            s.add(CameraNode(node_id="CAM-02", display_name="Front", camera_label="C920", inference_mode="local"))
            s.add(CameraNode(node_id="MOBILE-01", display_name="Tablet", camera_label="iPad", inference_mode="central"))
            s.commit()
            s.refresh(ann)
            s.refresh(ben)
            self.ann, self.ben = ann, ben
        # One history row from before provenance/seq existed: an Upload without
        # source columns, and a detection written past the ORM's seq listener.
        with Session(self.engine) as s:
            s.add(Upload(id="old-upload", filename="kiosk_scan.jpg", image_path="", uploaded_at=datetime(2026, 9, 1)))
            s.commit()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("INSERT INTO facedetection (id, upload_id, person_id, confidence, bbox, detected_at, status) "
                         "VALUES ('old-row', 'old-upload', ?, 0.7, '0,0,1,1', ?, 'matched')", (self.ann.id, datetime(2026, 9, 1).isoformat(" ")))
            conn.commit()
        finally:
            conn.close()

        storage = root / "storage"
        (storage / "thumbnails").mkdir(parents=True)
        self.storage = storage
        index = RecognitionIndex()
        index.rebuild([SimpleNamespace(id=p.id, first_name=p.first_name, last_name=p.last_name, participant_id=p.participant_id,
                                       embedding=p.embedding) for p in (self.ann, self.ben)])
        self.faces = [DetectedFace(unit(0), (100.0, 80.0, 300.0, 330.0), 0.95), DetectedFace(unit(300), (400.0, 50.0, 460.0, 120.0), 0.8)]
        detect = lambda img, timings=None, lock_timeout=None: list(self.faces)  # noqa: E731
        for target, name, value in [
            (recognition, "recognition_index", index), (look_service, "recognition_index", index),
            (recognition, "engine", self.engine), (nodes, "db_engine", self.engine), (look_service, "db_engine", self.engine),
            (recognition, "detect_faces", detect), (look_service.engine, "detect_faces", detect),
            (look_service.engine, "is_ready", lambda: True),
            (storage_service, "STORAGE_PATH", storage), (storage_service, "EVENTS_DIR", storage / "events"),
            (storage_service, "THUMBNAILS_DIR", storage / "thumbnails"),
        ]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

        def session_override():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = session_override
        self.addCleanup(app.dependency_overrides.pop, get_session, None)
        settings_cache.set_threshold(0.45)
        settings_cache.set_debug_mode(False)
        node_registry._nodes.clear()
        self.addCleanup(node_registry._nodes.clear)
        presence_service.reset()
        self.addCleanup(presence_service.reset)
        for target in (events_feed._subscribers, events_feed._presence_subscribers):
            target.clear()
            self.addCleanup(target.clear)

    # --- helpers -------------------------------------------------------------
    def call(self, method, path, token=None, body=b"", content_type="application/json"):
        headers = {"Content-Type": content_type}
        if token:
            headers["Authorization"] = "Bearer " + token
        status, raw, _ = asyncio.run(request(app, path, method, body, headers))
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw

    def login(self, username, password) -> str:
        status, out = self.call("POST", "/api/auth/login", body=json.dumps({"username": username, "password": password}).encode())
        self.assertEqual(status, 200, out)
        return out["access_token"]

    def counts(self) -> tuple[int, int, int]:
        with Session(self.engine) as s:
            return tuple(len(s.exec(select(m)).all()) for m in (Upload, FaceDetection, Attendance))

    def stored_files(self) -> list[str]:
        return sorted(str(p.relative_to(self.storage)) for p in self.storage.rglob("*") if p.is_file())


class RobotReadContractTests(ContractCase):
    def test_every_api_the_hub_reads_answers_a_machine_with_the_fields_it_reads(self):
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        station = self.login("station", STATION_PASSWORD)
        # A recorded central scan from the tablet node, so history and the dashboard have a new-style row.
        node_registry.touch("CAM-02", camera_label="C920")
        body, ctype = multipart({"node_id": "MOBILE-01", "photo": jpeg()})
        self.assertEqual(self.call("POST", "/api/node/recognize-central", station, body, ctype)[0], 200)

        status, out = self.call("GET", "/api/health")
        self.assertEqual((status, out.get("status")), (200, "ok"), "health needs no token")

        status, out = self.call("GET", "/api/history?page_size=100&page=1", robot)
        self.assertEqual(status, 200, out)
        for item in out["items"]:
            for key in ("id", "name", "participant_id", "status", "detected_at", "node_id", "source", "seq", "call_as"):
                self.assertIn(key, item)
        new = [i for i in out["items"] if i["seq"] is not None]
        old = next(i for i in out["items"] if i["id"] == "old-row")
        self.assertEqual((old["seq"], old["node_id"], old["source"], old["name"], old["participant_id"]),
                         (None, None, "unknown", "Ann Alpha", "9001"), "older rows are marked, not guessed")
        self.assertEqual({(i["node_id"], i["source"]) for i in new}, {("MOBILE-01", "node_central")})
        self.assertEqual(sorted(i["status"] for i in new), ["matched", "unknown"])
        self.assertEqual(out["latest_seq"], max(i["seq"] for i in new))

        status, out = self.call("GET", "/api/reports/dashboard", robot)
        self.assertEqual((status, out["detected_today"]), (200, 1))

        status, out = self.call("GET", "/api/node/status", robot)
        self.assertEqual(status, 200)
        by_id = {n["node_id"]: n for n in out["nodes"]}
        self.assertEqual((by_id["CAM-02"]["camera_label"], by_id["CAM-02"]["online"]), ("C920", True))
        self.assertEqual((by_id["MOBILE-01"]["camera_label"], by_id["MOBILE-01"]["online"]), ("iPad", True))

        status, out = self.call("GET", "/api/settings", robot)
        self.assertEqual((status, out), (200, {"camera_label": "Logitech C920", "app_name": "Reconize", "event_name": "My Conference"}))

        status, spec = self.call("GET", "/openapi.json")
        self.assertEqual(status, 200)
        params = {p["name"] for p in spec["paths"]["/api/recognition/upload"]["post"]["parameters"]}
        self.assertIn("persist", params)
        self.assertIn("persist", {p["name"] for p in spec["paths"]["/api/node/recognize-central"]["post"]["parameters"]})
        for path in ("/api/look", "/api/node/{node_id}/present", "/api/history", "/api/node/status"):
            self.assertIn(path, spec["paths"])
        self.assertIn("after_seq", {p["name"] for p in spec["paths"]["/api/history"]["get"]["parameters"]})

        body, ctype = multipart({"photo": jpeg()})
        status, out = self.call("POST", "/api/recognition/upload?persist=false", robot, body, ctype)
        self.assertEqual(status, 200, out)
        self.assertEqual((out["faces_total"], out["persisted"]), (2, False))
        self.assertEqual([(r["status"], r.get("name"), r.get("first_name")) for r in out["results"]],
                         [("matched", "Ann Alpha", "Ann"), ("unknown", None, None)])
        self.assertEqual(out["results"][0]["checkin_status"], "not_recorded")

        body, ctype = multipart({"photo": jpeg(), "source": "nong-cam-1"})
        status, out = self.call("POST", "/api/look", robot, body, ctype)
        self.assertEqual(status, 200, out)
        self.assertEqual((out["image"], out["kept"]), ({"w": 640, "h": 480}, False))
        self.assertEqual([f["status"] for f in out["faces"]], ["matched", "unknown"])     # largest face first
        matched, unknown = out["faces"]
        self.assertEqual((matched["participant_id"], matched["first_name"], matched["name"], matched["call_as"]),
                         ("9001", "Ann", "Ann Alpha", "พี่แอน"))
        self.assertEqual(set(unknown), {"box", "face_score", "status"})
        self.assertTrue(all(0.0 <= v <= 1.0 for v in matched["box"]))

        status, out = self.call("GET", "/api/node/MOBILE-01/present?seconds=10", robot)
        self.assertEqual(status, 200, out)
        self.assertEqual((out["node_id"], out["faces_now"], out["unknown_now"]), ("MOBILE-01", 2, 1))
        self.assertEqual([(p["participant_id"], p["name"], p["call_as"]) for p in out["people"]], [("9001", "Ann Alpha", "พี่แอน")])
        self.assertEqual(set(out["people"][0]), {"participant_id", "name", "call_as", "last_seen", "box", "confidence"})
        self.assertEqual(self.call("GET", "/api/node/CAM-02/present?seconds=10", robot)[1]["people"], [], "node isolation")
        for seconds, expected in (("0", 1), ("31", 30), ("10", 10)):
            self.assertEqual(self.call("GET", f"/api/node/MOBILE-01/present?seconds={seconds}", robot)[1]["seconds"], expected)

        status, out = self.call("GET", "/api/auth/me", robot)
        self.assertEqual((status, out["role"]), (200, "machine"))

    def test_bad_or_missing_token_is_401_on_every_robot_read(self):
        for method, path in (("GET", "/api/history"), ("GET", "/api/reports/dashboard"), ("GET", "/api/node/status"),
                             ("GET", "/api/settings"), ("GET", "/api/node/CAM-02/present")):
            for token in (None, "not-a-token"):
                self.assertEqual(self.call(method, path, token)[0], 401, (path, token))
        self.assertEqual(self.call("POST", "/api/auth/login", body=json.dumps({"username": "robot-hub", "password": "wrong"}).encode())[0], 401)


class MachineCannotWriteTests(ContractCase):
    def test_machine_is_refused_every_write_and_every_persistent_recognition(self):
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        before, files = self.counts(), self.stored_files()
        scan, ctype = multipart({"photo": jpeg()})
        central, ctype_c = multipart({"node_id": "MOBILE-01", "photo": jpeg()})
        refused = [
            ("POST", "/api/recognition/upload", scan, ctype),                     # persist omitted = would record
            ("POST", "/api/recognition/upload?persist=true", scan, ctype),
            ("POST", "/api/node/recognize-central?persist=false", central, ctype_c),
            ("POST", "/api/node/event", *multipart({"body": json.dumps({"node_id": "CAM-02", "matches": []})})),
            ("POST", "/api/node/observation", json.dumps({"node_id": "CAM-02", "image_w": 640, "image_h": 480, "faces": []}).encode(), "application/json"),
            ("POST", "/api/node/heartbeat", json.dumps({"node_id": "CAM-02"}).encode(), "application/json"),
            ("POST", "/api/node/register", json.dumps({"node_id": "ROBOT-X"}).encode(), "application/json"),
            ("DELETE", "/api/history", b"", "application/json"),
            ("PUT", "/api/settings", json.dumps({"values": {"camera_label": "x"}}).encode(), "application/json"),
            ("PUT", f"/api/people/{self.ann.id}/call-as", json.dumps({"call_as": "x"}).encode(), "application/json"),
            ("DELETE", f"/api/people/{self.ann.id}", b"", "application/json"),
            ("GET", "/api/people", b"", "application/json"),                       # not on the robot's list
        ]
        for method, path, body, content_type in refused:
            self.assertEqual(self.call(method, path, robot, body, content_type)[0], 403, (method, path))
        self.assertEqual((self.counts(), self.stored_files()), (before, files))
        with Session(self.engine) as s:
            self.assertEqual(s.get(Person, self.ann.id).call_as, "พี่แอน")
            self.assertEqual(s.exec(select(Setting).where(Setting.key == "camera_label")).one().value, "Logitech C920")
        self.assertNotIn("ROBOT-X", node_registry._nodes)


class FeedContractTests(ContractCase):
    """WS /api/node/events-feed for a machine: matched events exactly as
    before, once per match; with include=presence, unknown faces at a node."""

    class Socket:
        def __init__(self, stop_after: int):
            self.sent, self.closed, self.stop_after = [], None, stop_after

        async def accept(self):
            pass

        async def close(self, code=1000):
            self.closed = code

        async def send_text(self, text):
            self.sent.append(json.loads(text))
            if len(self.sent) >= self.stop_after:
                raise WebSocketDisconnect()

    def test_machine_feed_gets_one_matched_event_and_opt_in_unknown_presence_with_nothing_stored(self):
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        station = self.login("station", STATION_PASSWORD)
        node_registry.touch("CAM-02")
        ordinary, opted = self.Socket(stop_after=2), self.Socket(stop_after=3)

        async def scenario():
            feeds = [asyncio.create_task(nodes.events_feed_ws(ordinary, token=robot, include=None)),
                     asyncio.create_task(nodes.events_feed_ws(opted, token=robot, include="presence"))]
            await asyncio.sleep(0.05)
            # 1) a CCTV station reports ONE face nobody knows at CAM-02 (memory only)
            before = self.counts()
            report = {"node_id": "CAM-02", "image_w": 640, "image_h": 480, "reporter_id": "tab-1",
                      "faces": [{"bbox": [10, 10, 110, 130], "participant_id": None, "confidence": None}]}
            status, _, _ = await request(app, "/api/node/observation", "POST", json.dumps(report).encode(),
                                         {"Content-Type": "application/json", "Authorization": "Bearer " + station})
            self.assertEqual(status, 200)
            self.assertEqual(self.counts(), before, "an unknown face is not stored anywhere")
            await asyncio.sleep(0.05)
            # 2) a recorded scan of two matched people at the tablet node
            self.faces = [DetectedFace(unit(0), (100.0, 80.0, 300.0, 330.0), 0.95), DetectedFace(unit(1), (320.0, 80.0, 480.0, 300.0), 0.9)]
            body, ctype = multipart({"node_id": "MOBILE-01", "photo": jpeg()})
            status, _, _ = await request(app, "/api/node/recognize-central", "POST", body, {"Content-Type": ctype, "Authorization": "Bearer " + station})
            self.assertEqual(status, 200)
            await asyncio.wait_for(asyncio.gather(*feeds), timeout=10)

        asyncio.run(scenario())
        # The ordinary feed: exactly the two matched events, once each, with the real node id.
        self.assertEqual([(e["node_id"], e["participant_id"]) for e in ordinary.sent], [("MOBILE-01", "9001"), ("MOBILE-01", "9002")])
        for event in ordinary.sent:
            self.assertTrue(FEED_FIELDS <= set(event), event)
            self.assertEqual(set(event) - FEED_FIELDS, {"call_as"})
            self.assertNotIn("type", event)
        self.assertEqual([e["call_as"] for e in ordinary.sent], ["พี่แอน", None])
        # The opt-in feed: first the unknown-person presence message, then the same matched events.
        presence = opted.sent[0]
        self.assertEqual({k: presence[k] for k in ("type", "node_id", "status", "faces", "unknown")},
                         {"type": "presence", "node_id": "CAM-02", "status": "unknown", "faces": 1, "unknown": 1})
        self.assertNotIn("participant_id", presence)
        self.assertEqual(opted.sent[1:], ordinary.sent)
        # Presence at CAM-02 is the unknown face only, and nothing about it reached the database.
        present = self.call("GET", "/api/node/CAM-02/present", robot)[1]
        self.assertEqual((present["faces_now"], present["unknown_now"], present["people"]), (1, 1, []))
        with Session(self.engine) as s:
            uploads = s.exec(select(Upload)).all()
        self.assertEqual(sorted((u.source_kind, u.source_node_id) for u in uploads if u.id != "old-upload"), [("node_central", "MOBILE-01")])

    def test_machine_cannot_open_the_signaling_socket(self):
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        socket = self.Socket(stop_after=1)
        asyncio.run(nodes.signal(socket, room="CAM-02", token=robot))
        self.assertEqual(socket.closed, 4401)


if __name__ == "__main__":
    unittest.main()
