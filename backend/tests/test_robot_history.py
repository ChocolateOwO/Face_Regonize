"""Phase 4 — call_as, history provenance and the write-order cursor."""
from __future__ import annotations

import importlib
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import BackgroundTasks, HTTPException, UploadFile
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import history, nodes, people, recognition
from app.auth import deps
from app.face_recognition.engine import DetectedFace
from app.face_recognition.index import RecognitionIndex
from app.models.models import FaceDetection, Person, Upload, User
from app.services import look_service, person_address, settings_cache, storage_service

ADMIN = User(id="a1", username="admin", password_hash="x", role="admin")
migration = importlib.import_module("app.migrations.012_robot_api")


def unit(i):
    v = np.zeros(512, np.float32)
    v[i] = 1
    return v


class DbCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-robot-hist-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine(f"sqlite:///{(self.root / 't.db').as_posix()}",
                                    connect_args={"check_same_thread": False, "timeout": 30})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

    def session(self):
        s = Session(self.engine)
        self.addCleanup(s.close)
        return s

    def add_detection(self, upload_id="u", person_id=None, **kw):
        with Session(self.engine) as s:
            d = FaceDetection(upload_id=upload_id, person_id=person_id, confidence=0.5, bbox="0,0,1,1",
                              status="matched" if person_id else "unknown", **kw)
            s.add(d)
            s.commit()
            s.refresh(d)
            return d


