"""Phase F1 / Binding Amendment A4 — proof that the separate-metadata design
works before any capture code depends on it.

Pins, on temporary databases only:
  * create_all() on the default metadata never creates the identity tables;
  * migration 010 creates them on every real database shape — fresh, Dummy
    (007 already applied, old create_all-made sample table), production
    (008 present, no 007) — applying 007/009/010 in the runner's order;
  * the migration DDL matches the ORM models column for column;
  * ORM writes, reads, cross-metadata joins, the uniqueness constraint,
    mapper configuration and init_db() startup all work.
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.migrations import runner
from app.models import models
from app.models.identity_models import EventIdentityCandidate, EventIdentitySample, identity_metadata
from app.models.models import Person

IDENTITY_TABLES = {"eventidentitysample", "eventidentitycandidate"}

# Verbatim from the Dummy database: the table create_all() made under 007.
LEGACY_SAMPLE_DDL = (
    "CREATE TABLE eventidentitysample (\n\tid VARCHAR NOT NULL, \n\tperson_id VARCHAR NOT NULL, \n\t"
    "batch_id VARCHAR, \n\tsource VARCHAR NOT NULL, \n\ttrust VARCHAR NOT NULL, \n\tembedding BLOB NOT NULL, \n\t"
    "pose_bucket VARCHAR NOT NULL, \n\tquality_score FLOAT NOT NULL, \n\tface_pixel_area INTEGER NOT NULL, \n\t"
    "created_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(person_id) REFERENCES person (id), \n\t"
    "FOREIGN KEY(batch_id) REFERENCES photobatch (id)\n)"
)


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table) -> set[tuple]:
    return {(r[1], r[2].upper(), bool(r[3]), bool(r[5])) for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn, table) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name NOT LIKE 'sqlite_autoindex%'",
        (table,))}


class DbCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-identity-meta-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "app.db"
        self.engine = create_engine(f"sqlite:///{self.db.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        # Production-real resolved path check (A2): this must never be a real DB.
        self.assertIn("reconize-identity-meta-", str(self.db))

    def core_schema(self, registry_ids: list[str]):
        """What init_db's create_all() produces, plus a chosen registry."""
        SQLModel.metadata.create_all(self.engine)
        conn = sqlite3.connect(self.db)
        names = {mid: name for mid, name, _ in runner._discover()}
        conn.executemany("INSERT INTO schemamigration (id, name, applied_at) VALUES (?, ?, '2026-01-01')",
                         [(i, names[i]) for i in registry_ids])
        conn.commit()
        conn.close()

    def migrate(self) -> list[str]:
        with patch.object(runner, "DATABASE_PATH", self.db):
            return runner.run_pending_migrations()

    def conn(self):
        c = sqlite3.connect(self.db)
        self.addCleanup(c.close)
        return c


class MetadataSeparationTests(DbCase):
    def test_create_all_on_the_default_metadata_never_creates_identity_tables(self):
        SQLModel.metadata.create_all(self.engine)
        self.assertFalse(IDENTITY_TABLES & _tables(self.conn()))
        self.assertFalse(IDENTITY_TABLES & set(SQLModel.metadata.tables))
        self.assertEqual(IDENTITY_TABLES, set(identity_metadata.tables))

    def test_the_default_models_module_no_longer_registers_the_sample(self):
        self.assertFalse(hasattr(models, "EventIdentitySample"))

    def test_mappers_configure_across_both_registries(self):
        from sqlalchemy.orm import configure_mappers

        configure_mappers()  # raises if any mapping/FK/relationship is broken


