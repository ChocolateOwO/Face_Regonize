"""Event Photo retention "Never / ไม่ลบอัตโนมัติ".

Never is a persisted per-batch state (retention_policy "never", delete_at
NULL). Runs the real cleanup, the real status transitions, the real
migration and the real offline conversion against temp databases/storage.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

import app.services.photo_processing_service as pps
from app.api import photo_batches as api
from app.api import settings_routes
from app.auth.security import hash_password
from app.models.models import (
    CleanupLog, Person, PhotoBatch, PhotoBatchFace, PhotoBatchParticipantFolder, PhotoBatchPhoto,
    PhotoBatchReviewItem, Setting, User,
)
from app.services import retention_policy as rp

migration = importlib.import_module("app.migrations.013_retention_never")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import set_retention_never  # noqa: E402

ADMIN = User(id="a1", username="admin", password_hash=hash_password("pw"), role="admin")


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-never-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.db_path = self.root / "t.db"
        self.engine = create_engine(f"sqlite:///{self.db_path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        for target, value in (("engine", self.engine), ("STORAGE_PATH", self.storage),
                              ("PHOTO_BATCHES_DIR", self.storage / "photo_batches")):
            p = patch.object(pps, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("app.services.maintenance.is_active", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def session(self):
        s = Session(self.engine)
        self.addCleanup(s.close)
        return s

    def make_batch(self, batch_id: str, *, status="ready", local_status="READY", workflow_version=2,
                   expired=True, policy=rp.RETENTION_DAYS, with_content=True) -> None:
        storage_dir = f"photo_batches/{batch_id}"
        folder = self.storage / storage_dir
        (folder / "SORTED").mkdir(parents=True)
        (folder / "SORTED" / "p1.jpg").write_bytes(b"photo-bytes")
        (folder / "thumbs").mkdir()
        (folder / "thumbs" / "p1.jpg").write_bytes(b"preview-bytes")
        never = policy == rp.RETENTION_NEVER
        delete_at = None if never else datetime.now() + timedelta(days=-1 if expired else 1)
        with Session(self.engine) as s:
            s.add(PhotoBatch(id=batch_id, label="test", drive_folder_id="", storage_dir=storage_dir, status=status,
                             local_status=local_status, workflow_version=workflow_version, total_photos=1,
                             processed_photos=1, retention_days=0 if never else 7, delete_at=delete_at,
                             retention_policy=policy))
            if with_content:
                photo = PhotoBatchPhoto(batch_id=batch_id, filename="p1.jpg", drive_file_id="",
                                        original_path=f"{storage_dir}/SORTED/p1.jpg")
                s.add(photo)
                s.commit()
                s.refresh(photo)
                s.add(PhotoBatchFace(photo_id=photo.id, bbox="0,0,1,1", confidence=0.9))
                s.add(PhotoBatchParticipantFolder(batch_id=batch_id, person_id="p", folder_id="f", photo_count=1))
                s.add(PhotoBatchReviewItem(batch_id=batch_id, photo_id=photo.id, reason="unknown_face"))
            s.commit()

    def inventory(self, batch_id: str) -> dict:
        with Session(self.engine) as s:
            photos = s.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch_id)).all()
            return {
                "batch": s.get(PhotoBatch, batch_id) is not None,
                "photos": len(photos),
                "faces": sum(len(s.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == p.id)).all()) for p in photos),
                "folders": len(s.exec(select(PhotoBatchParticipantFolder).where(PhotoBatchParticipantFolder.batch_id == batch_id)).all()),
                "reviews": len(s.exec(select(PhotoBatchReviewItem).where(PhotoBatchReviewItem.batch_id == batch_id)).all()),
                "files": sorted(str(p.relative_to(self.storage)).replace("\\", "/")
                                for p in (self.storage / "photo_batches" / batch_id).rglob("*") if p.is_file())
                if (self.storage / "photo_batches" / batch_id).exists() else [],
            }

    def row(self, batch_id: str) -> PhotoBatch:
        with Session(self.engine) as s:
            b = s.get(PhotoBatch, batch_id)
            s.expunge(b)
            return b


class CleanupTests(Case):
    def test_expired_ready_batch_converted_to_never_survives_restart_and_repeated_cleanup(self):
        """The Main situation: ready, deadline already passed, then converted."""
        bid = "a" * 32
        self.make_batch(bid, expired=True)
        before = self.inventory(bid)
        self.assertEqual((before["photos"], before["faces"], before["folders"], before["reviews"]), (1, 1, 1, 1))
        with Session(self.engine) as s:
            set_retention_never.convert(s, existing_batches=True, default=True)
        row = self.row(bid)
        self.assertEqual((row.retention_policy, row.delete_at, row.retention_days), ("never", None, 0))
        for _ in range(3):
            self.assertEqual(pps.run_retention_cleanup(), 0)
        # "Restart": a fresh engine on the same file, as a new process would open it.
        self.engine.dispose()
        fresh = create_engine(f"sqlite:///{self.db_path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(fresh.dispose)
        with patch.object(pps, "engine", fresh):
            self.assertEqual(pps.run_retention_cleanup(), 0)
        self.assertEqual(self.inventory(bid), before, "rows, files, previews and reviews are intact")
        with Session(self.engine) as s:
            self.assertEqual(s.exec(select(CleanupLog)).all(), [])

    def test_never_is_skipped_in_every_state_that_would_otherwise_expire(self):
        states = [("ready", "READY", 2), ("completed", "READY", 2), ("paused", "PROCESSING", 2), ("paused", "CREATED", 1),
                  ("ready", "CREATED", 1), ("completed", "CREATED", 1), ("failed", "FAILED", 2), ("cancelled", "CREATED", 1)]
        for i, (status, local_status, version) in enumerate(states):
            never_id, days_id = f"{i:x}".rjust(32, "e"), f"{i:x}".rjust(32, "d")
            self.make_batch(never_id, status=status, local_status=local_status, workflow_version=version,
                            policy=rp.RETENTION_NEVER, with_content=False)
            self.make_batch(days_id, status=status, local_status=local_status, workflow_version=version,
                            expired=True, with_content=False)
        pps.run_retention_cleanup()
        for i, state in enumerate(states):
            self.assertTrue(self.inventory(f"{i:x}".rjust(32, "e"))["batch"], f"Never batch deleted in state {state}")
            self.assertFalse(self.inventory(f"{i:x}".rjust(32, "d"))["batch"], f"finite expired batch kept in state {state}")

    def test_finite_retention_still_expires_on_schedule(self):
        self.make_batch("1" * 32, expired=True)
        self.make_batch("2" * 32, expired=False)
        self.assertEqual(pps.run_retention_cleanup(), 1)
        self.assertEqual(self.inventory("1" * 32), {"batch": False, "photos": 0, "faces": 0, "folders": 0,
                                                    "reviews": 0, "files": []})
        self.assertTrue(self.inventory("2" * 32)["batch"])
        with Session(self.engine) as s:
            self.assertEqual([log.batch_id for log in s.exec(select(CleanupLog)).all()], ["1" * 32])

    def test_a_missing_deadline_alone_also_protects(self):
        # Defence in depth: policy says days but there is no deadline -> not deletable.
        bid = "3" * 32
        self.make_batch(bid, expired=True)
        with Session(self.engine) as s:
            b = s.get(PhotoBatch, bid)
            b.delete_at = None
            s.add(b)
            s.commit()
        self.assertEqual(pps.run_retention_cleanup(), 0)
        self.assertTrue(self.inventory(bid)["batch"])

    def test_older_code_cleanup_query_cannot_select_a_never_batch(self):
        """Rollback safety: the previous cleanup was `delete_at <= now`."""
        self.make_batch("4" * 32, policy=rp.RETENTION_NEVER)
        self.make_batch("5" * 32, expired=True)
        conn = sqlite3.connect(str(self.db_path))
        rows = [r[0] for r in conn.execute("SELECT id FROM photobatch WHERE delete_at <= ?", (datetime.now().isoformat(" "),))]
        conn.close()
        self.assertEqual(rows, ["5" * 32])


class LifecycleTests(Case):
    def create_never(self) -> str:
        with Session(self.engine) as s, patch.object(pps.settings_cache, "get_use_concurrent_pipeline", return_value=True):
            batch = pps.create_local_batch(s, "experiment", 0, "a1", retention=rp.RETENTION_NEVER)
            return batch.id

    def test_new_never_batch_has_no_deadline_and_survives_pause_resume_ready_restart(self):
        bid = self.create_never()
        row = self.row(bid)
        self.assertEqual((row.retention_policy, row.delete_at, row.retention_days), ("never", None, 0))
        # Processing -> pause -> resume -> ready, the fields a real run moves.
        steps = [("processing", "PROCESSING"), ("pausing", "PROCESSING"), ("paused", "PROCESSING"),
                 ("resuming", "PROCESSING"), ("processing", "PROCESSING"), ("ready", "READY")]
        for status, local_status in steps:
            with Session(self.engine) as s:
                b = s.get(PhotoBatch, bid)
                b.status = status
                pps._set_new_axis_local_status(b, local_status)
                s.add(b)
                s.commit()
            self.assertEqual(pps.run_retention_cleanup(), 0)
            row = self.row(bid)
            self.assertEqual((row.retention_policy, row.delete_at), ("never", None), f"after {status}")
        self.engine.dispose()  # restart
        self.assertEqual(pps.run_retention_cleanup(), 0)
        self.assertTrue(self.inventory(bid)["batch"])

    def test_finite_batch_creation_unchanged(self):
        with Session(self.engine) as s, patch.object(pps.settings_cache, "get_use_concurrent_pipeline", return_value=True):
            batch = pps.create_local_batch(s, "x", 3, "a1")
            self.assertEqual((batch.retention_policy, batch.retention_days), ("days", 3))
            self.assertEqual(batch.delete_at, batch.retention_start_at + timedelta(days=3))
            with self.assertRaises(ValueError):
                pps.create_local_batch(s, "x", 8, "a1")
            with self.assertRaises(ValueError):
                pps.create_local_batch(s, "x", 0, "a1")

    def test_change_retention_transitions(self):
        bid = "6" * 32
        self.make_batch(bid, expired=False, with_content=False)
        with Session(self.engine) as s:
            b = s.get(PhotoBatch, bid)
            b.retention_start_at = datetime.now() - timedelta(days=30)
            s.add(b)
            s.commit()
            old_start = b.retention_start_at
            b = pps.change_retention(s, b, None, rp.RETENTION_NEVER)
            self.assertEqual((b.retention_policy, b.delete_at, b.retention_days), ("never", None, 0))
            self.assertEqual(b.retention_start_at, old_start)
            # never -> days: the period starts now, so it cannot already be expired.
            b = pps.change_retention(s, b, 2, rp.RETENTION_DAYS)
            self.assertEqual((b.retention_policy, b.retention_days), ("days", 2))
            self.assertGreater(b.retention_start_at, old_start)
            self.assertGreater(b.delete_at, datetime.now() + timedelta(days=1, hours=23))
            self.assertEqual(pps.run_retention_cleanup(), 0)
            # days -> days keeps the anchor, as before.
            anchor = b.retention_start_at
            b = pps.change_retention(s, b, 5)
            self.assertEqual((b.retention_start_at, b.delete_at), (anchor, anchor + timedelta(days=5)))
            with self.assertRaises(ValueError):
                pps.change_retention(s, b, 9)
            with self.assertRaises(ValueError):
                pps.change_retention(s, b, 3, "forever")


class SettingAndApiTests(Case):
    def test_setting_defaults_saves_never_and_rejects_garbage(self):
        s = self.session()
        self.assertEqual(settings_routes.get_settings(session=s, user=ADMIN)["photo_retention_default"], "7")
        settings_routes.update_settings(settings_routes.SettingsUpdate(values={"photo_retention_default": "never"}),
                                        session=s, user=ADMIN)
        self.assertEqual(settings_routes.get_settings(session=s, user=ADMIN)["photo_retention_default"], "never")
        self.assertEqual(rp.default_for_new_batches(s), ("never", 0))
        for bad in ("0", "8", "forever", ""):
            with self.assertRaises(HTTPException) as ctx:
                settings_routes.update_settings(settings_routes.SettingsUpdate(values={"photo_retention_default": bad}),
                                                session=s, user=ADMIN)
            self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(rp.default_for_new_batches(s), ("never", 0), "a rejected value changed nothing")

    def test_request_resolution_explicit_choice_beats_default(self):
        s = self.session()
        self.assertEqual(rp.resolve_request(s, None, None), ("days", 7))
        s.add(Setting(key=rp.DEFAULT_SETTING_KEY, value="never"))
        s.commit()
        self.assertEqual(rp.resolve_request(s, None, None), ("never", 0))
        self.assertEqual(rp.resolve_request(s, "days", 3), ("days", 3))
        self.assertEqual(rp.resolve_request(s, None, 2), ("days", 2))
        self.assertEqual(rp.resolve_request(s, "never", 5), ("never", 0))
        with self.assertRaises(rp.RetentionError):
            rp.resolve_request(s, "days", None)
        with self.assertRaises(rp.RetentionError):
            rp.resolve_request(s, None, 0)

    def test_changing_the_default_later_never_touches_existing_batches(self):
        self.make_batch("7" * 32, policy=rp.RETENTION_NEVER, with_content=False)
        self.make_batch("8" * 32, expired=False, with_content=False)
        finite_deadline = self.row("8" * 32).delete_at
        s = self.session()
        for value in ("1", "never", "3"):
            settings_routes.update_settings(settings_routes.SettingsUpdate(values={"photo_retention_default": value}),
                                            session=s, user=ADMIN)
            self.assertEqual(pps.run_retention_cleanup(), 0)
        never, finite = self.row("7" * 32), self.row("8" * 32)
        self.assertEqual((never.retention_policy, never.delete_at), ("never", None))
        self.assertEqual((finite.retention_policy, finite.delete_at), ("days", finite_deadline))

    def test_batch_output_and_per_batch_change_endpoint(self):
        bid = "9" * 32
        self.make_batch(bid, expired=False, with_content=False)
        with Session(self.engine) as s:
            s.add(User(id="a1", username="admin", password_hash=ADMIN.password_hash, role="admin"))
            s.commit()
        s = self.session()
        user = s.get(User, "a1")
        with self.assertRaises(HTTPException) as ctx:
            api.update_retention(bid, api.RetentionChangeBody(password="wrong", retention_policy="never"), session=s, user=user)
        self.assertEqual(ctx.exception.status_code, 401)
        out = api.update_retention(bid, api.RetentionChangeBody(password="pw", retention_policy="never"), session=s, user=user)
        self.assertEqual((out["retention_policy"], out["delete_at"], out["retention_days"]), ("never", None, 0))
        out = api.update_retention(bid, api.RetentionChangeBody(password="pw", retention_days=4), session=s, user=user)
        self.assertEqual((out["retention_policy"], out["retention_days"]), ("days", 4))
        self.assertIsNotNone(out["delete_at"])
        with self.assertRaises(HTTPException) as ctx:
            api.update_retention(bid, api.RetentionChangeBody(password="pw", retention_days=0), session=s, user=user)
        self.assertEqual(ctx.exception.status_code, 400)


class ConversionScriptTests(Case):
    def test_dry_run_changes_nothing_and_real_run_only_touches_retention(self):
        self.make_batch("a" * 32, expired=True)
        self.make_batch("b" * 32, expired=False, status="processing", local_status="PROCESSING")
        with Session(self.engine) as s:
            s.add(Person(participant_id="0001", first_name="Ann", image_path="", embedding=b""))
            s.commit()
        conn = sqlite3.connect(str(self.db_path))
        tables = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
        counts = lambda: {t: conn.execute(f"select count(*) from [{t}]").fetchone()[0] for t in tables}  # noqa: E731
        other = lambda: conn.execute(  # noqa: E731
            "select id,label,status,local_status,total_photos,processed_photos,storage_dir,created_at,retention_start_at "
            "from photobatch order by id").fetchall()
        before_counts, before_other = counts(), other()
        with Session(self.engine) as s:
            report = set_retention_never.convert(s, existing_batches=True, default=True, dry_run=True)
        self.assertEqual(len(report["batches"]), 2)
        self.assertEqual(conn.execute("select count(*) from photobatch where retention_policy='never'").fetchone()[0], 0)
        self.assertIsNone(conn.execute("select value from setting where key='photo_retention_default'").fetchone())

        with Session(self.engine) as s:
            set_retention_never.convert(s, existing_batches=True, default=True)
        self.assertEqual(conn.execute("select retention_policy, delete_at, retention_days from photobatch").fetchall(),
                         [("never", None, 0), ("never", None, 0)])
        self.assertEqual(conn.execute("select value from setting where key='photo_retention_default'").fetchone()[0], "never")
        after_counts = counts()
        self.assertEqual({k: v for k, v in after_counts.items() if k != "setting"},
                         {k: v for k, v in before_counts.items() if k != "setting"})
        self.assertEqual(after_counts["setting"], before_counts["setting"] + 1)
        self.assertEqual(other(), before_other, "nothing but the retention fields changed")
        # Idempotent.
        with Session(self.engine) as s:
            again = set_retention_never.convert(s, existing_batches=True, default=True)
        self.assertTrue(all(b["already_never"] for b in again["batches"]))
        conn.close()


OLD_PHOTOBATCH_DDL = """CREATE TABLE photobatch (
	id VARCHAR NOT NULL,
	label VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	total_photos INTEGER NOT NULL,
	retention_days INTEGER NOT NULL,
	retention_start_at DATETIME NOT NULL,
	delete_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	created_by VARCHAR, failed_photos INTEGER DEFAULT 0, storage_dir TEXT DEFAULT '', local_status TEXT NOT NULL DEFAULT 'CREATED', workflow_version INTEGER NOT NULL DEFAULT 1, processing_started_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by) REFERENCES user (id)
)"""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-never-mig-")
        self.addCleanup(self.temp.cleanup)
        self.conn = sqlite3.connect(str(Path(self.temp.name) / "old.db"))
        self.addCleanup(self.conn.close)
        self.conn.executescript(f"""
            CREATE TABLE user (id VARCHAR PRIMARY KEY);
            {OLD_PHOTOBATCH_DDL};
            CREATE INDEX ix_photobatch_status ON photobatch (status);
            CREATE TABLE photobatchphoto (id VARCHAR PRIMARY KEY, batch_id VARCHAR NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES photobatch (id));
            INSERT INTO user VALUES ('u1');
            INSERT INTO photobatch (id,label,status,total_photos,retention_days,retention_start_at,delete_at,created_at,
                                    created_by,failed_photos,storage_dir,local_status,workflow_version)
              VALUES ('b1','test','ready',183,7,'2026-09-30 01:30:00','2026-10-07 01:30:00','2026-09-30 01:30:00',
                      'u1',0,'photo_batches/b1','READY',2),
                     ('b2','งาน','processing',916,3,'2026-10-01 00:00:00','2026-10-04 00:00:00','2026-10-01 00:00:00',
                      NULL,2,'photo_batches/b2','PROCESSING',2);
            INSERT INTO photobatchphoto VALUES ('p1','b1'),('p2','b1'),('p3','b2');
        """)

    def columns(self):
        return {r[1]: r for r in self.conn.execute("PRAGMA table_info(photobatch)")}

    def test_upgrade_preserves_every_row_and_value_and_is_idempotent(self):
        before = self.conn.execute("SELECT * FROM photobatch ORDER BY id").fetchall()
        self.assertEqual(self.columns()["delete_at"][3], 1)
        for _ in range(2):
            migration.upgrade(self.conn)
            self.conn.commit()
        cols = self.columns()
        self.assertEqual(cols["delete_at"][3], 0, "delete_at is nullable now")
        self.assertEqual((cols["retention_policy"][3], cols["retention_policy"][4]), (1, "'days'"))
        self.assertEqual((cols["retention_days"][3], cols["workflow_version"][4], cols["storage_dir"][4]), (1, "1", "''"))
        after = self.conn.execute("SELECT * FROM photobatch ORDER BY id").fetchall()
        self.assertEqual([row[:-1] for row in after], before, "every pre-existing column value is unchanged")
        self.assertEqual([row[-1] for row in after], ["days", "days"], "no batch's retention was changed")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM photobatchphoto").fetchone()[0], 3)
        self.assertIn("REFERENCES photobatch", self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='photobatchphoto'").fetchone()[0])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_photobatch_status'").fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE '%013_rebuild%'").fetchone())
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        # The point of it: a Never row can now be stored.
        self.conn.execute("UPDATE photobatch SET retention_policy='never', delete_at=NULL, retention_days=0 WHERE id='b1'")
        self.conn.commit()

    def test_a_failed_rebuild_leaves_the_original_table_untouched(self):
        before = self.conn.execute("SELECT * FROM photobatch ORDER BY id").fetchall()
        with patch.object(migration, "_deadline_is_not_null", side_effect=[True, True]):
            # Second call (the post-rebuild check) claims it is still NOT NULL -> must roll back.
            with self.assertRaises(RuntimeError):
                migration.upgrade(self.conn)
        self.assertEqual(self.conn.execute("PRAGMA table_info(photobatch)").fetchall()[6][3], 1, "still the old table")
        self.assertEqual([r[:-1] for r in self.conn.execute("SELECT * FROM photobatch ORDER BY id")], before)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM photobatchphoto").fetchone()[0], 3)

    def test_fresh_schema_needs_no_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{(Path(tmp) / 'n.db').as_posix()}")
            SQLModel.metadata.create_all(engine)
            engine.dispose()
            conn = sqlite3.connect(str(Path(tmp) / "n.db"))
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='photobatch'").fetchone()[0]
            migration.upgrade(conn)
            self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE name='photobatch'").fetchone()[0], ddl)
            conn.close()


if __name__ == "__main__":
    unittest.main()
