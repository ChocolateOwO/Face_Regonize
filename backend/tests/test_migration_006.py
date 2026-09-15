"""Phase E1 — migration 006 (review persistence) + the two new review tables.

Exercises the raw sqlite3 upgrade() directly (matching how runner.py calls
it), the full runner against a fresh DB, and the already-migrated case that
matters most here: production has run 001-005, so 006 must be a safe no-op
when re-run and must never disturb existing face rows.
"""
import importlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine, select

import app.models.models  # noqa: F401 - registers every table on SQLModel.metadata
from app.models.models import (
    PhotoBatch,
    PhotoBatchFace,
    PhotoBatchPhoto,
    PhotoBatchReviewDecision,
    PhotoBatchReviewItem,
)

m006 = importlib.import_module("app.migrations.006_review_persistence")
runner = importlib.import_module("app.migrations.runner")


def _make_face_table(conn: sqlite3.Connection) -> None:
    """photobatchface as it exists BEFORE 006 (i.e. what production has)."""
    conn.execute(
        """CREATE TABLE photobatchface (
            id TEXT PRIMARY KEY,
            photo_id TEXT NOT NULL,
            person_id TEXT,
            confidence REAL NOT NULL,
            bbox TEXT NOT NULL,
            consent_status_at_processing TEXT NOT NULL DEFAULT 'unknown',
            blurred BOOLEAN NOT NULL DEFAULT 0
        )"""
    )


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


class Migration006ColumnTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        _make_face_table(self.conn)

    def test_adds_manual_mask_column(self):
        self.assertNotIn("manual_mask", _columns(self.conn, "photobatchface"))
        m006.upgrade(self.conn)
        self.assertIn("manual_mask", _columns(self.conn, "photobatchface"))

    def test_is_idempotent(self):
        m006.upgrade(self.conn)
        m006.upgrade(self.conn)  # must not raise on an already-migrated DB
        m006.upgrade(self.conn)
        cols = [row[1] for row in self.conn.execute("PRAGMA table_info(photobatchface)")]
        self.assertEqual(cols.count("manual_mask"), 1)

    def test_existing_faces_get_null_not_false(self):
        """NULL means 'no reviewer decision'; 0 would fabricate a confirmation
        nobody made, and would be indistinguishable from a real one."""
        self.conn.execute(
            "INSERT INTO photobatchface (id, photo_id, confidence, bbox) VALUES ('f1', 'p1', 0.9, '1,2,3,4')"
        )
        m006.upgrade(self.conn)
        value = self.conn.execute("SELECT manual_mask FROM photobatchface WHERE id='f1'").fetchone()[0]
        self.assertIsNone(value)

    def test_existing_face_data_is_untouched(self):
        self.conn.execute(
            "INSERT INTO photobatchface (id, photo_id, person_id, confidence, bbox, "
            "consent_status_at_processing, blurred) VALUES ('f1','p1','per1',0.77,'5,6,7,8','declined',1)"
        )
        m006.upgrade(self.conn)
        row = self.conn.execute(
            "SELECT photo_id, person_id, confidence, bbox, consent_status_at_processing, blurred "
            "FROM photobatchface WHERE id='f1'"
        ).fetchone()
        self.assertEqual(row, ("p1", "per1", 0.77, "5,6,7,8", "declined", 1))

    def test_column_accepts_all_three_states(self):
        m006.upgrade(self.conn)
        for face_id, value in (("a", None), ("b", 1), ("c", 0)):
            self.conn.execute(
                "INSERT INTO photobatchface (id, photo_id, confidence, bbox, manual_mask) VALUES (?,?,?,?,?)",
                (face_id, "p1", 0.5, "1,2,3,4", value),
            )
        stored = dict(self.conn.execute("SELECT id, manual_mask FROM photobatchface"))
        self.assertEqual(stored, {"a": None, "b": 1, "c": 0})


class Migration006RunnerTests(unittest.TestCase):
    """The runner creates tables via create_all() BEFORE applying migrations,
    so on a fresh database the new review tables appear that way and 006's
    ALTER is a no-op — both paths must end at the same schema."""

    def test_full_runner_on_fresh_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fresh.db"
            engine = create_engine(f"sqlite:///{db.as_posix()}")
            SQLModel.metadata.create_all(engine)
            engine.dispose()

            conn = sqlite3.connect(db)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("photobatchreviewitem", tables)
                self.assertIn("photobatchreviewdecision", tables)
                self.assertIn("manual_mask", _columns(conn, "photobatchface"))
                m006.upgrade(conn)  # no-op on a fresh, model-created schema
                self.assertIn("manual_mask", _columns(conn, "photobatchface"))
            finally:
                conn.close()  # Windows will not remove the temp dir while it is open

    def test_migration_is_registered_in_order(self):
        """006 must exist and sort after 005. It is deliberately NOT asserted
        to be the last migration — later phases add their own."""
        directory = Path(runner.__file__).parent
        names = sorted(p.stem for p in directory.glob("[0-9][0-9][0-9]_*.py"))
        self.assertIn("006_review_persistence", names)
        self.assertIn("005_output_completeness", names)
        self.assertGreater(names.index("006_review_persistence"), names.index("005_output_completeness"))