class MigrationTests(unittest.TestCase):
    def test_upgrade_on_pre_012_schema_is_additive_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(str(Path(tmp) / "old.db"))
            conn.executescript("""
                CREATE TABLE person (id VARCHAR PRIMARY KEY, participant_id VARCHAR, first_name VARCHAR);
                CREATE TABLE upload (id VARCHAR PRIMARY KEY, filename VARCHAR);
                CREATE TABLE facedetection (id VARCHAR PRIMARY KEY, upload_id VARCHAR, person_id VARCHAR);
                INSERT INTO person VALUES ('p1','0001','Ann'),('p2','0002','Ben');
                INSERT INTO upload VALUES ('u1','MOBILE-01_scan.jpg');
                INSERT INTO facedetection VALUES ('f1','u1','p1'),('f2','u1',NULL);
            """)
            for _ in range(2):
                migration.upgrade(conn)
                conn.commit()
            cols = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in ("person", "upload", "facedetection")}
            self.assertIn("call_as", cols["person"])
            self.assertTrue({"source_kind", "source_node_id"} <= set(cols["upload"]))
            self.assertIn("seq", cols["facedetection"])
            self.assertEqual([conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("person", "upload", "facedetection")],
                             [2, 1, 2])
            self.assertEqual(conn.execute("SELECT call_as FROM person").fetchall(), [(None,), (None,)])
            self.assertEqual(conn.execute("SELECT source_kind, source_node_id FROM upload").fetchall(), [(None, None)])
            self.assertEqual(conn.execute("SELECT seq FROM facedetection").fetchall(), [(None,), (None,)])
            self.assertIn("AUTOINCREMENT", conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='historysequence'").fetchone()[0])
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_facedetection_seq'").fetchone())
            conn.close()

    def test_fresh_schema_matches_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{(Path(tmp) / 'n.db').as_posix()}")
            SQLModel.metadata.create_all(engine)
            engine.dispose()
            conn = sqlite3.connect(str(Path(tmp) / "n.db"))
            migration.upgrade(conn)  # no-op on a fresh schema, must not fail
            self.assertIn("AUTOINCREMENT", conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='historysequence'").fetchone()[0])
            conn.close()


class SequenceTests(DbCase):
    def test_monotonic_never_reused_after_delete_all(self):
        seqs = [self.add_detection().seq for _ in range(5)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), 5)
        history.delete_all_history(session=self.session(), user=ADMIN)
        self.assertGreater(self.add_detection().seq, max(seqs))

    def test_concurrent_writers_get_unique_seq_in_commit_order(self):
        errors = []

        def writer():
            try:
                for _ in range(15):
                    self.add_detection()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        conn = sqlite3.connect(str(self.root / "t.db"))
        rows = conn.execute("SELECT rowid, seq FROM facedetection ORDER BY rowid").fetchall()
        conn.close()
        self.assertEqual(len(rows), 60)
        seqs = [r[1] for r in rows]
        self.assertEqual(seqs, sorted(seqs))  # insertion (write) order == seq order
        self.assertEqual(len(set(seqs)), 60)


class CursorTests(DbCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as s:
            self.person = Person(participant_id="0001", first_name="Ann", last_name="Alpha", image_path="",
                                 embedding=b"", call_as="พี่แอน")
            s.add(self.person)
            s.add(Upload(id="old", filename="x", image_path=""))
            s.add(Upload(id="u1", filename="MOBILE-01_scan.jpg", image_path="", source_kind="node_central", source_node_id="MOBILE-01"))
            s.commit()
            s.refresh(self.person)
        # Two pre-existing rows written before seq existed: bypass the listener.
        conn = sqlite3.connect(str(self.root / "t.db"))
        for i in range(2):
            conn.execute("INSERT INTO facedetection (id, upload_id, person_id, confidence, bbox, detected_at, status) "
                         "VALUES (?, 'old', NULL, 0.1, '0,0,1,1', ?, 'unknown')", (f"old{i}", datetime(2026, 1, 1).isoformat(" ")))
        conn.commit()
        conn.close()
        self.new = [self.add_detection(upload_id="u1", person_id=self.person.id) for _ in range(5)]

    def test_cursor_mode_pages_forward_and_skips_unsequenced_rows(self):
        out = history.recognition_history(page_size=20, after_seq=0, session=self.session(), user=ADMIN)
        self.assertEqual([i["seq"] for i in out["items"]], [d.seq for d in self.new])
        self.assertEqual((out["total"], out["latest_seq"], out["next_after_seq"]), (5, self.new[-1].seq, self.new[-1].seq))
        first = self.new[0]
        item = out["items"][0]
        self.assertEqual((item["node_id"], item["source"], item["call_as"], item["name"], item["participant_id"]),
                         ("MOBILE-01", "node_central", "พี่แอน", "Ann Alpha", "0001"))
        self.assertEqual(item["id"], first.id)

        page = history.recognition_history(page_size=20, after_seq=self.new[2].seq, session=self.session(), user=ADMIN)
        self.assertEqual([i["seq"] for i in page["items"]], [d.seq for d in self.new[3:]])
        empty = history.recognition_history(page_size=20, after_seq=self.new[-1].seq, session=self.session(), user=ADMIN)
        self.assertEqual((empty["items"], empty["next_after_seq"]), ([], self.new[-1].seq))

    def test_default_mode_unchanged_and_old_rows_unknown(self):
        out = history.recognition_history(page_size=20, session=self.session(), user=ADMIN)
        self.assertEqual(out["total"], 7)
        self.assertNotIn("next_after_seq", out)
        self.assertEqual(out["latest_seq"], self.new[-1].seq)
        old = [i for i in out["items"] if i["id"].startswith("old")]
        self.assertEqual({(i["seq"], i["node_id"], i["source"]) for i in old}, {(None, None, "unknown")})
        for key in ("id", "upload_id", "upload_filename", "person_id", "name", "participant_id",
                    "confidence", "status", "detected_at"):
            self.assertIn(key, out["items"][0])
        dates = [i["detected_at"] for i in out["items"]]
        self.assertEqual(dates, sorted(dates, reverse=True))


class CursorUnderConcurrencyTests(DbCase):
    """The robot polls GET /api/history?after_seq=N while recognitions are
    being written. It must receive every committed row exactly once, in
    order, page after page - even while a writer holds an open transaction
    whose seq is already allocated, and when a transaction rolls back."""

    def test_a_polling_reader_sees_every_committed_row_once_in_order(self):
        import random
        import time

        errors, committed, done = [], [], threading.Event()
        lock = threading.Lock()

        def writer(n: int):
            rng = random.Random(n)
            try:
                for t in range(30):
                    with Session(self.engine) as s:
                        s.add(Upload(id=f"w{n}-{t}", filename="x", image_path=""))
                        rows = [FaceDetection(upload_id=f"w{n}-{t}", person_id=None, confidence=0.5, bbox="0,0,1,1", status="unknown")
                                for _ in range(rng.randint(1, 3))]
                        for row in rows:
                            s.add(row)
                            s.flush()                       # seq allocated now, inside the open transaction
                            time.sleep(rng.random() * 0.004)  # ... while the reader keeps polling
                        if t % 7 == 3:
                            s.rollback()                    # allocated, never committed
                            continue
                        s.commit()
                        with lock:
                            committed.extend(r.seq for r in rows)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        seen, pages = [], []

        def reader():
            cursor = 0
            try:
                while True:
                    finished = done.is_set()
                    with Session(self.engine) as s:
                        out = history.recognition_history(page_size=20, after_seq=cursor, session=s, user=ADMIN)
                    batch = [i["seq"] for i in out["items"]]
                    pages.append(batch)
                    seen.extend(batch)
                    cursor = out["next_after_seq"]
                    if finished and not batch:
                        return
                    if not batch:
                        time.sleep(0.002)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        poll = threading.Thread(target=reader)
        poll.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        done.set()
        poll.join(60)
        self.assertFalse(poll.is_alive())
        self.assertEqual(errors, [])
        self.assertGreater(len(committed), 150)
        self.assertEqual(seen, sorted(committed), "every committed row, once, in seq order")
        self.assertEqual(len(seen), len(set(seen)))
        self.assertGreater(sum(1 for p in pages if p), 10, "the reader really interleaved with the writers")
        for page in pages:
            self.assertLessEqual(len(page), 20)


class CallAsTests(DbCase):
    def test_normalize(self):
        n = person_address.normalize_call_as
        self.assertEqual(n("  พี่   บอส "), "พี่ บอส")
        self.assertIsNone(n("   "))
        self.assertIsNone(n(None))
        for bad in ("a\nb", "a​b", "x" * 41):
            with self.assertRaises(person_address.CallAsError):
                n(bad)

    def test_patch_is_admin_only_and_put_keeps_it(self):
        with Session(self.engine) as s:
            p = Person(participant_id="0001", first_name="Ann", last_name="A", image_path="", embedding=b"")
            s.add(p)
            s.commit()
            s.refresh(p)
        out = people.set_call_as(p.id, people.CallAsBody(call_as=" พี่แอน "), session=self.session(), user=ADMIN)
        self.assertEqual(out["call_as"], "พี่แอน")
        with self.assertRaises(HTTPException) as ctx:
            people.set_call_as(p.id, people.CallAsBody(call_as="a\nb"), session=self.session(), user=ADMIN)
        self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(HTTPException) as ctx:
            people.set_call_as("missing", people.CallAsBody(call_as="x"), session=self.session(), user=ADMIN)
        self.assertEqual(ctx.exception.status_code, 404)
        for role in ("viewer", "machine"):
            with self.assertRaises(HTTPException) as ctx:
                deps.require_admin(User(username=role, password_hash="x", role=role))
            self.assertEqual(ctx.exception.status_code, 403)
        # The existing full-form PUT (name/email only) must not clear it.
        people.update_person(p.id, first_name="Annie", last_name="", email=None, photo=None,
                             session=self.session(), user=ADMIN)
        with Session(self.engine) as s:
            self.assertEqual(s.get(Person, p.id).call_as, "พี่แอน")
        out = people.set_call_as(p.id, people.CallAsBody(call_as=""), session=self.session(), user=ADMIN)
        self.assertIsNone(out["call_as"])


class ExposureTests(DbCase):
    """call_as and provenance reach /api/look, recognition results, the feed and Upload rows."""

    def setUp(self):
        super().setUp()
        with Session(self.engine) as s:
            p = Person(participant_id="0001", first_name="Ann", last_name="Alpha", image_path="",
                       embedding=unit(0).tobytes(), call_as="พี่แอน")
            s.add(p)
            s.commit()
            s.refresh(p)
            self.pid = p.id
        index = RecognitionIndex()
        index.rebuild([SimpleNamespace(id=self.pid, first_name="Ann", last_name="Alpha", participant_id="0001",
                                       embedding=unit(0).tobytes())])
        self.published = []
        storage = self.root / "storage"
        (storage / "thumbnails").mkdir(parents=True)
        faces = [DetectedFace(unit(0), (1.0, 1.0, 9.0, 9.0), 0.9)]
        for target, name, value in [
            (recognition, "recognition_index", index), (recognition, "engine", self.engine),
            (recognition, "detect_faces", lambda img, timings=None: list(faces)),
            (look_service, "recognition_index", index), (look_service, "db_engine", self.engine),
            (look_service.engine, "is_ready", lambda: True),
            (look_service.engine, "detect_faces", lambda img, timings=None, lock_timeout=None: list(faces)),
            (storage_service, "STORAGE_PATH", storage), (storage_service, "EVENTS_DIR", storage / "events"),
            (storage_service, "THUMBNAILS_DIR", storage / "thumbnails"),
        ]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(recognition.events_feed, "publish", side_effect=self.published.append)
        p.start()
        self.addCleanup(p.stop)
        settings_cache.set_threshold(0.45)
        settings_cache.set_debug_mode(False)
        self.jpeg = cv2.imencode(".jpg", np.zeros((16, 16, 3), np.uint8))[1].tobytes()

    def test_look_includes_call_as(self):
        out = look_service.look(self.jpeg)
        self.assertEqual(out["faces"][0]["call_as"], "พี่แอน")

    def test_kiosk_and_central_record_provenance_and_call_as(self):
        for node in (None, "MOBILE-01"):
            tasks = BackgroundTasks()
            with Session(self.engine) as s:
                if node is None:
                    out = recognition.recognize(tasks, photo=UploadFile(io.BytesIO(self.jpeg), filename="k.jpg"),
                                                debug=None, activity_id=None, persist=True, session=s, user=ADMIN)
                else:
                    out = nodes.recognize_central(tasks, node_id=node, photo=UploadFile(io.BytesIO(self.jpeg), filename="c.jpg"),
                                                  activity_id=None, persist=True, session=s, user=ADMIN)
                for t in tasks.tasks:
                    t.func(*t.args, **t.kwargs)
            self.assertEqual(out["results"][0]["call_as"], "พี่แอน")
        with Session(self.engine) as s:
            uploads = s.exec(select(Upload).order_by(Upload.uploaded_at)).all()
            self.assertEqual([(u.source_kind, u.source_node_id) for u in uploads],
                             [("kiosk", "kiosk"), ("node_central", "MOBILE-01")])
            self.assertTrue(all(d.seq for d in s.exec(select(FaceDetection)).all()))
        self.assertEqual([(e["node_id"], e["call_as"]) for e in self.published],
                         [("kiosk", "พี่แอน"), ("MOBILE-01", "พี่แอน")])
        for e in self.published:
            self.assertEqual(set(e), {"node_id", "participant_id", "name", "activity_id", "checkin", "at", "call_as"})

    def test_node_event_records_provenance(self):
        body = '{"node_id": "CAM-02", "camera_label": "C920", "matches": [{"participant_id": "0001", "confidence": 0.8}]}'
        with Session(self.engine) as s:
            with patch.object(nodes.events_feed, "publish", side_effect=self.published.append):
                out = nodes.node_event(body=body, image=None, session=s, user=ADMIN)
        self.assertEqual(out["results"][0]["call_as"], "พี่แอน")
        with Session(self.engine) as s:
            u = s.exec(select(Upload)).one()
            self.assertEqual((u.source_kind, u.source_node_id), ("node_event", "CAM-02"))
        self.assertEqual((self.published[-1]["node_id"], self.published[-1]["call_as"]), ("CAM-02", "พี่แอน"))


if __name__ == "__main__":
    unittest.main()
