"""Workstream 1 — recognition without saving (persist=false) and the single
live-feed event per recognize-central match.

Handlers are called directly (no HTTP client in this environment); the
BackgroundTasks they queue are executed by hand, exactly as Starlette would
after sending the response. Detection is faked; the matcher, storage,
check-in logic and database writes are the real ones against a temp DB."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import nodes, recognition
from app.face_recognition.engine import DetectedFace
from app.face_recognition.index import RecognitionIndex
from app.models.models import Activity, Attendance, FaceDetection, Person, Setting, Upload, User
from app.services import node_registry, settings_cache, storage_service

USER = User(id="u1", username="admin", password_hash="x")


def unit(i: int) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    v[i] = 1.0
    return v


def jpeg(w: int = 64, h: int = 48) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((h, w, 3), 128, np.uint8))
    assert ok
    return buf.tobytes()


def face(i: int | None, x: float = 0.0, size: float = 10.0) -> DetectedFace:
    # i=None -> an embedding orthogonal to every enrolled person (unknown).
    emb = unit(i) if i is not None else unit(500)
    return DetectedFace(emb, (x, 0.0, x + size, size), 0.9)


class PersistCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-persist-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.engine = create_engine(f"sqlite:///{(root / 't.db').as_posix()}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        self.storage = root / "storage"
        for name, value in {"STORAGE_PATH": self.storage, "EVENTS_DIR": self.storage / "events",
                            "THUMBNAILS_DIR": self.storage / "thumbnails"}.items():
            p = patch.object(storage_service, name, value)
            p.start()
            self.addCleanup(p.stop)
        (self.storage / "thumbnails").mkdir(parents=True)
        p = patch.object(recognition, "engine", self.engine)
        p.start()
        self.addCleanup(p.stop)

        with Session(self.engine) as s:
            self.people = []
            for i, (first, last) in enumerate([("Ann", "Alpha"), ("Ben", "Beta")]):
                person = Person(participant_id=f"{i + 1:04d}", first_name=first, last_name=last,
                                image_path="", embedding=unit(i).tobytes())
                s.add(person)
                s.commit()
                s.refresh(person)
                self.people.append(SimpleNamespace(id=person.id, first_name=first, last_name=last,
                                                   participant_id=person.participant_id,
                                                   embedding=person.embedding))
        index = RecognitionIndex()
        index.rebuild(self.people)
        p = patch.object(recognition, "recognition_index", index)
        p.start()
        self.addCleanup(p.stop)
        settings_cache.set_threshold(0.45)
        settings_cache.set_debug_mode(False)

        self.published: list[dict] = []
        for module in (recognition, nodes):
            p = patch.object(module.events_feed, "publish", side_effect=self.published.append)
            p.start()
            self.addCleanup(p.stop)
        self.faces: list[DetectedFace] = []
        p = patch.object(recognition, "detect_faces", side_effect=lambda img, timings=None: list(self.faces))
        p.start()
        self.addCleanup(p.stop)
        node_registry._nodes.clear()
        self.addCleanup(node_registry._nodes.clear)

    # --- helpers -----------------------------------------------------------
    def counts(self) -> tuple[int, int, int]:
        with Session(self.engine) as s:
            return tuple(len(s.exec(select(m)).all()) for m in (Upload, FaceDetection, Attendance))

    def stored_files(self) -> list[str]:
        return sorted(str(p.relative_to(self.storage)) for p in self.storage.rglob("*") if p.is_file())

    def run_tasks(self, tasks: BackgroundTasks) -> None:
        for task in tasks.tasks:
            task.func(*task.args, **task.kwargs)

    def upload(self, data: bytes | None = None, **kw):
        # Direct calls bypass FastAPI's parameter resolution, so the Form()
        # defaults must be supplied explicitly.
        kw.setdefault("activity_id", None)
        kw.setdefault("persist", True)
        tasks = BackgroundTasks()
        with Session(self.engine) as s:
            out = recognition.recognize(tasks, photo=UploadFile(io.BytesIO(data or jpeg()), filename="scan.jpg"),
                                        debug=None, session=s, user=USER, **kw)
            self.run_tasks(tasks)
        return out, tasks

    def central(self, data: bytes | None = None, node_id: str = "MOBILE-01", **kw):
        kw.setdefault("activity_id", None)
        kw.setdefault("persist", True)
        tasks = BackgroundTasks()
        with Session(self.engine) as s:
            out = nodes.recognize_central(tasks, node_id=node_id,
                                          photo=UploadFile(io.BytesIO(data or jpeg()), filename="scan.jpg"),
                                          session=s, user=USER, **kw)
            self.run_tasks(tasks)
        return out, tasks


class NoPersistTests(PersistCase):
    def assert_nothing_kept(self, tasks: BackgroundTasks):
        self.assertEqual(tasks.tasks, [])
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.published, [])
        self.assertEqual(node_registry._nodes, {})

    def test_matched_unknown_and_same_person_twice(self):
        self.faces = [face(0), face(None, 20), face(1, 40), face(0, 60)]
        for call in (self.upload, self.central):
            out, tasks = call(persist=False, activity_id=None)
            self.assert_nothing_kept(tasks)
            self.assertIs(out["persisted"], False)
            self.assertEqual((out["faces_total"], out["faces_matched"], out["faces_unknown"]), (4, 3, 1))
            statuses = [r["status"] for r in out["results"]]
            self.assertEqual(statuses, ["matched", "unknown", "matched", "matched"])
            matched = [r for r in out["results"] if r["status"] == "matched"]
            self.assertEqual({r["checkin_status"] for r in matched}, {"not_recorded"})
            self.assertEqual([r["first_name"] for r in matched], ["Ann", "Ben", "Ann"])
            self.assertEqual(matched[0]["name"], "Ann Alpha")
            self.assertNotIn("person_id", out["results"][1])

    def test_zero_faces(self):
        self.faces = []
        out, tasks = self.upload(persist=False)
        self.assert_nothing_kept(tasks)
        self.assertEqual((out["faces_total"], out["results"], out["persisted"]), (0, [], False))

    def test_invalid_image_is_400_and_writes_nothing(self):
        for call in (self.upload, self.central):
            with self.assertRaises(HTTPException) as ctx:
                call(data=b"not an image", persist=False)
            self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.stored_files(), [])

    def test_activity_is_not_validated_or_used(self):
        # A deleted/unknown activity would be a 404 for a real check-in; an
        # observe-only call does not check anyone in, so it must not care.
        self.faces = [face(0)]
        out, tasks = self.upload(persist=False, activity_id="no-such-activity")
        self.assert_nothing_kept(tasks)
        self.assertEqual(out["results"][0]["checkin_status"], "not_recorded")

    def test_no_attendance_read_happens(self):
        self.faces = [face(0)]
        with patch.object(recognition, "_checked_in_today", side_effect=AssertionError("read")):
            self.upload(persist=False)


class DefaultBehaviourTests(PersistCase):
    def test_default_upload_unchanged(self):
        self.faces = [face(0), face(None, 20)]
        out, _ = self.upload()
        self.assertEqual(set(out), {"faces_total", "faces_matched", "faces_unknown",
                                    "processing_duration_ms", "threshold_used", "results"})
        self.assertEqual(out["results"][0]["checkin_status"], "new")
        self.assertEqual(set(out["results"][1]), {"bbox", "confidence", "status"})
        self.assertEqual(self.counts(), (1, 2, 1))
        self.assertEqual(len([f for f in self.stored_files() if f.startswith("events")]), 1)
        self.assertEqual([(e["node_id"], e["participant_id"], e["checkin"]) for e in self.published],
                         [("kiosk", "0001", "new")])
        # The six original keys plus the additive call_as (Phase 4).
        self.assertEqual(set(self.published[0]), {"node_id", "participant_id", "name", "activity_id", "checkin", "at", "call_as"})

        self.published.clear()
        out, _ = self.upload()
        self.assertEqual(out["results"][0]["checkin_status"], "already_checked_in")
        self.assertEqual(self.counts(), (2, 4, 1))
        self.assertEqual([e["checkin"] for e in self.published], ["already"])

    def test_default_is_scoped_per_activity(self):
        with Session(self.engine) as s:
            food, gadget = Activity(name="Food"), Activity(name="Gadget")
            s.add(food)
            s.add(gadget)
            s.commit()
            ids = food.id, gadget.id
        self.faces = [face(1)]
        first, _ = self.upload(activity_id=ids[0])
        second, _ = self.upload(activity_id=ids[1])
        again, _ = self.upload(activity_id=ids[1])
        self.assertEqual([r["results"][0]["checkin_status"] for r in (first, second, again)],
                         ["new", "new", "already_checked_in"])
        self.assertEqual(self.counts()[2], 2)

    def test_default_rejects_unknown_activity(self):
        self.faces = [face(0)]
        with self.assertRaises(HTTPException) as ctx:
            self.upload(activity_id="gone")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_recognize_central_publishes_once_with_real_node_id(self):
        self.faces = [face(0), face(1, 20), face(None, 40)]
        out, _ = self.central()
        self.assertNotIn("persisted", out)
        self.assertEqual(sorted((e["node_id"], e["participant_id"]) for e in self.published),
                         [("MOBILE-01", "0001"), ("MOBILE-01", "0002")])
        self.assertTrue(all(e["checkin"] == "new" for e in self.published))
        self.assertEqual(self.counts(), (1, 3, 2))
        self.assertEqual(node_registry._nodes["MOBILE-01"]["faces_seen"], 3)
        with Session(self.engine) as s:
            self.assertEqual(s.exec(select(Upload)).one().filename, "MOBILE-01_scan.jpg")

    def test_recognize_central_second_scan_is_already(self):
        self.faces = [face(0)]
        self.central()
        self.published.clear()
        out, _ = self.central()
        self.assertEqual(out["results"][0]["checkin_status"], "already_checked_in")
        self.assertEqual([(e["node_id"], e["checkin"]) for e in self.published], [("MOBILE-01", "already")])


class OpenApiTests(unittest.TestCase):
    def test_persist_is_declared_on_both_routes(self):
        app = FastAPI()
        app.include_router(recognition.router)
        app.include_router(nodes.router)
        spec = app.openapi()
        for path in ("/api/recognition/upload", "/api/node/recognize-central"):
            params = {p["name"]: p for p in spec["paths"][path]["post"].get("parameters", [])}
            self.assertIn("persist", params, path)
            self.assertEqual(params["persist"]["in"], "query")
            self.assertIs(params["persist"]["schema"]["default"], True)


if __name__ == "__main__":
    unittest.main()