class ReviewTableShapeTests(unittest.TestCase):
    """The schema must actually support what the Review workflow needs."""

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        with Session(self.engine) as s:
            batch = PhotoBatch(label="b", drive_folder_id="", storage_dir="d")
            s.add(batch)
            s.commit()
            s.refresh(batch)
            photo = PhotoBatchPhoto(batch_id=batch.id, filename="a.jpg", drive_file_id="", original_path="o")
            s.add(photo)
            s.commit()
            s.refresh(photo)
            face = PhotoBatchFace(photo_id=photo.id, confidence=0.9, bbox="1,2,3,4")
            s.add(face)
            s.commit()
            s.refresh(face)
            self.batch_id, self.photo_id, self.face_id = batch.id, photo.id, face.id

    def test_review_item_defaults_to_unresolved(self):
        with Session(self.engine) as s:
            item = PhotoBatchReviewItem(batch_id=self.batch_id, photo_id=self.photo_id, reason="score_ambiguous")
            s.add(item)
            s.commit()
            s.refresh(item)
            self.assertIsNone(item.resolved_at)
            self.assertIsNone(item.resolved_by)
            self.assertIsNotNone(item.created_at)

    def test_decision_records_both_outcomes_and_is_append_only_in_shape(self):
        """A failed attempt must be recordable — that is what stops the audit
        trail claiming an assignment that never reached the files."""
        with Session(self.engine) as s:
            item = PhotoBatchReviewItem(batch_id=self.batch_id, photo_id=self.photo_id, reason="manual")
            s.add(item)
            s.commit()
            s.refresh(item)
            for outcome in ("applied", "failed"):
                s.add(PhotoBatchReviewDecision(
                    review_item_id=item.id, photo_id=self.photo_id, face_id=self.face_id,
                    action="assign", previous_person_id=None, new_person_id="per1",
                    consent_snapshot_at_decision="declined", outcome=outcome,
                ))
            s.commit()
            rows = s.exec(select(PhotoBatchReviewDecision)).all()
        self.assertEqual({r.outcome for r in rows}, {"applied", "failed"})
        self.assertEqual(len(rows), 2, "both attempts are retained; nothing overwrites")

    def test_manual_mask_tri_state_round_trips(self):
        with Session(self.engine) as s:
            face = s.get(PhotoBatchFace, self.face_id)
            self.assertIsNone(face.manual_mask, "no reviewer decision by default")
            face.manual_mask = True
            s.add(face)
            s.commit()
        with Session(self.engine) as s:
            self.assertIs(s.get(PhotoBatchFace, self.face_id).manual_mask, True)
            face = s.get(PhotoBatchFace, self.face_id)
            face.manual_mask = False
            s.add(face)
            s.commit()
        with Session(self.engine) as s:
            self.assertIs(s.get(PhotoBatchFace, self.face_id).manual_mask, False)

    def test_archive_membership_is_derivable_without_a_second_flag(self):
        """REVIEW-archive membership = 'a review item exists for this photo',
        resolved or not. Resolving must NOT remove the photo from the archive."""
        with Session(self.engine) as s:
            item = PhotoBatchReviewItem(batch_id=self.batch_id, photo_id=self.photo_id, reason="tile_conflict")
            s.add(item)
            s.commit()

            def in_archive(photo_id):
                return s.exec(
                    select(PhotoBatchReviewItem).where(PhotoBatchReviewItem.photo_id == photo_id)
                ).first() is not None

            self.assertTrue(in_archive(self.photo_id))
            item = s.exec(select(PhotoBatchReviewItem)).first()
            item.resolved_at = item.created_at
            s.add(item)
            s.commit()
            self.assertTrue(in_archive(self.photo_id), "resolving must not drop archive membership")

    def test_no_separate_membership_table_was_introduced(self):
        """The plan decided membership derives from PhotoBatchFace/ReviewItem;
        a junction table would be second state that could drift."""
        tables = set(SQLModel.metadata.tables)
        self.assertNotIn("photobatchphotoperson", tables)


if __name__ == "__main__":
    unittest.main()