class MigrationShapeTests(DbCase):
    def assert_identity_schema(self):
        conn = self.conn()
        self.assertTrue(IDENTITY_TABLES <= _tables(conn))
        # The migration DDL is the schema of record; it must equal the models.
        ref = Path(self.temp.name) / "ref.db"
        ref_engine = create_engine(f"sqlite:///{ref.as_posix()}")
        try:
            identity_metadata.create_all(ref_engine)
        finally:
            ref_engine.dispose()
        ref_conn = sqlite3.connect(ref)
        try:
            for table in IDENTITY_TABLES:
                self.assertEqual(_columns(conn, table), _columns(ref_conn, table), table)
                self.assertEqual(_indexes(conn, table), _indexes(ref_conn, table), table)
        finally:
            ref_conn.close()

    def test_fresh_database(self):
        SQLModel.metadata.create_all(self.engine)
        applied = self.migrate()
        self.assertEqual(applied, [mid for mid, _, _ in runner._discover()])
        self.assert_identity_schema()

    def test_dummy_shape_with_007_applied_and_the_old_sample_table(self):
        self.core_schema(["001", "002", "003", "004", "005", "006", "007", "008", "009"])
        conn = sqlite3.connect(self.db)
        conn.execute(LEGACY_SAMPLE_DDL)
        conn.execute("CREATE INDEX ix_eventidentitysample_person_id ON eventidentitysample (person_id)")
        conn.execute("CREATE INDEX ix_eventidentitysample_batch_id ON eventidentitysample (batch_id)")
        conn.execute("INSERT INTO eventidentitysample VALUES ('s1','p1',NULL,'event_harvested','trusted',x'00',"
                     "'unknown',0.5,100,'2026-01-01')")
        conn.commit()
        conn.close()

        self.assertEqual(self.migrate(), ["010"])
        self.assert_identity_schema()
        row = self.conn().execute("SELECT id, person_id, source_candidate_id FROM eventidentitysample").fetchall()
        self.assertEqual(row, [("s1", "p1", None)], "an existing row is preserved, never rewritten")

    def test_production_shape_with_008_and_no_007(self):
        self.core_schema(["001", "002", "003", "004", "005", "006", "008"])
        applied = self.migrate()
        self.assertEqual(applied, ["007", "009", "010"], "applied in the runner's order")
        self.assert_identity_schema()
        cols = {r[1] for r in self.conn().execute("PRAGMA table_info(photobatchface)")}
        self.assertIn("detected_bbox", cols)
        registry = [r[0] for r in self.conn().execute("SELECT id FROM schemamigration ORDER BY id")]
        self.assertEqual(registry, ["001", "002", "003", "004", "005", "006", "007", "008", "009", "010"])

    def test_re_running_is_a_no_op(self):
        SQLModel.metadata.create_all(self.engine)
        self.migrate()
        self.assertEqual(self.migrate(), [])
        self.assert_identity_schema()


class OrmTests(DbCase):
    def setUp(self):
        super().setUp()
        SQLModel.metadata.create_all(self.engine)
        self.migrate()

    def candidate(self, **overrides) -> EventIdentityCandidate:
        values = dict(batch_id="b1", photo_id="ph1", face_index=0, capture_bucket="matched",
                      selection_key="00ff", bbox="1,2,3,4", extractor_version="v1", embedding=b"\0" * 2048)
        values.update(overrides)
        return EventIdentityCandidate(**values)

    def test_write_read_and_join_across_metadata(self):
        with Session(self.engine) as session:
            person = Person(participant_id="0001", first_name="A", last_name="B", image_path="", embedding=b"")
            session.add(person)
            session.commit()
            session.refresh(person)
            session.add(self.candidate(predicted_person_id=person.id))
            session.add(EventIdentitySample(person_id=person.id, source="event_harvested", embedding=b"\0" * 2048,
                                            extractor_version="v1"))
            session.commit()

            joined = session.exec(
                select(EventIdentityCandidate, Person).join(Person, Person.id == EventIdentityCandidate.predicted_person_id)
            ).all()
            self.assertEqual(len(joined), 1)
            self.assertEqual(joined[0][1].participant_id, "0001")
            self.assertEqual(session.exec(select(EventIdentitySample)).one().extractor_version, "v1")

    def test_the_same_face_and_extractor_cannot_be_captured_twice(self):
        with Session(self.engine) as session:
            session.add(self.candidate())
            session.commit()
            session.add(self.candidate())
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_a_different_extractor_version_is_a_separate_candidate(self):
        with Session(self.engine) as session:
            session.add(self.candidate())
            session.add(self.candidate(extractor_version="v2"))
            session.commit()
            self.assertEqual(len(session.exec(select(EventIdentityCandidate)).all()), 2)


class StartupTests(DbCase):
    def test_init_db_creates_the_identity_tables_through_the_migration(self):
        from app.database import db

        with patch.object(db, "engine", self.engine), patch.object(runner, "DATABASE_PATH", self.db):
            db.init_db()
        conn = self.conn()
        self.assertTrue(IDENTITY_TABLES <= _tables(conn))
        self.assertEqual(conn.execute("SELECT id FROM schemamigration WHERE id='010'").fetchone(), ("010",))


if __name__ == "__main__":
    unittest.main()
