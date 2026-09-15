"""Pause processing, Resume, and a separate Delete.

Built on the existing cancellation machinery (_cancelled_batches,
_active_batches, the PipelineRegistry handle and the edit/ZIP/upload
reservations). Every test runs on a temp DB and temp storage.
"""
import hashlib
from pathlib import Path
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select

import app.services.event_pipeline_service as eps
import app.services.photo_processing_service as pps
from app.models.identity_models import EventIdentityCandidate, identity_metadata
from app.models.models import ConsentRecord, Person, PhotoBatch, PhotoBatchFace, PhotoBatchParticipantFolder, PhotoBatchPhoto
from app.services import batch_edit_lock
from app.services import drive_destination_service as dds
from app.services.event_pipeline_registry import registry as pipeline_registry
from app.services.photo_batch_download_service import local_output_ready
from tests.test_event_photo_completeness import CompletenessTestCase, _png_bytes

OTHER = "b" * 32


def _tree_digest(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class PauseCase(CompletenessTestCase):
    def setUp(self):
        super().setUp()
        identity_metadata.create_all(self.engine)
        # Candidate capture stamps the extractor version by loading the real
        # face model — slow and GPU-contended; irrelevant to Pause/Resume.
        # The pipeline's worker profile probes real ONNX providers — same story.
        from app.services import hardware_info_service, identity_candidate_service
        for p in (patch.object(identity_candidate_service, "extractor_version", return_value="test-extractor"),
                  patch.object(hardware_info_service, "_verified_providers", return_value=["CPUExecutionProvider"]),
                  patch.object(hardware_info_service, "_gpus_via_nvidia_smi", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        for bid in (self.batch_id, OTHER):
            for container in (pps._cancelled_batches, pps._pause_waiters, pps._active_batches):
                self.addCleanup(container.discard, bid)
            self.addCleanup(pps._pause_requested_at.pop, bid, None)
            self.addCleanup(pps._heartbeats.pop, bid, None)
            self.addCleanup(dds._release_upload, bid)
            self.addCleanup(batch_edit_lock.release_edit, bid)
        self.detect_impl = lambda img: []
        self.detect_calls = 0

    def set_batch(self, **fields):
        with Session(self.engine) as s:
            b = s.get(PhotoBatch, self.batch_id)
            for k, v in fields.items():
                setattr(b, k, v)
            s.add(b)
            s.commit()

    def patch_source(self, n: int):
        files = [SimpleNamespace(id=f"drive-p{i:02d}.png", name=f"p{i:02d}.png") for i in range(n)]
        data = {f"p{i:02d}.png": _png_bytes(i) for i in range(n)}

        def detect(img):
            self.detect_calls += 1
            return self.detect_impl(img)

        self.list_mock = self.start(patch.object(pps, "list_image_files", return_value=files))
        self.download_mock = self.start(patch.object(
            pps, "download_file", side_effect=lambda fid: data[fid.removeprefix("drive-")]))
        self.start(patch.object(pps, "detect_faces", side_effect=detect))
        self.start(patch.object(eps, "detect_event_faces", side_effect=detect))

    def start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    @property
    def media_dir(self):
        return self.photo_batches_dir / self.batch_id / "MEDIA"

    def seed_paused(self, total: int, done: int, staged: int | None = None) -> list[str]:
        """A paused batch exactly as Pause leaves it: `total` source items,
        `done` of them finalized (row + MEDIA), and the first `staged` items
        already promoted to ORIGINAL/ (default: all of them)."""
        staged = total if staged is None else staged
        root = self.photo_batches_dir / self.batch_id
        for sub_dir in ("ORIGINAL", "MEDIA", "SORTED", "AMBIENCE"):
            (root / sub_dir).mkdir(parents=True, exist_ok=True)
        names = [f"p{i:02d}.png" for i in range(total)]
        for i, name in enumerate(names[:staged]):
            (root / "ORIGINAL" / name).write_bytes(_png_bytes(i))
        with Session(self.engine) as s:
            for i, name in enumerate(names[:done]):
                (root / "MEDIA" / name).write_bytes(_png_bytes(i))
                s.add(PhotoBatchPhoto(batch_id=self.batch_id, filename=name, drive_file_id=f"drive-{name}",
                                      original_path=f"photo_batches/{self.batch_id}/ORIGINAL/{name}",
                                      media_path=f"photo_batches/{self.batch_id}/MEDIA/{name}",
                                      classification="ambience"))
            b = s.get(PhotoBatch, self.batch_id)
            b.status, b.total_photos, b.processed_photos = "paused", total, done
            s.add(b)
            s.commit()
        return names

    def pausing_detector(self, pause_on_call: int, gate: threading.Event | None = None):
        calls = {"n": 0}

        def detect(img):
            calls["n"] += 1
            if calls["n"] == pause_on_call:
                pps.request_pause(self.batch_id)
                if gate is not None:
                    gate.wait(timeout=20)
            return []

        self.detect_impl = detect
        return calls

    def start_run(self, target=None) -> threading.Thread:
        t = threading.Thread(target=target or pps.run_photo_batch, args=(self.batch_id,), daemon=True)
        t.start()
        return t

    def finish_pause(self, thread: threading.Thread | None = None):
        if thread is not None:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "the run must exit after Pause")
        self.assertTrue(pps.wait_for_pause(self.batch_id, timeout=30), "Pause must complete")

    def resume_and_wait(self):
        self.detect_impl = lambda img: []
        self.assertEqual(pps.request_resume(self.batch_id), {"status": "resuming", "resuming": True})
        self.assertTrue(pps.wait_until_idle(self.batch_id, timeout=60))

    def media_files(self):
        media = self.photo_batches_dir / self.batch_id / "MEDIA"
        return sorted(p.name for p in media.iterdir()) if media.exists() else []

    def photo_names(self):
        return [p.filename for p in self.photos()]

    def wait_status(self, status, timeout=10):
        deadline = time.time() + timeout
        while self.batch().status != status and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.batch().status, status)

    def assert_no_worker_leak(self):
        self.assertNotIn(self.batch_id, pps._active_batches)
        self.assertNotIn(self.batch_id, pps._pause_waiters)
        self.assertFalse(pipeline_registry.is_active(self.batch_id))
        alive = [t.name for t in threading.enumerate()
                 if t.name.startswith(("fetch-", "decode-", "inference-", "render-",
                                       f"pause-{self.batch_id[:8]}", f"resume-{self.batch_id[:8]}"))]
        self.assertEqual(alive, [], "no orphan worker thread")

    def assert_paused(self):
        b = self.batch()
        self.assertEqual(b.status, "paused")
        self.assertNotEqual(b.local_status, "READY")
        self.assertFalse(local_output_ready(b, self.storage), "a paused batch is never exportable")
        self.assertIn("Processing paused", b.current_stage)
        self.assert_no_worker_leak()


class PauseResumeTests(PauseCase):
    def test_pause_during_detection_keeps_completed_work_then_resume_finishes(self):
        self.patch_source(6)
        self.pausing_detector(2)
        self.finish_pause(self.start_run())
        self.assert_paused()
        paused = self.batch()
        self.assertEqual(paused.processed_photos, 2, "the photo in progress reached its checkpoint; nothing after it")
        self.assertEqual(self.media_files(), ["p00.png", "p01.png"])
        self.assertEqual(len(list((self.photo_batches_dir / self.batch_id / "ORIGINAL").iterdir())), 2)

        self.resume_and_wait()
        b = self.batch()
        self.assertEqual((b.status, b.processed_photos, b.total_photos, b.failed_photos), ("ready", 6, 6, 0))
        self.assertEqual(sorted(self.photo_names()), [f"p{i:02d}.png" for i in range(6)], "no duplicate rows")
        self.assertEqual(self.media_files(), [f"p{i:02d}.png" for i in range(6)], "no duplicate files")
        self.assertEqual(b.processing_started_at, paused.processing_started_at, "resume keeps the original start")
        self.assert_no_worker_leak()

    def test_pause_with_queued_pipeline_work_then_resume_finishes(self):
        self.set_batch(workflow_version=2)
        self.patch_source(12)
        calls = self.pausing_detector(3)
        self.finish_pause(self.start_run())
        self.assert_paused()
        done = self.batch().processed_photos
        self.assertLess(done, 12, "queued work was not processed")
        self.assertLess(calls["n"], 12)
        self.resume_and_wait()
        b = self.batch()
        self.assertEqual((b.status, b.local_status, b.processed_photos), ("ready", "READY", 12))
        self.assertEqual(sorted(self.photo_names()), [f"p{i:02d}.png" for i in range(12)])
        self.assertEqual(len(self.media_files()), 12)
        self.assert_no_worker_leak()

    def test_pause_during_source_staging_before_the_run_starts(self):
        self.set_batch(status="pending")
        self.patch_source(3)
        self.assertEqual(pps.request_pause(self.batch_id)["status"], "pausing")
        self.finish_pause()
        self.assertEqual(self.batch().status, "paused")
        pps.run_photo_batch(self.batch_id)  # the scheduled task starts afterwards
        self.assertEqual((self.batch().status, self.batch().total_photos), ("paused", 0), "paused never starts itself")
        self.resume_and_wait()
        self.assertEqual((self.batch().status, self.batch().processed_photos), ("ready", 3))

    def test_pause_while_a_retry_is_active(self):
        for sub in ("ORIGINAL", "SORTED", "AMBIENCE", "MEDIA"):
            (self.photo_batches_dir / self.batch_id / sub).mkdir(parents=True, exist_ok=True)
        original_dir = self.photo_batches_dir / self.batch_id / "ORIGINAL"
        with Session(self.engine) as s:
            for i in range(4):
                (original_dir / f"r{i}.png").write_bytes(_png_bytes(10 + i))
                s.add(PhotoBatchPhoto(batch_id=self.batch_id, filename=f"r{i}.png", drive_file_id="",
                                      original_path=f"photo_batches/{self.batch_id}/ORIGINAL/r{i}.png"))
            b = s.get(PhotoBatch, self.batch_id)
            b.status, b.total_photos, b.failed_photos = "needs_retry", 4, 4
            s.add(b)
            s.commit()
        self.patch_source(0)
        self.pausing_detector(1)
        self.finish_pause(self.start_run(pps.retry_unresolved_photos))
        self.assert_paused()
        b = self.batch()
        self.assertEqual((b.processed_photos, b.failed_photos), (1, 3))

    def test_repeated_pause_is_idempotent(self):
        gate = threading.Event()
        self.patch_source(4)
        self.pausing_detector(1, gate)
        run = self.start_run()
        self.wait_status("pausing")
        self.assertEqual(pps.request_pause(self.batch_id), {"status": "pausing", "pausing": True})
        waiters = [t for t in threading.enumerate() if t.name == f"pause-{self.batch_id[:8]}"]
        self.assertEqual(len(waiters), 1, "a second press never starts a second pause task")
        gate.set()
        self.finish_pause(run)
        self.assertEqual(pps.request_pause(self.batch_id), {"status": "paused", "pausing": False})

    def test_resume_twice_starts_one_run(self):
        self.patch_source(5)
        self.pausing_detector(1)
        self.finish_pause(self.start_run())
        gate = threading.Event()
        self.detect_impl = lambda img: (gate.wait(timeout=20), [])[1]
        self.assertEqual(pps.request_resume(self.batch_id), {"status": "resuming", "resuming": True})
        second = pps.request_resume(self.batch_id)
        self.assertFalse(second["resuming"])
        self.assertIn(second["status"], ("resuming", "processing"))
        self.assertEqual(len([t for t in threading.enumerate() if t.name == f"resume-{self.batch_id[:8]}"]), 1)
        gate.set()
        self.assertTrue(pps.wait_until_idle(self.batch_id, timeout=60))
        self.assertEqual((self.batch().status, self.batch().processed_photos), ("ready", 5))
        self.assertEqual(len(self.photo_names()), 5, "no duplicate rows")

    def test_resume_is_refused_for_terminal_or_unknown_batches(self):
        self.set_batch(status="ready")
        with self.assertRaises(pps.ResumeRefused):
            pps.request_resume(self.batch_id)
        self.assertEqual(pps.request_resume("f" * 32)["status"], "not_found")
        self.assertEqual(pps.request_pause(self.batch_id), {"status": "ready", "pausing": False})

    def test_pause_survives_a_restart_and_resumes_after_it(self):
        self.patch_source(4)
        self.pausing_detector(2)
        self.finish_pause(self.start_run())
        done = self.batch().processed_photos
        # a new process starts with empty in-memory state
        for container in (pps._cancelled_batches, pps._active_batches, pps._pause_waiters):
            container.discard(self.batch_id)
        pps._heartbeats.pop(self.batch_id, None)
        pps.run_photo_batch(self.batch_id)
        pps.retry_unresolved_photos(self.batch_id)
        self.assertEqual((self.batch().status, self.batch().processed_photos), ("paused", done), "never auto-resumes")
        self.resume_and_wait()
        self.assertEqual((self.batch().status, self.batch().processed_photos), ("ready", 4))

    def test_pause_watchdog_exposes_heartbeat_and_stage(self):
        from app.api.photo_batches import _batch_out

        gate = threading.Event()
        self.patch_source(3)
        self.pausing_detector(1, gate)
        run = self.start_run()
        self.wait_status("pausing")
        with patch.object(pps, "PAUSE_SLOW_SECONDS", 0):
            time.sleep(0.05)
            out = _batch_out(self.batch())
        diag = out["pause_diagnostics"]
        self.assertTrue(diag["slow"])
        self.assertTrue(diag["workers_active"])
        self.assertIsNotNone(diag["last_heartbeat_at"])
        self.assertIsNotNone(diag["pause_requested_at"])
        self.assertIn("Pausing", diag["stage"])
        self.assertEqual((out["status"], out["can_resume"], out["can_delete"]), ("pausing", False, False))
        gate.set()
        self.finish_pause(run)
        self.assertNotIn("pause_diagnostics", _batch_out(self.batch()))

    def test_pause_removes_only_abandoned_temporaries(self):
        root = self.photo_batches_dir / self.batch_id
        keep = ["ORIGINAL/x.png", "MEDIA/x.png", "SORTED/0001_A/x.png", "AMBIENCE/y.png", "THUMBNAILS/x.png.jpg",
                "UPLOAD_STAGING/s.png"]
        drop = ["THUMBNAILS/.x.png.jpg.0a1b.tmp", "MEDIA/.x.png.regen-tmp", "ORIGINAL/z.png.part",
                "UPLOAD_STAGING/half.png.part"]
        for rel in keep + drop:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(b"data")
        self.set_batch(status="pending")
        pps.request_pause(self.batch_id)
        self.finish_pause()
        for rel in keep:
            self.assertTrue((root / rel).exists(), rel)
        for rel in drop:
            self.assertFalse((root / rel).exists(), rel)


class ResumeEfficiencyTests(PauseCase):
    """Resume continues from what staging already produced: no source listing
    or download it does not need, no reprocessing of finished photos, and a
    bulk handled-state lookup instead of one query per photo."""

    def resume_and_finish(self, timeout=60):
        self.assertTrue(pps.request_resume(self.batch_id)["resuming"])
        self.assertTrue(pps.wait_until_idle(self.batch_id, timeout=timeout))

    def test_a_fully_staged_batch_resumes_without_listing_or_downloading(self):
        names = self.seed_paused(total=6, done=2)
        self.patch_source(6)
        finished_before = {n: (self.media_dir / n).read_bytes() for n in names[:2]}

        self.resume_and_finish()

        b = self.batch()
        self.assertEqual((b.status, b.processed_photos, b.total_photos), ("ready", 6, 6))
        self.assertEqual(self.list_mock.call_count, 0, "a fully staged batch never lists the SOURCE again")
        self.assertEqual(self.download_mock.call_count, 0, "staged files are read locally, never downloaded again")
        self.assertEqual(self.detect_calls, 4, "only the pending photos are processed")
        self.assertEqual({n: (self.media_dir / n).read_bytes() for n in names[:2]}, finished_before,
                         "finished outputs are not re-rendered")
        self.assertEqual(sorted(self.photo_names()), names, "no duplicate rows")
        self.assertEqual(sorted(self.media_files()), names, "no duplicate outputs")

    def test_partial_staging_downloads_only_the_files_that_never_arrived(self):
        self.seed_paused(total=6, done=2, staged=4)
        self.patch_source(6)

        self.resume_and_finish()

        self.assertEqual(self.list_mock.call_count, 1, "the listing is what finds items that never arrived")
        self.assertEqual(sorted(c.args[0] for c in self.download_mock.call_args_list),
                         ["drive-p04.png", "drive-p05.png"])
        self.assertEqual((self.batch().status, self.batch().processed_photos), ("ready", 6))
        self.assertEqual(len(self.photo_names()), 6)

    def test_the_pipeline_path_also_resumes_from_staged_files(self):
        self.set_batch(workflow_version=2)
        self.seed_paused(total=6, done=2)
        self.patch_source(6)

        self.resume_and_finish()

        self.assertEqual((self.list_mock.call_count, self.download_mock.call_count), (0, 0))
        self.assertEqual((self.batch().status, self.batch().processed_photos), ("ready", 6))
        self.assertEqual(len(self.photo_names()), 6, "no duplicate rows")

    def test_the_handled_state_lookup_is_two_bulk_queries_not_one_per_photo(self):
        from sqlalchemy import event

        self.seed_paused(total=40, done=40)
        seen: list[str] = []

        def record(conn, cursor, statement, *a):
            if statement.lstrip().upper().startswith("SELECT"):
                seen.append(statement)

        event.listen(self.engine, "before_cursor_execute", record)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record)
        with Session(self.engine) as session:
            handled = pps._already_handled(session, self.batch_id)
        self.assertEqual(len(handled), 40)
        self.assertEqual(len(seen), 2, f"one query per table, not per photo (got {len(seen)})")

    def test_rebuilding_the_pending_queue_for_400_photos_is_fast(self):
        names = self.seed_paused(total=400, done=300)
        with Session(self.engine) as session:
            clock = time.perf_counter()
            handled = pps._already_handled(session, self.batch_id)
            todo = [n for n in names if n not in handled]
            elapsed = time.perf_counter() - clock
        self.assertEqual((len(handled), len(todo)), (300, 100))
        self.assertLess(elapsed, 2.0, f"queue reconstruction took {elapsed:.2f}s")

    def test_the_model_stays_warm_across_pause_and_resume_and_loads_once_per_process(self):
        from app.face_recognition import engine as face_engine

        built = []

        class FakeFaceAnalysis:
            def __init__(self, **kwargs):
                built.append(kwargs)
                self.models = {"recognition": SimpleNamespace(
                    session=SimpleNamespace(get_providers=lambda: ["CUDAExecutionProvider"]))}

            def prepare(self, **kwargs):
                pass

        with patch.object(face_engine, "FaceAnalysis", FakeFaceAnalysis), \
                patch.object(face_engine, "detect_faces", lambda img: []), \
                patch.object(face_engine, "_face_app", None):
            first = face_engine.get_face_app()          # this process's one cold load
            self.patch_source(4)
            self.pausing_detector(2)
            self.finish_pause(self.start_run())
            self.detect_impl = lambda img: []
            self.resume_and_finish()
            second = face_engine.get_face_app()
            self.assertIs(second, first, "Pause never unloads the model")
            self.assertEqual(len(built), 1, "a warm process loads the model exactly once")

            # A backend restart starts from an empty cache: one cold load, once.
            with patch.object(face_engine, "_face_app", None):
                face_engine.get_face_app()
                face_engine.get_face_app()
            self.assertEqual(len(built), 2)

    def test_resume_does_not_move_the_retention_deadline(self):
        self.set_batch(workflow_version=2)
        self.seed_paused(total=4, done=1)
        deadline = self.batch().delete_at
        self.patch_source(4)
        gate = threading.Event()
        self.detect_impl = lambda img: (gate.wait(timeout=20), [])[1]

        self.assertTrue(pps.request_resume(self.batch_id)["resuming"])
        self.assertEqual(self.batch().delete_at, deadline, "resuming never extends retention")
        gate.set()
        self.assertTrue(pps.wait_until_idle(self.batch_id, timeout=60))
        b = self.batch()
        self.assertEqual((b.status, b.local_status), ("ready", "READY"))
        self.assertEqual(b.delete_at, deadline, "reaching READY after a pause never re-anchors the deadline")

    def test_a_run_that_reaches_ready_without_pausing_keeps_the_deadline(self):
        self.set_batch(workflow_version=2)
        self.patch_source(3)
        deadline = self.batch().delete_at

        pps.run_photo_batch(self.batch_id)

        b = self.batch()
        self.assertEqual((b.status, b.local_status, b.processed_photos), ("ready", "READY", 3))
        self.assertEqual(b.delete_at, deadline, "a normal run keeps the deadline it was created with")

    def test_resume_after_the_batch_was_deleted_is_not_found(self):
        self.seed_paused(total=3, done=1)
        self.assertTrue(pps.delete_batch(self.batch_id)["deleted"])
        self.assertEqual(pps.request_resume(self.batch_id), {"status": "not_found", "resuming": False})


