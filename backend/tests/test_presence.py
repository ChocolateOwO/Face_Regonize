"""Live presence: who is in front of one camera right now.

Memory only, short-lived, per node, never rebuilt from history; unknown faces
are counted without saving anything; presence messages reach only feed
subscribers that asked for them. Handlers are called directly, detection is
faked, the database and storage are temporary."""
from __future__ import annotations

import asyncio
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException, UploadFile, WebSocketDisconnect
from pydantic import ValidationError
from sqlmodel import Session, select

with patch("logging.basicConfig"):  # the agent configures root logging on import; keep test output quiet
    import node_agent
from app.api import nodes
from app.face_recognition.engine import DetectedFace
from app.face_recognition.index import RecognitionIndex
from app.models.models import CameraNode, Person, User
from app.services import events_feed, node_registry, presence_service
from tests.test_look_api import LookCase
from tests.test_look_api import jpeg as look_jpeg
from tests.test_recognition_persist import USER, PersistCase, face, unit


def known(pid: str, box, confidence=0.8, name="N") -> dict:
    return {"box": box, "participant_id": pid, "person_id": "id-" + pid, "name": name, "confidence": confidence}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ServiceCase(unittest.TestCase):
    def setUp(self):
        presence_service.reset()
        self.addCleanup(presence_service.reset)
        self.clock = Clock()
        p = patch.object(presence_service, "_clock", self.clock)
        p.start()
        self.addCleanup(p.stop)


