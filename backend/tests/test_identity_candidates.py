"""Phase F1 — evaluation-candidate capture (candidates only; nothing trusted).

Pins: capture on both processing paths inside the per-photo commit; the
bounded, order-independent bottom-k quota; retry idempotency; lifecycle on
batch and participant deletion; fail-soft when the tables are absent; and
that promotion and API exposure stay impossible.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine, select

import app.services.event_pipeline_service as eps
import app.services.photo_processing_service as pps
from app.models.identity_models import EventIdentityCandidate, EventIdentitySample, identity_metadata
from app.models.models import PhotoBatchFace, PhotoBatchPhoto
from app.services import identity_candidate_service as ics
from tests.test_event_photo_completeness import CompletenessTestCase, _png_bytes
from tests.test_event_pipeline_service import FakeSource, PipelineTestCase, _fake_source_item, _image
from tests.test_recognized_photos_semantics import _face, _matches, _people

VERSION = "test-extractor-v1"


def _pinned_version(testcase):
    p = patch.object(ics, "extractor_version", return_value=VERSION)
    p.start()
    testcase.addCleanup(p.stop)


class BucketAndKeyTests(unittest.TestCase):
    def test_buckets_are_strata_of_prediction_score_and_margin(self):
        self.assertEqual(ics.capture_bucket(True, 0.8, 0.3, True), "matched")
        self.assertEqual(ics.capture_bucket(True, 0.8, 0.01, True), "ambiguous")
        self.assertEqual(ics.capture_bucket(False, 0.40, 0.2, True), "low_score")
        self.assertEqual(ics.capture_bucket(False, 0.40, 0.01, True), "ambiguous")
        self.assertEqual(ics.capture_bucket(False, 0.10, 0.01, True), "no_match")

    def test_selection_key_is_a_pure_function_of_stable_ids(self):
        self.assertEqual(ics.selection_key("b", "p.jpg", 3), ics.selection_key("b", "p.jpg", 3))
        self.assertNotEqual(ics.selection_key("b", "p.jpg", 3), ics.selection_key("b", "p.jpg", 4))

    def test_sharpness_separates_texture_from_flat(self):
        flat = np.full((100, 100, 3), 128, np.uint8)
        noisy = np.random.default_rng(0).integers(0, 255, (100, 100, 3), dtype=np.uint8)
        self.assertEqual(ics.sharpness(flat, (10, 10, 90, 90)), 0.0)
        self.assertGreater(ics.sharpness(noisy, (10, 10, 90, 90)), 100.0)
        self.assertEqual(ics.sharpness(noisy, (150, 150, 200, 200)), 0.0, "a crop outside the image is safe")
        self.assertGreater(ics.sharpness(noisy, (90, 90, 200, 200)), 0.0, "a partly-outside crop is clipped")


class IdentityDb(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-candidates-")
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine(f"sqlite:///{(Path(self.temp.name) / 't.db').as_posix()}",
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        identity_metadata.create_all(self.engine)

    def row(self, photo: str, index: int, bucket: str = "matched", batch: str = "b1"):
        return EventIdentityCandidate(batch_id=batch, photo_id="", face_index=index, capture_bucket=bucket,
                                      selection_key=ics.selection_key(batch, photo, index), bbox="0,0,1,1",
                                      extractor_version=VERSION, embedding=b"\0" * 2048)

    def keys(self, session, batch="b1", bucket="matched"):
        return sorted(r.selection_key for r in session.exec(select(EventIdentityCandidate).where(
            EventIdentityCandidate.batch_id == batch, EventIdentityCandidate.capture_bucket == bucket)).all())


class ReservoirTests(IdentityDb):
    def fill(self, order):
        with Session(self.engine) as session:
            for photo in order:
                ics.replace_candidates(session, "b1", f"id-{photo}", [self.row(photo, i) for i in range(3)])
            session.commit()
            return self.keys(session)

    def test_the_quota_is_a_hard_bound_per_bucket(self):
        with patch.object(ics, "QUOTA_PER_BUCKET", 4):
            kept = self.fill(["p1", "p2", "p3", "p4"])
        self.assertEqual(len(kept), 4)

    def test_the_sample_is_the_same_whatever_order_photos_finish_in(self):
        with patch.object(ics, "QUOTA_PER_BUCKET", 4):
            a = self.fill(["p1", "p2", "p3", "p4"])
        for table in (EventIdentityCandidate,):
            with Session(self.engine) as session:
                for r in session.exec(select(table)).all():
                    session.delete(r)
                session.commit()
        with patch.object(ics, "QUOTA_PER_BUCKET", 4):
            b = self.fill(["p4", "p2", "p1", "p3"])
        self.assertEqual(a, b)
        every = sorted(ics.selection_key("b1", p, i) for p in ("p1", "p2", "p3", "p4") for i in range(3))
        self.assertEqual(a, every[:4], "the bottom-k keys, exactly")

    def test_buckets_and_batches_have_separate_quotas(self):
        with patch.object(ics, "QUOTA_PER_BUCKET", 2), Session(self.engine) as session:
            ics.replace_candidates(session, "b1", "x", [self.row("x", i) for i in range(3)])
            ics.replace_candidates(session, "b1", "y", [self.row("y", i, bucket="no_match") for i in range(3)])
            ics.replace_candidates(session, "b2", "z", [self.row("z", i, batch="b2") for i in range(3)])
            session.commit()
            self.assertEqual(len(self.keys(session)), 2)
            self.assertEqual(len(self.keys(session, bucket="no_match")), 2)
            self.assertEqual(len(self.keys(session, batch="b2")), 2)

    def test_a_retried_photo_replaces_its_own_candidates_without_duplicates(self):
        with Session(self.engine) as session:
            for _ in range(3):
                ics.replace_candidates(session, "b1", "ph", [self.row("p", i) for i in range(3)], ["f0", "f1", "f2"])
                session.commit()
            rows = session.exec(select(EventIdentityCandidate)).all()
            self.assertEqual(sorted((r.face_index, r.face_id) for r in rows), [(0, "f0"), (1, "f1"), (2, "f2")])


class LifecycleTests(IdentityDb):
    def test_candidates_are_deleted_with_their_batch_only(self):
        with Session(self.engine) as session:
            ics.replace_candidates(session, "b1", "x", [self.row("x", 0)])
            ics.replace_candidates(session, "b2", "y", [self.row("y", 0, batch="b2")])
            session.commit()
            self.assertEqual(ics.delete_for_batch(session, "b1"), 1)
            session.commit()
            self.assertEqual([r.batch_id for r in session.exec(select(EventIdentityCandidate)).all()], ["b2"])

    def test_forgetting_a_participant_nulls_references_and_excludes_labels(self):
        with Session(self.engine) as session:
            a = self.row("x", 0)
            a.predicted_person_id, a.top1_person_id, a.top2_person_id = "alice", "alice", "bob"
            b = self.row("x", 1)
            b.label_person_id, b.label_status, b.top2_person_id = "alice", "labeled_person", "alice"
            c = self.row("x", 2)
            c.predicted_person_id = "bob"
            ics.replace_candidates(session, "b1", "ph", [a, b, c])
            session.add(EventIdentitySample(person_id="alice", source="event_harvested", embedding=b"\0"))
            session.add(EventIdentitySample(person_id="bob", source="event_harvested", embedding=b"\0"))
            session.commit()

            ics.forget_person(session, "alice")
            session.commit()
            rows = {r.face_index: r for r in session.exec(select(EventIdentityCandidate)).all()}
            self.assertEqual((rows[0].predicted_person_id, rows[0].top1_person_id, rows[0].top2_person_id,
                              rows[0].label_status), (None, None, "bob", "excluded"))
            self.assertEqual((rows[1].label_person_id, rows[1].top2_person_id, rows[1].label_status),
                             (None, None, "excluded"))
            self.assertEqual((rows[2].predicted_person_id, rows[2].label_status), ("bob", "unlabeled"))
            self.assertEqual([s.person_id for s in session.exec(select(EventIdentitySample)).all()], ["bob"])

    def test_the_full_wipe_forgets_everyone(self):
        with Session(self.engine) as session:
            a = self.row("x", 0)
            a.predicted_person_id = "alice"
            ics.replace_candidates(session, "b1", "ph", [a])
            session.add(EventIdentitySample(person_id="bob", source="event_harvested", embedding=b"\0"))
            session.commit()
            ics.forget_person(session, None)
            session.commit()
            self.assertIsNone(session.exec(select(EventIdentityCandidate)).one().predicted_person_id)
            self.assertEqual(session.exec(select(EventIdentitySample)).all(), [])


class FailSoftTests(unittest.TestCase):
    def test_without_the_identity_tables_everything_is_a_no_op(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        SQLModel.metadata.create_all(engine)
        img = np.zeros((20, 20, 3), np.uint8)
        self.assertEqual(ics.build_candidates(engine=engine, batch_id="b", filename="p", img=img,
                                              faces=[_face(0)], predicted=[None], threshold=0.45), [])
        with Session(engine) as session:
            ics.replace_candidates(session, "b", "p", [])
            self.assertEqual(ics.delete_for_batch(session, "b"), 0)
            ics.forget_person(session, "x")


class SequentialCaptureTests(CompletenessTestCase):
    def setUp(self):
        super().setUp()
        identity_metadata.create_all(self.engine)
        _pinned_version(self)

    def test_a_processed_photo_captures_one_candidate_per_face_in_its_commit(self):
        ids = _people(self.engine, 3)
        with patch.object(pps.recognition_index, "match_batch", side_effect=lambda e, t: _matches(ids)):
            self.run_batch({"group.png": _png_bytes(7)}, detect_side_effect=lambda img: [_face(i) for i in range(4)])
        with Session(self.engine) as session:
            photo = session.exec(select(PhotoBatchPhoto)).one()
            faces = session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all()
            rows = sorted(session.exec(select(EventIdentityCandidate)).all(), key=lambda r: r.face_index)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r.photo_id == photo.id and r.extractor_version == VERSION for r in rows))
        self.assertEqual({r.face_id for r in rows}, {f.id for f in faces}, "linked to this attempt's face rows")
        self.assertEqual([r.predicted_person_id for r in rows], ids + [None])
        self.assertTrue(all(len(r.embedding) == 512 * 4 and r.capture_bucket in ics.BUCKETS for r in rows))
        self.assertTrue(all(r.image_w > 0 and r.image_h > 0 and r.det_score == 0.9 for r in rows))

    def test_an_ambience_photo_captures_nothing(self):
        self.run_batch({"empty.png": _png_bytes(9)}, detect_side_effect=lambda img: [])
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(EventIdentityCandidate)).all(), [])

    def test_a_capture_failure_never_affects_processing(self):
        ids = _people(self.engine, 3)
        with patch.object(ics, "_gallery_for", side_effect=RuntimeError("boom")), \
                patch.object(pps.recognition_index, "match_batch", side_effect=lambda e, t: _matches(ids)):
            self.run_batch({"group.png": _png_bytes(7)}, detect_side_effect=lambda img: [_face(i) for i in range(4)])
        b = self.batch()
        self.assertEqual((b.processed_photos, b.failed_photos, b.recognized_photos), (1, 0, 1))
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(EventIdentityCandidate)).all(), [])


class PipelineCaptureTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        identity_metadata.create_all(self.engine)
        _pinned_version(self)

    def test_the_pipeline_captures_in_the_same_commit(self):
        ids = _people(self.engine, 3)
        source = FakeSource({"group.png": cv2.imencode(".png", _image(7))[1].tobytes()})
        with patch.object(eps, "detect_event_faces", side_effect=lambda img: [_face(i) for i in range(4)]), \
             patch.object(eps.recognition_index, "match_batch", side_effect=lambda e, t: _matches(ids)):
            self.make_pipeline(source).run([(1, _fake_source_item("group.png"), "group.png")],
                                           self.batch_storage_dir, self.batch_id)
        with Session(self.engine) as session:
            photo = session.exec(select(PhotoBatchPhoto)).one()
            faces = session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all()
            rows = session.exec(select(EventIdentityCandidate)).all()
        self.assertEqual(len(rows), 4)
        self.assertEqual({r.face_id for r in rows}, {f.id for f in faces})
        self.assertTrue(all(r.photo_id == photo.id for r in rows))
        self.assertEqual(self.batch().processed_photos, 1)


class GuardrailTests(unittest.TestCase):
    def test_promotion_is_hard_disabled(self):
        self.assertFalse(ics.promotion_enabled())
        with self.assertRaises(ics.PromotionDisabledError):
            ics.promote_candidate("anything")

    def test_no_api_module_exposes_identity_rows_or_embeddings(self):
        api_dir = Path(__file__).resolve().parents[1] / "app" / "api"
        for path in api_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                self.assertNotIn("identity_models", text)
                self.assertNotIn("EventIdentityCandidate", text)

    def test_capture_does_not_touch_the_live_gallery_or_kiosk_index(self):
        import inspect

        source = inspect.getsource(ics)
        self.assertNotIn("event_identity_gallery.rebuild", source.replace("gallery.rebuild(session)", ""))
        self.assertNotIn("recognition_index", source)
        self.assertNotIn("match_batch", source)

    def test_deletion_paths_remove_candidates(self):
        import inspect

        from app.api import people

        self.assertIn("delete_for_batch", inspect.getsource(pps._delete_owned_state))
        self.assertIn("delete_for_batch", inspect.getsource(pps.run_retention_cleanup))
        self.assertIn("forget_person(session, person_id)", inspect.getsource(people.delete_person))
        self.assertIn("forget_person(session, None)", inspect.getsource(people.delete_all_people))


if __name__ == "__main__":
    unittest.main()