class DeleteTests(PauseCase):
    def seed_other_batch(self):
        other_root = self.photo_batches_dir / OTHER
        with Session(self.engine) as s:
            person = Person(participant_id="0099", first_name="Keep", last_name="Me", image_path="people/keep.jpg",
                            embedding=b"\x01" * 2048)
            s.add(person)
            s.commit()
            s.add(ConsentRecord(person_id=person.id, choice="declined", source="kiosk"))
            s.add(PhotoBatch(id=OTHER, label="other", drive_folder_id="", storage_dir=f"photo_batches/{OTHER}",
                             status="ready"))
            photo = PhotoBatchPhoto(batch_id=OTHER, filename="o.png", drive_file_id="", original_path="")
            s.add(photo)
            s.add(PhotoBatchFace(photo_id=photo.id, person_id=person.id, confidence=0.9, bbox="1,1,5,5"))
            s.add(PhotoBatchParticipantFolder(batch_id=OTHER, person_id=person.id, folder_id=""))
            s.add(EventIdentityCandidate(batch_id=OTHER, photo_id=photo.id, face_index=0, capture_bucket="matched",
                                         selection_key="k", bbox="1,1,5,5", extractor_version="v", embedding=b"\0"))
            s.commit()
        for rel in ("ORIGINAL/o.png", "MEDIA/o.png"):
            (other_root / rel).parent.mkdir(parents=True, exist_ok=True)
            (other_root / rel).write_bytes(_png_bytes(99))
        shared = self.storage / "people" / "keep.jpg"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_bytes(b"participant photo")

    def rows_snapshot(self):
        with Session(self.engine) as s:
            return {
                "people": sorted((p.id, p.participant_id, p.embedding) for p in s.exec(select(Person)).all()),
                "consent": sorted((c.person_id, c.choice) for c in s.exec(select(ConsentRecord)).all()),
                "other_photos": len(s.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == OTHER)).all()),
                "other_faces": len(s.exec(select(PhotoBatchFace)).all()),
                "other_candidates": len(s.exec(select(EventIdentityCandidate)
                                               .where(EventIdentityCandidate.batch_id == OTHER)).all()),
                "other_batch": s.get(PhotoBatch, OTHER) is not None,
            }

    def test_delete_is_refused_while_pausing_then_offered_with_resume(self):
        from app.api.photo_batches import _batch_out

        gate = threading.Event()
        self.patch_source(3)
        self.pausing_detector(1, gate)
        run = self.start_run()
        self.wait_status("pausing")
        with self.assertRaises(pps.BatchBusy) as ctx:
            pps.delete_batch(self.batch_id)
        self.assertIn("pausing", str(ctx.exception))
        gate.set()
        self.finish_pause(run)
        out = _batch_out(self.batch())
        self.assertEqual((out["can_pause"], out["can_resume"], out["can_delete"]), (False, True, True))
        self.assertEqual(self.batch().status, "paused", "Pause never deletes anything by itself")
        self.assertEqual(pps.delete_batch(self.batch_id), {"deleted": True, "already_deleted": False})

    def test_delete_is_refused_while_processing(self):
        self.set_batch(status="processing")
        with self.assertRaises(pps.BatchBusy) as ctx:
            pps.delete_batch(self.batch_id)
        self.assertIn("Pause processing", str(ctx.exception))

    def test_delete_paused_batch_removes_only_this_batch(self):
        self.seed_other_batch()
        self.patch_source(4)
        self.pausing_detector(2)
        self.finish_pause(self.start_run())
        with Session(self.engine) as s:
            photo = s.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == self.batch_id)).first()
            s.add(EventIdentityCandidate(batch_id=self.batch_id, photo_id=photo.id, face_index=0,
                                         capture_bucket="matched", selection_key="k", bbox="1,1,5,5",
                                         extractor_version="v", embedding=b"\0"))
            s.commit()
        other_files = _tree_digest(self.photo_batches_dir / OTHER)
        shared = (self.storage / "people" / "keep.jpg").read_bytes()
        rows = self.rows_snapshot()

        self.assertTrue(pps.delete_batch(self.batch_id)["deleted"])

        self.assertFalse((self.photo_batches_dir / self.batch_id).exists())
        with Session(self.engine) as s:
            self.assertIsNone(s.get(PhotoBatch, self.batch_id))
            self.assertEqual(s.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == self.batch_id)).all(), [])
            self.assertEqual(s.exec(select(EventIdentityCandidate)
                                    .where(EventIdentityCandidate.batch_id == self.batch_id)).all(), [])
        self.assertEqual(_tree_digest(self.photo_batches_dir / OTHER), other_files, "other batch byte-for-byte")
        self.assertEqual((self.storage / "people" / "keep.jpg").read_bytes(), shared)
        self.assertEqual(self.rows_snapshot(), rows, "participants, consent and the other batch are untouched")

    def test_no_writes_after_delete_and_delete_is_idempotent(self):
        self.set_batch(status="paused")
        (self.photo_batches_dir / self.batch_id / "MEDIA").mkdir(parents=True, exist_ok=True)
        self.assertEqual(pps.delete_batch(self.batch_id), {"deleted": True, "already_deleted": False})
        pps.run_photo_batch(self.batch_id)
        pps.retry_unresolved_photos(self.batch_id)
        self.assertEqual(pps.request_resume(self.batch_id)["status"], "not_found")
        time.sleep(0.2)
        self.assertFalse((self.photo_batches_dir / self.batch_id).exists())
        self.assertEqual(pps.delete_batch(self.batch_id), {"deleted": False, "already_deleted": True})

    def test_delete_is_refused_while_any_batch_operation_holds_it(self):
        self.set_batch(status="paused")
        for hold, release, words in (
            (lambda: batch_edit_lock.begin_download(self.batch_id), lambda: batch_edit_lock.end_download(self.batch_id), "ZIP"),
            (lambda: batch_edit_lock.reserve_edit(self.batch_id), lambda: batch_edit_lock.release_edit(self.batch_id), "face-box"),
            (lambda: dds.reserve_upload(self.batch_id), lambda: dds._release_upload(self.batch_id), "Drive"),
        ):
            with self.subTest(words):
                self.assertTrue(hold())
                with self.assertRaises(pps.BatchBusy) as ctx:
                    pps.delete_batch(self.batch_id)
                self.assertIn(words, str(ctx.exception))
                release()
        self.assertTrue(pps.delete_batch(self.batch_id)["deleted"])

    def test_a_paused_batch_cannot_be_edited_or_exported(self):
        from app.services.face_geometry_service import edit_block_reason

        self.set_batch(status="paused")
        self.assertIsNotNone(edit_block_reason(self.batch()))
        self.assertFalse(local_output_ready(self.batch(), self.storage))