class ServiceTests(ServiceCase):
    def test_nodes_are_separate(self):
        presence_service.record("CAM-01", 640, 480, [known("0001", [0.1, 0.1, 0.3, 0.5])])
        presence_service.record("CAM-02", 640, 480, [{"box": [0.2, 0.2, 0.4, 0.6]}, {"box": None}])
        one, two, three = (presence_service.present(n) for n in ("CAM-01", "CAM-02", "CAM-03"))
        self.assertEqual((one["faces_now"], one["unknown_now"], [p["participant_id"] for p in one["people"]]), (1, 0, ["0001"]))
        self.assertEqual((two["faces_now"], two["unknown_now"], two["people"]), (2, 2, []))
        self.assertEqual((three["faces_now"], three["unknown_now"], three["people"], three["observed_at"]), (0, 0, [], None))
        self.assertEqual(one["image"], {"w": 640, "h": 480})

    def test_unknown_faces_are_counted_but_never_given_an_identity(self):
        presence_service.record("CAM-01", 640, 480, [{"box": [0.1, 0.1, 0.2, 0.2]}, known("0001", [0.5, 0.5, 0.6, 0.6])])
        out = presence_service.present("CAM-01")
        self.assertEqual((out["faces_now"], out["unknown_now"]), (2, 1))
        self.assertEqual(len(out["people"]), 1)
        self.assertEqual(set(out["people"][0]), {"participant_id", "person_id", "name", "call_as", "last_seen", "box", "confidence"})
        self.assertNotIn("gender", str(out).lower())

    def test_people_are_sorted_largest_face_first_using_the_newest_sighting(self):
        presence_service.record("CAM-01", 640, 480, [known("0001", [0.0, 0.0, 0.9, 0.9]), known("0002", [0.0, 0.0, 0.1, 0.1])])
        self.clock.now += 1
        # 0001 walked away (small box now); 0003 is closest; 0002 left the frame.
        presence_service.record("CAM-01", 640, 480, [known("0001", [0.0, 0.0, 0.2, 0.2]), known("0003", [0.0, 0.0, 0.5, 0.5]),
                                                       known("0004", None)])
        out = presence_service.present("CAM-01")
        self.assertEqual([p["participant_id"] for p in out["people"]], ["0003", "0001", "0002", "0004"])
        self.assertEqual(out["people"][1]["box"], [0.0, 0.0, 0.2, 0.2])
        self.assertEqual((out["faces_now"], out["unknown_now"]), (3, 0))
        self.assertLess(out["people"][2]["last_seen"], out["people"][0]["last_seen"] + "~")

    def test_window_is_bounded_and_expires(self):
        self.assertEqual([presence_service.clamp_window(v) for v in (-5, 0, 1, 10, 30, 31, 9999, "x", None)],
                         [1, 1, 1, 10, 30, 30, 30, 10, 10])
        presence_service.record("CAM-01", 640, 480, [known("0001", [0.1, 0.1, 0.3, 0.3])])
        self.clock.now += 6
        self.assertEqual(presence_service.present("CAM-01", 5)["people"], [])
        self.assertEqual(presence_service.present("CAM-01", 5)["observed_at"], None)
        self.assertEqual(len(presence_service.present("CAM-01", 10)["people"]), 1)
        self.clock.now += 20  # 26 s old: inside the hard limit only
        self.assertEqual(len(presence_service.present("CAM-01", 9999)["people"]), 1)
        self.assertEqual(presence_service.present("CAM-01", 9999)["seconds"], 30)
        self.clock.now += 5   # 31 s old: gone for good, whatever is asked for
        self.assertEqual(presence_service.present("CAM-01", 9999)["people"], [])
        self.assertEqual(len(presence_service._nodes["CAM-01"]), 0)

    def test_old_observations_are_dropped_and_memory_is_bounded(self):
        for _ in range(presence_service.MAX_OBSERVATIONS + 50):
            presence_service.record("CAM-01", 640, 480, [{"box": None}] * (presence_service.MAX_FACES + 20))
        queue = presence_service._nodes["CAM-01"]
        self.assertEqual(len(queue), presence_service.MAX_OBSERVATIONS)
        self.assertEqual(len(queue[-1]["faces"]), presence_service.MAX_FACES)
        self.clock.now += 31
        presence_service.record("CAM-01", 640, 480, [])
        self.assertEqual(len(queue), 1)

    def test_restart_starts_empty(self):
        presence_service.record("CAM-01", 640, 480, [known("0001", [0.1, 0.1, 0.3, 0.3])])
        presence_service.reset()  # what a new process starts with: the module's empty dicts
        out = presence_service.present("CAM-01")
        self.assertEqual((out["faces_now"], out["people"], out["observed_at"]), (0, [], None))

    def test_no_image_or_embedding_is_held(self):
        presence_service.record("CAM-01", 640, 480, [known("0001", [0.1, 0.1, 0.3, 0.3])])
        held = presence_service._nodes["CAM-01"][0]
        self.assertEqual(set(held), {"at", "wall", "image", "faces"})
        self.assertEqual(held["image"], {"w": 640, "h": 480})
        self.assertEqual(set(held["faces"][0]), {"box", "participant_id", "person_id", "name", "confidence"})

    def test_boxes_are_normalized_and_clipped(self):
        box = presence_service.normalized_box
        self.assertEqual(box([64, 48, 320, 240], 640, 480), [0.1, 0.1, 0.5, 0.5])
        self.assertEqual(box([-20, -5, 700, 500], 640, 480), [0.0, 0.0, 1.0, 1.0])
        for bad in ([0, 0, 0, 0], [10, 10, 5, 5], [1, 2, 3], "x", [float("nan"), 0, 1, 1], [0, 0, float("inf"), 1]):
            self.assertIsNone(box(bad, 640, 480), bad)
        self.assertIsNone(box([0, 0, 10, 10], 0, 480))

    def test_a_node_is_bound_to_one_reporter_while_it_keeps_reporting(self):
        presence_service.claim("CAM-01", "station-a")
        presence_service.claim("CAM-01", "station-a")                       # the same reporter renews freely
        presence_service.claim("CAM-02", "station-b")                       # another node is another binding
        with self.assertRaises(presence_service.ReporterConflict):
            presence_service.claim("CAM-01", "station-b")
        self.clock.now += presence_service.REPORTER_LEASE_SECONDS - 1
        presence_service.claim("CAM-01", "station-a")                       # still reporting: lease renewed
        self.clock.now += presence_service.REPORTER_LEASE_SECONDS - 1
        with self.assertRaises(presence_service.ReporterConflict):
            presence_service.claim("CAM-01", "station-b")                   # a refused attempt does not steal or extend
        self.clock.now += 2                                                 # station-a has been silent past the lease
        presence_service.claim("CAM-01", "station-b")
        with self.assertRaises(presence_service.ReporterConflict):
            presence_service.claim("CAM-01", "station-a")
        presence_service.reset()
        presence_service.claim("CAM-01", "station-a")                       # a restart forgets every binding

    def test_presence_events_are_throttled(self):
        unknown, nobody = [{"box": None}], []
        self.assertEqual(presence_service.record("CAM-01", 640, 480, nobody), {})  # nothing to announce
        first = presence_service.record("CAM-01", 640, 480, unknown)
        self.assertEqual({k: first[k] for k in ("type", "node_id", "status", "faces", "unknown")},
                         {"type": "presence", "node_id": "CAM-01", "status": "unknown", "faces": 1, "unknown": 1})
        self.assertEqual(set(first), {"type", "node_id", "status", "faces", "unknown", "at"})
        self.clock.now += 0.4
        self.assertEqual(presence_service.record("CAM-01", 640, 480, unknown), {})       # same situation, too soon
        self.assertEqual(presence_service.record("CAM-02", 640, 480, unknown)["node_id"], "CAM-02")  # per node
        self.assertEqual(presence_service.record("CAM-01", 640, 480, unknown * 2)["unknown"], 2)     # count changed
        self.clock.now += presence_service.PRESENCE_EVENT_SECONDS
        self.assertEqual(presence_service.record("CAM-01", 640, 480, unknown * 2)["unknown"], 2)     # still there, due
        gone = presence_service.record("CAM-01", 640, 480, [known("0001", None)])
        self.assertEqual((gone["status"], gone["unknown"], gone["faces"]), ("clear", 0, 1))
        self.clock.now += 60
        self.assertEqual(presence_service.record("CAM-01", 640, 480, nobody), {})        # clear is said once


