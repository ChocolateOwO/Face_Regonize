"""Robot-hub work packages (2026-11-05), over ASGI with the real app on a
temporary database (setup from test_robot_contract). Detection is faked.

WP1 person tags · WP2 tag import · WP3 capacity hints · WP4 batch look ·
WP6 per-source threshold · F3 id stability.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session

from app.api import people as people_api
from app.face_recognition.engine import DetectedFace
from app.models.models import Person
from app.services import events_feed, person_tags
from test_robot_contract import ROBOT_PASSWORD, STATION_PASSWORD, ContractCase, jpeg, multipart, unit


class TagRulesTests(unittest.TestCase):
    def test_normalise_trim_lowercase_dedupe(self):
        self.assertEqual(person_tags.normalize([" VIP ", "staff", "vip", "", "Staff"]), ["vip", "staff"])
        self.assertEqual(person_tags.normalize(person_tags.split_text("vip; staff ,speaker")), ["vip", "staff", "speaker"])
        self.assertEqual(person_tags.normalize(person_tags.split_text('["VIP","ผู้บริหาร"]')), ["vip", "ผู้บริหาร"])
        self.assertEqual(person_tags.normalize(person_tags.split_text("")), [])
        self.assertEqual(person_tags.normalize(["team-a", "level_2", "v1.0"]), ["team-a", "level_2", "v1.0"])

    def test_invalid_values_are_refused_or_reported(self):
        for bad in (["two words"], ['quo"te'], ["x" * 33], ["a/b"]):
            with self.assertRaises(person_tags.TagError):
                person_tags.normalize(bad)
        with self.assertRaises(person_tags.TagError):
            person_tags.normalize([f"t{i}" for i in range(person_tags.MAX_TAGS + 1)])
        with self.assertRaises(person_tags.TagError):
            person_tags.split_text('["vip", 3]')
        self.assertEqual(person_tags.sort_out(["vip", "bad tag", "VIP"]), (["vip"], ["bad tag"]))

    def test_merge_keeps_order_and_bound(self):
        self.assertEqual(person_tags.merge(["vip"], ["staff", "vip"]), ["vip", "staff"])
        self.assertEqual(len(person_tags.merge([f"a{i}" for i in range(20)], ["b"])), person_tags.MAX_TAGS)


class Migration015Tests(unittest.TestCase):
    def test_existing_people_get_an_empty_list(self):
        import importlib

        mod = importlib.import_module("app.migrations.015_person_tags")
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(str(Path(d) / "m.db"))
            try:
                conn.execute("CREATE TABLE person (id TEXT PRIMARY KEY, call_as VARCHAR)")
                conn.execute("INSERT INTO person (id, call_as) VALUES ('p1', 'Boss')")
                mod.upgrade(conn)
                mod.upgrade(conn)  # idempotent
                self.assertEqual(conn.execute("SELECT id, call_as, tags FROM person").fetchall(), [("p1", "Boss", "[]")])
            finally:
                conn.close()


class TagsApiTests(ContractCase):
    def setUp(self):
        super().setUp()
        enroll = DetectedFace(unit(7), (10.0, 10.0, 200.0, 220.0), 0.95)
        for target, name, value in ((people_api, "detect_faces", lambda img, **kw: [enroll]),
                                    (__import__("app.services.storage_service", fromlist=["x"]), "PEOPLE_DIR", self.storage / "people")):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.published = []
        p = patch.object(events_feed, "publish", lambda event: self.published.append(event))
        p.start()
        self.addCleanup(p.stop)

    def put_form(self, token, path, fields):
        body, ctype = multipart(fields)
        return self.call("PUT", path, token, body, ctype)

    def test_create_update_filter_and_read(self):
        station = self.login("station", STATION_PASSWORD)
        body, ctype = multipart({"participant_id": "9100", "first_name": "Vee", "photo": jpeg(), "tags": " VIP; staff,vip "})
        status, out = self.call("POST", "/api/people", station, body, ctype)
        self.assertEqual((status, out["tags"]), (200, ["vip", "staff"]), out)
        vee = out["id"]

        body, ctype = multipart({"participant_id": "9101", "first_name": "Bad", "photo": jpeg(), "tags": "two words"})
        status, out = self.call("POST", "/api/people", station, body, ctype)
        self.assertEqual(status, 422)
        with Session(self.engine) as s:
            self.assertIsNone(s.exec(__import__("sqlmodel").select(Person).where(Person.participant_id == "9101")).first())

        # Full-form update WITHOUT tags keeps them; with tags replaces; empty clears.
        self.assertEqual(self.put_form(station, f"/api/people/{vee}", {"first_name": "Vee2"})[1]["tags"], ["vip", "staff"])
        self.assertEqual(self.put_form(station, f"/api/people/{vee}", {"first_name": "Vee2", "tags": '["Speaker"]'})[1]["tags"], ["speaker"])
        self.assertEqual(self.put_form(station, f"/api/people/{vee}", {"first_name": "Vee2", "tags": "vip"})[1]["tags"], ["vip"])
        self.assertEqual(self.call("GET", f"/api/people/{vee}", station)[1]["tags"], ["vip"])
        status, out = self.put_form(station, f"/api/people/{vee}", {"first_name": "Vee2", "tags": "a/b"})
        self.assertEqual(status, 422)
        self.assertEqual(self.call("GET", f"/api/people/{vee}", station)[1]["first_name"], "Vee2", "nothing changed on a bad tag")

        # Every person has the field; older rows read as [].
        people = self.call("GET", "/api/people", station)[1]
        self.assertTrue(all(isinstance(p["tags"], list) for p in people))
        self.assertEqual({p["participant_id"]: p["tags"] for p in people}["9001"], [])
        # Filter, bare list and paged envelope.
        self.assertEqual([p["participant_id"] for p in self.call("GET", "/api/people?tag=VIP", station)[1]], ["9100"])
        paged = self.call("GET", "/api/people?tag=vip&page=1", station)[1]
        self.assertEqual(([p["participant_id"] for p in paged["items"]], paged["total"]), (["9100"], 1))
        self.assertEqual(self.call("GET", "/api/people?tag=vi&page=1", station)[1]["total"], 0, "whole tags only")
        self.assertEqual(self.call("GET", "/api/people?tag=a%20b", station)[0], 422)

    def test_tags_reach_look_history_and_feed_for_the_matched_person_only(self):
        with Session(self.engine) as s:
            ann = s.get(Person, self.ann.id)
            ann.tags = ["vip", "speaker"]
            s.add(ann)
            s.commit()
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        station = self.login("station", STATION_PASSWORD)

        body, ctype = multipart({"photo": jpeg(), "source": "cam-1"})
        status, out = self.call("POST", "/api/look", robot, body, ctype)
        self.assertEqual(status, 200, out)
        matched, unknown = out["faces"]
        self.assertEqual((matched["participant_id"], matched["tags"], unknown["tags"]), ("9001", ["vip", "speaker"], []))

        body, ctype = multipart({"node_id": "MOBILE-01", "photo": jpeg()})
        self.assertEqual(self.call("POST", "/api/node/recognize-central", station, body, ctype)[0], 200)
        self.assertEqual([(e["participant_id"], e["tags"]) for e in self.published], [("9001", ["vip", "speaker"])])

        items = self.call("GET", "/api/history?page_size=100", robot)[1]["items"]
        new = [i for i in items if i["seq"] is not None]
        self.assertEqual(sorted((i["status"], tuple(i["tags"])) for i in new), [("matched", ("vip", "speaker")), ("unknown", ())])
        old = next(i for i in items if i["id"] == "old-row")
        self.assertEqual(old["tags"], ["vip", "speaker"], "the person's current tags, also on older rows")

        # Local-agent event path publishes them too.
        self.published.clear()
        node_body = json.dumps({"node_id": "CAM-02", "matches": [{"participant_id": "9001", "confidence": 0.9}]})
        status, out = self.call("POST", "/api/node/event", station, *multipart({"body": node_body}))
        self.assertEqual(status, 200, out)
        self.assertEqual([e["tags"] for e in self.published], [["vip", "speaker"]])


class TagImportTests(ContractCase):
    """WP2: optional Tags column in file uploads and Google Sheet sync."""

    def setUp(self):
        super().setUp()
        from app.api import imports as imports_api
        from app.face_recognition.index import RecognitionIndex
        from app.services import import_service, storage_service

        enroll = DetectedFace(unit(9), (10.0, 10.0, 200.0, 220.0), 0.95)
        self.sheet_csv = b""
        for target, name, value in (
            (import_service, "engine", self.engine),
            (import_service, "detect_faces", lambda img, **kw: [enroll]),
            (import_service, "_download_image_bytes", lambda url: jpeg()),
            (import_service, "recognition_index", RecognitionIndex()),
            (storage_service, "PEOPLE_DIR", self.storage / "people"),
            (imports_api, "fetch_sheet_csv", lambda url: self.sheet_csv),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.station = self.login("station", STATION_PASSWORD)

    def upload(self, csv_text):
        body, ctype = multipart({"file": csv_text.encode("utf-8")})
        body = body.replace(b'filename="f.jpg"', b'filename="people.csv"').replace(b"image/jpeg", b"text/csv")
        status, out = self.call("POST", "/api/import/preview", self.station, body, ctype)
        self.assertEqual(status, 200, out)
        return out

    def confirm(self, import_id, **fields):
        status, out = self.call("POST", f"/api/import/{import_id}/confirm", self.station, *multipart(fields))
        return status, out

    def tags_of(self, participant_id):
        with Session(self.engine) as s:
            from sqlmodel import select
            p = s.exec(select(Person).where(Person.participant_id == participant_id)).first()
            return None if p is None else list(p.tags)

    def test_file_with_tags_column_imports_valid_tags_and_reports_bad_ones(self):
        out = self.upload("Name,Photo,Tags\nNok Noi,https://x/1.jpg,VIP; staff\nGus Gee,https://x/2.jpg,\"bad tag, guest\"\n"
                          "Ann Alpha,https://x/3.jpg,vip\n")
        self.assertEqual((out["tags_column"], out["invalid_tag_rows"]), ("Tags", 1))
        rows = {r["name"]: r for r in out["preview"]}
        self.assertEqual((rows["Nok Noi"]["tags"], rows["Nok Noi"]["invalid_tags"]), (["vip", "staff"], []))
        self.assertEqual((rows["Gus Gee"]["tags"], rows["Gus Gee"]["invalid_tags"]), (["guest"], ["bad tag"]))
        self.assertEqual(self.confirm(out["import_id"])[0], 200)
        self.assertEqual((self.tags_of(rows["Nok Noi"]["participant_id"]), self.tags_of(rows["Gus Gee"]["participant_id"])),
                         (["vip", "staff"], ["guest"]))
        job = self.call("GET", f"/api/imports/{out['import_id']}", self.station)[1]
        self.assertEqual(job["job"]["status"], "completed", "a bad tag never fails the import")
        self.assertEqual(job["job"]["tags_mode"], "merge")
        by_name = {r["first_name"]: r for r in job["rows"]}
        self.assertEqual((by_name["Gus Gee"]["status"], by_name["Gus Gee"]["tag_errors"]), ("imported", "bad tag"))
        self.assertEqual(self.tags_of("9001"), [], "a file upload never touches an existing participant")

    def test_file_without_tags_column_is_unchanged_behaviour(self):
        out = self.upload("Name,Photo\nNok Noi,https://x/1.jpg\n")
        self.assertIsNone(out["tags_column"])
        self.assertIsNone(out["preview"][0]["tags"])
        self.assertEqual(self.confirm(out["import_id"])[0], 200)
        self.assertEqual(self.tags_of(out["preview"][0]["participant_id"]), [])

    def test_bad_tags_mode_is_refused(self):
        out = self.upload("Name,Photo,Tags\nNok Noi,https://x/1.jpg,vip\n")
        self.assertEqual(self.confirm(out["import_id"], tags_mode="overwrite")[0], 422)

    def sync(self, csv_text, **confirm_fields):
        self.sheet_csv = csv_text.encode("utf-8")
        status, out = self.call("POST", "/api/import/sheet/preview", self.station,
                                json.dumps({"url": "https://docs.google.com/spreadsheets/d/x",
                                            "name_column": "Name", "photo_column": "Photo"}).encode())
        self.assertEqual(status, 200, out)
        self.assertEqual(self.confirm(out["import_id"], **confirm_fields)[0], 200)
        return out

    def test_sheet_sync_merges_by_default_replaces_on_request_and_ignores_a_missing_column(self):
        with Session(self.engine) as s:
            ann = s.get(Person, self.ann.id)
            ann.tags = ["speaker"]
            s.add(ann)
            s.commit()
        self.sync("Name,Photo,Tags\nAnn Alpha,,VIP;bad tag\n")                   # unchanged participant row
        self.assertEqual(self.tags_of("9001"), ["speaker", "vip"])
        self.sync("Name,Photo,Tags\nAnn Alpha,,staff\n", tags_mode="replace")
        self.assertEqual(self.tags_of("9001"), ["staff"])
        self.sync("Name,Photo\nAnn Alpha,\n", tags_mode="replace")               # no Tags column: untouched
        self.assertEqual(self.tags_of("9001"), ["staff"])
        self.sync("Name,Photo,Tags\nAnn Alpha,,\n", tags_mode="replace")          # column present, cell blank
        self.assertEqual(self.tags_of("9001"), [], "replace with an empty cell clears")


class CapacityTests(unittest.TestCase):
    """WP3: capacity hints from the live scheduler."""

    def test_no_estimate_before_any_frame_then_measured_per_frame(self):
        import time as _time
        from app.services.live_scheduler import ACTIVE_WINDOW_S, HEADROOM, LiveScheduler

        s = LiveScheduler()
        cap = s.capacity()
        self.assertIsNone(cap["max_live_fps"])
        self.assertIsNone(cap["recommended_fps_per_source"])
        for src in ("a", "b", "c"):
            t = s.acquire(src, timeout=1, drop=True, frames=2)   # 2 frames in one turn (a batch)
            _time.sleep(0.04)
            s.release(t)
        cap = s.capacity()
        self.assertEqual(cap["active_sources"], 3)
        self.assertGreater(cap["frame_ms"], 15)     # ~20 ms per frame, not ~40 per turn
        self.assertLess(cap["frame_ms"], 40)
        self.assertAlmostEqual(cap["recommended_fps_per_source"], round(cap["max_live_fps"] * HEADROOM / 3, 1), places=1)
        # A source that has been quiet longer than the window no longer counts.
        s._seen["a"] -= ACTIVE_WINDOW_S + 1
        self.assertEqual(s.capacity()["active_sources"], 2)


class BusyBodyTests(ContractCase):
    """WP3: a 429 from /api/look says which limit was hit; headers unchanged."""

    def test_inflight_limit_is_global_queue(self):
        from app.services import look_service

        robot = self.login("robot-hub", ROBOT_PASSWORD)
        held = [look_service._admission.acquire(blocking=False) for _ in range(look_service.MAX_IN_FLIGHT)]
        try:
            self.assertTrue(all(held))
            status, raw, headers = asyncio.run(__import__("test_local_video_experiment").request(
                __import__("app.main", fromlist=["app"]).app, "/api/look", "POST",
                *self._look_body(robot)))
        finally:
            for _ in held:
                look_service._admission.release()
        body = json.loads(raw)
        self.assertEqual(status, 429)
        self.assertEqual((body["limit"], body["reason"]), ("global_queue", "inflight_full"))
        self.assertIsInstance(body["retry_after_ms"], int)
        self.assertIn("in progress", body["detail"], "the original detail text is kept")
        hdrs = {k.decode().lower() if isinstance(k, bytes) else k.lower(): v for k, v in (headers.items() if isinstance(headers, dict) else headers)}
        self.assertGreaterEqual(int(hdrs["retry-after"] if not isinstance(hdrs["retry-after"], bytes) else hdrs["retry-after"].decode()), 1)

    def _look_body(self, token):
        body, ctype = multipart({"photo": jpeg(), "source": "cam-1"})
        return body, {"Content-Type": ctype, "Authorization": "Bearer " + token}

    def test_per_source_and_timeout_limits(self):
        import threading
        import time as _time
        from app.services import live_scheduler, look_service

        sched = live_scheduler.LiveScheduler()
        with patch.object(live_scheduler, "scheduler", sched):
            holder = sched.acquire("look:other", timeout=1, drop=True)         # someone else is being served
            waiting = threading.Thread(target=lambda: sched.acquire("look:cam-1", timeout=3, drop=True))
            waiting.start()
            deadline = _time.time() + 2
            while sched.snapshot()["queue"] < 1 and _time.time() < deadline:
                _time.sleep(0.005)
            with self.assertRaises(look_service.LookError) as ctx:
                look_service.look(jpeg(), source="cam-1", user_id="u")          # cam-1 already has one waiting
            self.assertEqual((ctx.exception.limit, ctx.exception.reason), ("per_source", "source_busy"))
            with self.assertRaises(look_service.LookError) as ctx:
                look_service.look(jpeg(), source="cam-2", user_id="u")          # waits 0.5 s, holder never releases
            self.assertEqual((ctx.exception.status, ctx.exception.limit, ctx.exception.reason), (429, "timeout", "turn_timeout"))
            sched.release(holder)
            waiting.join(5)

    def test_health_reports_capacity_quickly(self):
        import time as _time

        robot = self.login("robot-hub", ROBOT_PASSWORD)
        self.call("POST", "/api/look", robot, *multipart({"photo": jpeg(), "source": "cam-1"}))
        began = _time.perf_counter()
        status, out = self.call("GET", "/api/health")
        self.assertEqual((status, out["status"]), (200, "ok"))
        cap = out["look_capacity"]
        for key in ("max_live_fps", "recommended_fps_per_source", "active_sources", "queue", "queue_limit",
                    "per_source_waiting_limit", "inflight", "inflight_limit", "frame_deadline_ms", "max_batch"):
            self.assertIn(key, cap)
        self.assertGreaterEqual(cap["active_sources"], 1)
        self.assertLess(_time.perf_counter() - began, 0.5)


class BatchEngineTests(unittest.TestCase):
    """WP4: detect_faces_batch detects frame by frame, embeds ALL faces in ONE call."""

    def test_one_recognition_call_for_all_frames_and_same_normalisation(self):
        from types import SimpleNamespace

        import numpy as np

        from app.face_recognition import engine

        calls = []

        class Det:
            def detect(self, img, max_num=0, metric="default"):
                n = int(img[0, 0, 0])  # the test encodes the face count in the first pixel
                boxes = np.array([[10 + 30 * i, 10, 40 + 30 * i, 50, 0.9] for i in range(n)], np.float32).reshape(-1, 5)
                kps = np.tile(np.array([[20, 20], [30, 20], [25, 28], [21, 36], [29, 36]], np.float32), (n, 1, 1))
                return boxes, kps

        class Rec:
            input_size = (112, 112)

            def get_feat(self, imgs):
                calls.append(len(imgs))
                return np.stack([np.full(512, i + 1.0, np.float32) for i in range(len(imgs))])

        app = SimpleNamespace(det_model=Det(), models={"recognition": Rec()})
        frames = [np.full((100, 160, 3), n, np.uint8) for n in (2, 0, 3)]
        with patch.object(engine, "get_face_app", lambda: app), patch.object(engine, "_live_app", None):
            timings = {}
            out = engine.detect_faces_batch(frames, timings)
        self.assertEqual(calls, [5], "one batched embedding call for all 5 faces")
        self.assertEqual([len(f) for f in out], [2, 0, 3])
        for f in out[0] + out[2]:
            self.assertAlmostEqual(float(np.linalg.norm(f.embedding)), 1.0, places=5)
        self.assertIn("embedding_ms", timings)
        self.assertFalse(engine.inference_lock.locked())


class BatchLookTests(ContractCase):
    """WP4: POST /api/look/batch."""

    def setUp(self):
        super().setUp()
        from app.services import look_service

        p = patch.object(look_service.engine, "detect_faces_batch",
                         lambda imgs, timings=None, lock_timeout=None: [list(self.faces) for _ in imgs])
        p.start()
        self.addCleanup(p.stop)
        self.robot = self.login("robot-hub", ROBOT_PASSWORD)

    def batch(self, photos, sources=None):
        boundary = "----batchtest"
        parts = []
        for data in photos:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photos"; filename="f.jpg"\r\n'
                         f"Content-Type: image/jpeg\r\n\r\n".encode() + data + b"\r\n")
        for s in sources or []:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="sources"\r\n\r\n{s}\r\n'.encode())
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        return self.call("POST", "/api/look/batch", self.robot, body, f"multipart/form-data; boundary={boundary}")

    def test_per_item_results_in_the_look_shape(self):
        status, out = self.batch([jpeg(), b"not a jpeg", jpeg(320, 240)], ["cam-a", "cam-b", "cam-c"])
        self.assertEqual(status, 200, out)
        self.assertIs(out["kept"], False)
        a, b, c = out["items"]
        self.assertEqual([(i["index"], i["source"], i["status"]) for i in out["items"]],
                         [(0, "cam-a", 200), (1, "cam-b", 400), (2, "cam-c", 200)])
        self.assertIn("detail", b)
        self.assertEqual((a["image"], c["image"]), ({"w": 640, "h": 480}, {"w": 320, "h": 240}))
        self.assertEqual([f["status"] for f in a["faces"]], ["matched", "unknown"])
        self.assertEqual((a["faces"][0]["participant_id"], a["faces"][0]["call_as"], a["faces"][1]["tags"]), ("9001", "พี่แอน", []))
        self.assertEqual(a["threshold_used"], 0.45)

    def test_malformed_batches_are_422(self):
        self.assertEqual(self.batch([jpeg()] * 9)[0], 422)
        self.assertEqual(self.batch([jpeg(), jpeg()], ["only-one"])[0], 422)

    def test_all_items_busy_is_429_with_the_limit(self):
        from app.services import look_service

        held = [look_service._admission.acquire(blocking=False) for _ in range(look_service.MAX_IN_FLIGHT)]
        try:
            status, out = self.batch([jpeg(), jpeg()], ["cam-a", "cam-b"])
        finally:
            for _ in held:
                look_service._admission.release()
        self.assertEqual((status, out["limit"], out["reason"]), (429, "global_queue", "inflight_full"))

    def test_single_look_is_unchanged_next_to_batch(self):
        status, out = self.call("POST", "/api/look", self.robot, *multipart({"photo": jpeg(), "source": "cam-1"}))
        self.assertEqual(status, 200)
        self.assertEqual(set(out), {"image", "took_ms", "kept", "faces", "threshold_used"})


class ThresholdOverrideTests(ContractCase):
    """WP6: per-source face_match_threshold with fallback to the global value."""

    def setUp(self):
        super().setUp()
        import numpy as np

        from app.services import settings_cache

        self.addCleanup(settings_cache.set_threshold_overrides, {})
        v = unit(0) * 0.35 + unit(5) * np.sqrt(1 - 0.35 ** 2)          # similarity 0.35 to Ann
        self.faces = [DetectedFace(v.astype(np.float32), (100.0, 80.0, 300.0, 330.0), 0.95)]
        self.station = self.login("station", STATION_PASSWORD)
        self.robot = self.login("robot-hub", ROBOT_PASSWORD)

    def put_overrides(self, value):
        return self.call("PUT", "/api/settings", self.station,
                         json.dumps({"values": {"face_match_threshold_overrides": value}}).encode())

    def look(self, source):
        return self.call("POST", "/api/look", self.robot, *multipart({"photo": jpeg(), "source": source}))[1]

    def test_override_applies_to_its_source_only(self):
        self.assertEqual(self.look("far-cam")["faces"][0]["status"], "unknown")       # global 0.45
        status, out = self.put_overrides(json.dumps({"far-cam": 0.3, "MOBILE-01": 0.3}))
        self.assertEqual(status, 200, out)
        self.assertEqual(json.loads(out["face_match_threshold_overrides"]), {"MOBILE-01": 0.3, "far-cam": 0.3})
        far = self.look("far-cam")
        self.assertEqual((far["threshold_used"], far["faces"][0]["status"], far["faces"][0]["participant_id"]),
                         (0.3, "matched", "9001"))
        near = self.look("near-cam")
        self.assertEqual((near["threshold_used"], near["faces"][0]["status"]), (0.45, "unknown"), "fallback to global")
        # Central camera node: keyed by node_id.
        status, out = self.call("POST", "/api/node/recognize-central?persist=false", self.station,
                                *multipart({"node_id": "MOBILE-01", "photo": jpeg()}))
        self.assertEqual((status, out["threshold_used"], out["faces_matched"]), (200, 0.3, 1))
        # Reading back (admin) and clearing.
        self.assertEqual(json.loads(self.call("GET", "/api/settings", self.station)[1]["face_match_threshold_overrides"]),
                         {"MOBILE-01": 0.3, "far-cam": 0.3})
        self.assertEqual(self.put_overrides("{}")[0], 200)
        self.assertEqual(self.look("far-cam")["threshold_used"], 0.45)

    def test_invalid_overrides_are_refused_and_change_nothing(self):
        self.assertEqual(self.put_overrides(json.dumps({"far-cam": 0.3}))[0], 200)
        for bad in (json.dumps({"far-cam": 0.95}), json.dumps({"far-cam": "0.3"}), json.dumps({"bad name!": 0.3}),
                    json.dumps(["far-cam"]), "not json", json.dumps({"x": True})):
            status, out = self.put_overrides(bad)
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.look("far-cam")["threshold_used"], 0.3, "the last valid value stays")
        self.assertEqual(self.call("PUT", "/api/settings", self.robot,
                                   json.dumps({"values": {"face_match_threshold_overrides": "{}"}}).encode())[0], 403)
        self.assertNotIn("face_match_threshold_overrides", self.call("GET", "/api/settings", self.robot)[1],
                         "a machine sees only its three settings")


class IdStabilityTests(ContractCase):
    """F3: the same person_id / participant_id everywhere the hub reads them."""

    def test_ids_match_across_look_history_feed_and_people(self):
        published = []
        with patch.object(events_feed, "publish", lambda e: published.append(e)):
            robot = self.login("robot-hub", ROBOT_PASSWORD)
            station = self.login("station", STATION_PASSWORD)
            look = self.call("POST", "/api/look", robot, *multipart({"photo": jpeg(), "source": "cam-1"}))[1]
            self.assertEqual(self.call("POST", "/api/node/recognize-central", station,
                                       *multipart({"node_id": "MOBILE-01", "photo": jpeg()}))[0], 200)
        hist = [i for i in self.call("GET", "/api/history?page_size=100", robot)[1]["items"] if i["status"] == "matched" and i["seq"]]
        people = {p["id"]: p["participant_id"] for p in self.call("GET", "/api/people", station)[1]}
        m = look["faces"][0]
        self.assertEqual(people[m["person_id"]], m["participant_id"])
        self.assertEqual({(i["person_id"], i["participant_id"]) for i in hist}, {(m["person_id"], m["participant_id"])})
        self.assertEqual({e["participant_id"] for e in published}, {m["participant_id"]})
        self.assertEqual((self.ann.id, self.ann.participant_id), (m["person_id"], m["participant_id"]))


if __name__ == "__main__":
    unittest.main()