class RouteTests(PauseCase):
    def asgi(self, method, path):
        import asyncio
        import json

        from fastapi import FastAPI

        from app.api import photo_batches as pb
        from app.auth.deps import get_current_user
        from app.models.models import User

        app = FastAPI()
        app.include_router(pb.router)
        app.dependency_overrides[get_current_user] = lambda: User(username="admin", password_hash="x")

        async def run():
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(m):
                messages.append(m)

            await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": method,
                       "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                       "root_path": "", "headers": [], "http_version": "1.1", "client": ("127.0.0.1", 1),
                       "server": ("t", 80)}, receive, send)
            start = next(m for m in messages if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return start["status"], json.loads(body) if body else None

        return asyncio.run(run())

    def test_pause_resume_and_delete_routes(self):
        self.patch_source(2)
        self.set_batch(status="pending")
        base = f"/api/photo-batches/{self.batch_id}"
        status, body = self.asgi("POST", f"{base}/pause")
        self.assertEqual((status, body["pausing"]), (200, True))
        self.finish_pause()
        self.assertEqual(self.asgi("POST", f"{base}/pause"), (200, {"status": "paused", "pausing": False}))
        self.assertEqual(self.asgi("POST", f"{base}/resume"), (200, {"status": "resuming", "resuming": True}))
        self.assertTrue(pps.wait_until_idle(self.batch_id, timeout=60))
        self.assertEqual(self.batch().status, "ready")
        status, body = self.asgi("POST", f"{base}/resume")
        self.assertEqual(status, 409)
        self.assertIn("Only a paused batch", body["detail"])
        self.assertEqual(self.asgi("DELETE", base), (200, {"deleted": True, "already_deleted": False}))
        self.assertEqual(self.asgi("DELETE", base), (200, {"deleted": False, "already_deleted": True}))
        self.assertEqual(self.asgi("POST", f"{base}/pause")[0], 404)
        self.assertEqual(self.asgi("POST", f"{base}/resume")[0], 404)

    def test_payload_flags(self):
        from app.api.photo_batches import _batch_out

        expected = {"processing": (True, False, False), "paused": (False, True, True), "ready": (False, False, True)}
        for status, flags in expected.items():
            with self.subTest(status):
                self.set_batch(status=status)
                out = _batch_out(self.batch())
                self.assertEqual((out["can_pause"], out["can_resume"], out["can_delete"]), flags)
                self.assertNotIn("can_stop", out)
                self.assertNotIn("pause_diagnostics", out)


if __name__ == "__main__":
    unittest.main()