class RouteCase(PersistCase):
    """Real handlers against the temp database of the recognition tests."""

    def setUp(self):
        super().setUp()
        presence_service.reset()
        self.addCleanup(presence_service.reset)
        self.presence_events: list[dict] = []
        p = patch.object(nodes.events_feed, "publish_presence", side_effect=self.presence_events.append)
        p.start()
        self.addCleanup(p.stop)
        with Session(self.engine) as s:
            for node_id, mode in (("CAM-01", "local"), ("CAM-02", "local"), ("MOBILE-01", "central")):
                s.add(CameraNode(node_id=node_id, display_name=node_id, inference_mode=mode))
                node_registry.touch(node_id)   # its station page is open and heartbeating
            ann = s.exec(select(Person).where(Person.participant_id == "0001")).one()
            ann.call_as = "Khun Ann"
            s.add(ann)
            s.commit()

    def observe(self, node_id="CAM-01", faces=(), w=640, h=480, *, tab="tab-1", host="192.168.1.20", user=USER):
        body = nodes.NodeObservationBody(node_id=node_id, image_w=w, image_h=h, faces=list(faces), reporter_id=tab)
        request = SimpleNamespace(client=SimpleNamespace(host=host))
        with Session(self.engine) as s:
            return nodes.node_observation(body, request, session=s, user=user)

    def refused(self, status, **kw) -> str:
        with self.assertRaises(HTTPException) as ctx:
            self.observe(**kw)
        self.assertEqual(ctx.exception.status_code, status, ctx.exception.detail)
        return ctx.exception.detail

    def present(self, node_id="CAM-01", seconds=10):
        with Session(self.engine) as s:
            return nodes.node_present(node_id, seconds=seconds, session=s, user=USER)


class ObservationRouteTests(RouteCase):
    def test_matched_and_unknown_faces_reach_presence_without_saving_anything(self):
        out = self.observe(faces=[
            {"bbox": [0, 0, 64, 48], "participant_id": "0002", "confidence": 0.61},
            {"bbox": [100, 100, 420, 340], "participant_id": "0001", "confidence": 0.72},
            {"bbox": [500, 10, 560, 80]},
        ])
        self.assertEqual(out, {"accepted": True, "node_id": "CAM-01", "faces": 3, "unknown": 1})
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.published, [])  # no matched event: attendance still travels by /event
        got = self.present()
        self.assertEqual((got["node_id"], got["faces_now"], got["unknown_now"], got["seconds"]), ("CAM-01", 3, 1, 10))
        self.assertEqual([(p["participant_id"], p["name"], p["call_as"], p["confidence"]) for p in got["people"]],
                         [("0001", "Ann Alpha", "Khun Ann", 0.72), ("0002", "Ben Beta", None, 0.61)])
        self.assertEqual(got["people"][0]["box"], [0.1562, 0.2083, 0.6562, 0.7083])
        self.assertEqual(set(got["people"][0]), {"participant_id", "name", "call_as", "last_seen", "box", "confidence"})
        self.assertEqual(set(got), {"node_id", "seconds", "as_of", "observed_at", "image", "faces_now", "unknown_now", "people"})
        self.assertEqual(self.present("CAM-02")["people"], [])
        self.assertEqual([e["status"] for e in self.presence_events], ["unknown"])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_node_must_be_registered(self):
        for node_id in ("CAM-99", "   "):
            with self.assertRaises(HTTPException) as ctx:
                self.observe(node_id=node_id, faces=[{"bbox": [0, 0, 10, 10]}])
            self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(presence_service._nodes, {})
        self.assertEqual(self.presence_events, [])
        with Session(self.engine) as s:
            self.assertEqual(len(s.exec(select(CameraNode)).all()), 3)  # reporting never creates a node

    def test_faces_cannot_be_attributed_to_a_node_another_station_is_reporting(self):
        ann = [{"bbox": [100, 100, 420, 340], "participant_id": "0001", "confidence": 0.72}]
        ben = [{"bbox": [0, 0, 64, 48], "participant_id": "0002", "confidence": 0.9}, {"bbox": [70, 0, 130, 48]}]
        self.observe(faces=ann)                                              # station A, the camera's real page
        other_account = User(id="u2", username="station2", password_hash="x")
        for label, kw in (("another browser tab or PC with the same node name", dict(tab="tab-2")),
                          ("another computer", dict(host="192.168.1.99")),
                          ("another account", dict(user=other_account)),
                          ("a client that sends no page id", dict(tab=None))):
            detail = self.refused(409, faces=ben, **kw)
            self.assertIn("Another station is already reporting for this node", detail, label)
        got = self.present()
        self.assertEqual(([p["participant_id"] for p in got["people"]], got["faces_now"], got["unknown_now"]), (["0001"], 1, 0))
        self.assertEqual(self.presence_events, [])                           # the injected unknown face announced nothing
        self.observe(faces=ann)                                              # station A is not disturbed by the attempts
        self.observe(node_id="CAM-02", faces=ben, tab="tab-2")               # the other station reports under its OWN node
        self.assertEqual([p["participant_id"] for p in self.present("CAM-02")["people"]], ["0002"])
        self.assertEqual([p["participant_id"] for p in self.present()["people"]], ["0001"])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_binding_lapses_only_after_the_reporter_stops(self):
        clock = Clock()
        with patch.object(presence_service, "_clock", clock):
            self.observe(faces=[{"bbox": [0, 0, 64, 48]}])
            clock.now += presence_service.REPORTER_LEASE_SECONDS - 0.5
            self.refused(409, tab="tab-2")
            clock.now += 1                                                   # the first station has been silent past the lease
            self.assertTrue(self.observe(tab="tab-2")["accepted"])           # e.g. the station PC was replaced
            self.refused(409, tab="tab-1")

    def test_node_must_be_online_and_a_local_agent_node(self):
        node_registry._nodes.pop("CAM-02")                                   # registered, but no station page is running
        self.assertIn("not online", self.refused(409, node_id="CAM-02", faces=[{"bbox": [0, 0, 10, 10]}]))
        node_registry.touch("CAM-02")
        node_registry._nodes["CAM-02"]["last_seen"] -= node_registry.HEARTBEAT_TIMEOUT_SEC + 1   # heartbeats stopped
        self.assertIn("not online", self.refused(409, node_id="CAM-02"))
        self.assertIn("sends its frames to Central", self.refused(409, node_id="MOBILE-01", faces=[{"bbox": [0, 0, 10, 10]}]))
        self.assertEqual(presence_service._nodes, {})
        self.assertEqual(presence_service._reporters, {})                    # a refused report binds nothing
        self.assertEqual(self.presence_events, [])

    def test_a_participant_central_does_not_know_is_counted_as_unknown(self):
        out = self.observe(faces=[{"bbox": [0, 0, 64, 48], "participant_id": "9999", "confidence": 0.9}])
        self.assertEqual((out["faces"], out["unknown"]), (1, 1))
        self.assertEqual(self.present()["people"], [])

    def test_body_limits(self):
        ok = {"node_id": "CAM-01", "image_w": 640, "image_h": 480}
        for bad in ({"image_w": 0}, {"image_h": -1}, {"image_w": 99999}, {"node_id": ""}, {"node_id": "x" * 129},
                    {"faces": [{"bbox": [1, 2, 3]}]}, {"faces": [{"bbox": [1, 2, 3, 4, 5]}]},
                    {"faces": [{"bbox": [0, 0, 1, 1]}] * (presence_service.MAX_FACES + 1)}):
            with self.assertRaises(ValidationError, msg=bad):
                nodes.NodeObservationBody(**{**ok, **bad})
        nodes.NodeObservationBody(**ok, faces=[{"bbox": [0, 0, 1, 1]}] * presence_service.MAX_FACES)
        with self.assertRaises(ValidationError):
            nodes.NodeObservationBody(**ok, reporter_id="x" * 65)

    def test_present_clamps_the_window_and_is_empty_for_a_silent_node(self):
        self.assertEqual(self.present(seconds=500)["seconds"], 30)
        self.assertEqual(self.present(seconds=0)["seconds"], 1)
        quiet = self.present("CAM-02")
        self.assertEqual((quiet["faces_now"], quiet["unknown_now"], quiet["people"], quiet["observed_at"], quiet["image"]),
                         (0, 0, [], None, None))
        self.assertEqual(self.present("never-heard-of")["people"], [])

    def test_presence_is_never_rebuilt_from_history(self):
        self.faces = [face(0)]
        self.central(node_id="MOBILE-01")           # a recorded match: history now has a row for this node
        self.assertEqual(self.counts(), (1, 1, 1))
        presence_service.reset()                    # backend restart
        out = self.present("MOBILE-01", seconds=30)
        self.assertEqual((out["faces_now"], out["people"], out["observed_at"]), (0, [], None))


class CentralNodeTests(RouteCase):
    def big_jpeg(self) -> bytes:
        ok, buf = cv2.imencode(".jpg", np.full((960, 1920, 3), 128, np.uint8))
        return buf.tobytes()

    def test_recorded_scan_reports_boxes_in_the_original_image(self):
        # 1920x960 is detected at half size; boxes must refer to the original.
        self.faces = [DetectedFace(unit(0), (96.0, 48.0, 192.0, 144.0), 0.9), DetectedFace(unit(500), (480.0, 240.0, 960.0, 480.0), 0.9)]
        out, _ = self.central(data=self.big_jpeg(), node_id="MOBILE-01")
        self.assertEqual((out["faces_total"], out["faces_matched"]), (2, 1))
        got = self.present("MOBILE-01")
        self.assertEqual(got["image"], {"w": 1920, "h": 960})
        self.assertEqual((got["faces_now"], got["unknown_now"]), (2, 1))
        self.assertEqual([(p["participant_id"], p["call_as"], p["box"]) for p in got["people"]],
                         [("0001", "Khun Ann", [0.1, 0.1, 0.2, 0.3])])
        self.assertEqual(len(self.published), 1)                       # still exactly one matched event
        self.assertEqual(set(self.published[0]), {"node_id", "participant_id", "name", "activity_id", "checkin", "at", "call_as"})
        self.assertEqual([e["type"] for e in self.presence_events], ["presence"])
        self.assertEqual(self.present("CAM-01")["people"], [])

    def test_response_is_unchanged_by_presence(self):
        self.faces = [face(0)]
        out, _ = self.central(node_id="MOBILE-01")
        self.assertEqual(set(out), {"faces_total", "faces_matched", "faces_unknown", "processing_duration_ms", "threshold_used", "results"})

    def test_persist_false_and_unregistered_nodes_leave_presence_alone(self):
        self.faces = [face(0), face(None, 20)]
        out, _ = self.central(node_id="MOBILE-01", persist=False)
        self.assertIs(out["persisted"], False)
        self.assertEqual(presence_service._nodes, {})
        out, _ = self.central(node_id="GHOST-7")   # recognition still works exactly as before
        self.assertEqual(out["faces_matched"], 1)
        self.assertEqual(presence_service._nodes, {})
        self.assertEqual(self.presence_events, [])

    def test_a_central_scan_cannot_write_into_a_local_agent_cameras_presence(self):
        self.observe(node_id="CAM-01", faces=[{"bbox": [100, 100, 420, 340], "participant_id": "0002", "confidence": 0.7}])
        self.presence_events.clear()
        self.faces = [face(0), face(None, 20)]
        out, _ = self.central(node_id="CAM-01")      # a photo posted under the CCTV camera's name
        self.assertEqual(out["faces_total"], 2)      # recognition itself is unchanged
        got = self.present("CAM-01")
        self.assertEqual(([p["participant_id"] for p in got["people"]], got["faces_now"], got["unknown_now"]), (["0002"], 1, 0))
        self.assertEqual(self.presence_events, [])

    def test_kiosk_upload_does_not_report_presence(self):
        self.faces = [face(0), face(None, 20)]
        self.upload()
        self.assertEqual(presence_service._nodes, {})
        self.assertEqual(self.presence_events, [])


class LookStaysStatelessTests(LookCase):
    def test_look_never_touches_presence(self):
        presence_service.reset()
        self.addCleanup(presence_service.reset)
        p = patch.object(events_feed, "publish_presence", side_effect=AssertionError("presence published"))
        p.start()
        self.addCleanup(p.stop)
        self.faces = [DetectedFace(unit(0), (10.0, 10.0, 100.0, 100.0), 0.9), DetectedFace(unit(500), (200.0, 10.0, 260.0, 90.0), 0.8)]
        for _ in range(5):
            out = self.call(look_jpeg(640, 480), source="CAM-01")
        self.assertEqual(len(out["faces"]), 2)
        self.assertEqual(presence_service._nodes, {})
        self.assertEqual(presence_service._last_event, {})


class FeedOptInTests(unittest.TestCase):
    MATCHED = {"node_id": "CAM-01", "participant_id": "0001", "name": "Ann Alpha", "activity_id": None,
               "checkin": "new", "at": "2026-10-12T10:00:00"}
    PRESENCE = {"type": "presence", "node_id": "CAM-01", "status": "unknown", "faces": 2, "unknown": 1, "at": "2026-10-12T10:00:00"}

    def setUp(self):
        for target in (events_feed._subscribers, events_feed._presence_subscribers):
            target.clear()
            self.addCleanup(target.clear)

    def drain(self, queue) -> list[dict]:
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        return items

    def test_presence_reaches_only_subscribers_that_asked(self):
        async def scenario():
            ordinary, robot, opted = events_feed.subscribe(), events_feed.subscribe(), events_feed.subscribe(presence=True)
            events_feed.publish_presence(self.PRESENCE)
            events_feed.publish(self.MATCHED)
            await asyncio.sleep(0.05)
            got = [self.drain(q) for q in (ordinary, robot, opted)]
            events_feed.unsubscribe(opted)
            events_feed.publish_presence(self.PRESENCE)
            await asyncio.sleep(0.05)
            return got, self.drain(opted)

        (ordinary, robot, opted), after = asyncio.run(scenario())
        self.assertEqual(ordinary, [self.MATCHED])   # existing pages: matched events only, six fields intact
        self.assertEqual(robot, [self.MATCHED])
        self.assertEqual(opted, [self.PRESENCE, self.MATCHED])
        self.assertEqual(after, [])
        self.assertEqual(events_feed._presence_subscribers, set())

    def test_presence_without_subscribers_is_dropped(self):
        events_feed.publish_presence(self.PRESENCE)  # no loop, nobody listening: must not raise

    def ws(self, include):
        calls, sent = [], []

        class Socket:
            async def accept(self):
                pass

            async def close(self, code=1000):
                sent.append(("closed", code))

            async def send_text(self, text):
                sent.append(text)
                raise WebSocketDisconnect()

        real = events_feed.subscribe

        def subscribe(presence=False):
            calls.append(presence)
            queue = real(presence)
            queue.put_nowait({"hello": 1})
            return queue

        with patch.object(nodes, "_ws_user", return_value=True), patch.object(nodes.events_feed, "subscribe", subscribe):
            asyncio.run(nodes.events_feed_ws(Socket(), token="t", include=include))
        return calls, sent

    def test_websocket_opts_in_only_with_include_presence(self):
        self.assertEqual(self.ws(None)[0], [False])
        self.assertEqual(self.ws("")[0], [False])
        self.assertEqual(self.ws("other")[0], [False])
        self.assertEqual(self.ws("presence")[0], [True])
        self.assertEqual(self.ws("other,presence")[0], [True])
        self.assertEqual(events_feed._subscribers, set())  # every socket unsubscribed on disconnect

    def test_websocket_still_requires_a_valid_user(self):
        sent = []

        class Socket:
            async def close(self, code=1000):
                sent.append(code)

        with patch.object(nodes, "_ws_user", return_value=False):
            asyncio.run(nodes.events_feed_ws(Socket(), token=None, include="presence"))
        self.assertEqual(sent, [4401])


class LocalAgentCompatibilityTests(unittest.TestCase):
    """The agent's /recognize answer only GAINS fields; the CCTV page's
    existing use of faces_total and matches is untouched."""

    def recognize(self, faces, data=None):
        index = RecognitionIndex()
        index.rebuild([SimpleNamespace(id="p0", first_name="Ann", last_name="Alpha", participant_id="0001", embedding=unit(0).tobytes())])
        agent = SimpleNamespace(node_id="STATION-A", index=index, threshold=0.45, central_reachable=True)
        app = node_agent.build_app(agent)
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/recognize")
        ok, buf = cv2.imencode(".jpg", np.full((480, 640, 3), 128, np.uint8))
        with patch.object(node_agent, "detect_faces", lambda img: list(faces)):
            return asyncio.run(endpoint(photo=UploadFile(io.BytesIO(data if data is not None else buf.tobytes()), filename="scan.jpg"),
                                        camera_id="STATION-A-cam2"))

    def test_existing_fields_are_unchanged_and_new_ones_describe_every_face(self):
        out = self.recognize([DetectedFace(unit(0), (64.0, 48.0, 320.0, 240.0), 0.9), DetectedFace(unit(500), (400.0, 100.0, 460.0, 180.0), 0.8)])
        self.assertEqual(out["faces_total"], 2)
        self.assertEqual(out["matches"], [{"participant_id": "0001", "name": "Ann Alpha", "confidence": 1.0}])
        self.assertEqual((out["node_id"], out["camera_id"]), ("STATION-A", "STATION-A-cam2"))
        self.assertEqual(set(out), {"faces_total", "matches", "recognition_ms", "node_id", "camera_id", "image", "faces"})
        self.assertEqual(out["image"], {"w": 640, "h": 480})
        self.assertEqual(out["faces"], [
            {"bbox": [64.0, 48.0, 320.0, 240.0], "participant_id": "0001", "confidence": 1.0},
            {"bbox": [400.0, 100.0, 460.0, 180.0], "participant_id": None, "confidence": None},
        ])
        # What the agent now returns is exactly what Central's route accepts.
        body = nodes.NodeObservationBody(node_id="STATION-A-cam2", image_w=out["image"]["w"], image_h=out["image"]["h"], faces=out["faces"])
        self.assertEqual(len(body.faces), 2)

    def test_no_faces_and_bad_image(self):
        out = self.recognize([])
        self.assertEqual((out["faces_total"], out["matches"], out["faces"], out["image"]), (0, [], [], {"w": 640, "h": 480}))
        with self.assertRaises(HTTPException) as ctx:
            self.recognize([], data=b"not an image")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
