"""Amendment A1 — the pipeline's per-photo commit is atomic and never poisons
the shared session.

The commit worker holds ONE Session for the whole batch. Previously a photo's
row, face rows and counters were three separate commits, and an exception left
that session in a failed transaction with no rollback — so every later photo's
commit failed too. A DB failure is injected here for one photo; the rest of
the batch must still commit, the failed photo must be recorded as failed
(retriable), and nothing may be half-written for it.
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, select

import app.services.event_pipeline_service as eps
from app.models.models import PhotoBatchFace, PhotoBatchPhoto
from tests.test_event_pipeline_service import FakeSource, PipelineTestCase, _fake_face, _fake_source_item, _image


class FlakyCommitSession(eps.Session):
    """Fails the commit that would finalize `bad.png`, exactly once.

    Looks in the identity map, not just `self.new`: the done branch's
    participant-folder query autoflushes, so by commit time the photo is
    already flushed-but-uncommitted rather than pending. `vars(o)` is read so
    that inspecting other (expired) objects never triggers a lazy load.
    """

    fired = False

    def _finalizing_bad_photo(self) -> bool:
        for o in list(self.new) + list(self.identity_map.values()):
            if isinstance(o, PhotoBatchPhoto):
                state = vars(o)
                if state.get("filename") == "bad.png" and state.get("media_path"):
                    return True
        return False

    def commit(self):
        if not FlakyCommitSession.fired and self._finalizing_bad_photo():
            FlakyCommitSession.fired = True
            raise RuntimeError("simulated database failure during the per-photo commit")
        return super().commit()


class PipelineCommitAtomicityTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        FlakyCommitSession.fired = False

    def run_pipeline(self, names):
        images = {n: _image(10 + i) for i, n in enumerate(names)}
        source = FakeSource({n: cv2.imencode(".png", img)[1].tobytes() for n, img in images.items()})
        items = [(i + 1, _fake_source_item(n), n) for i, n in enumerate(names)]

        def detect(img):
            return [_fake_face(1, (10.0, 10.0, 50.0, 50.0))]

        def match(embeddings, threshold):
            return [(self.person_a.id, "Aiko", "Aiko", "0001", 0.9)]

        pipeline = self.make_pipeline(source)
        with patch.object(eps, "Session", FlakyCommitSession), \
             patch.object(eps, "detect_event_faces", side_effect=detect), \
             patch.object(eps.recognition_index, "match_batch", side_effect=match):
            pipeline.run(items, self.batch_storage_dir, self.batch_id)

    def faces_for(self, filename):
        with Session(self.engine) as session:
            photo = session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.filename == filename)).one()
            return session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all()

    def test_one_failed_commit_does_not_poison_the_rest_of_the_batch(self):
        names = ["bad.png", "a.png", "b.png", "c.png", "d.png"]
        self.run_pipeline(names)
        self.assertTrue(FlakyCommitSession.fired, "the failure must actually have been injected")
        b = self.batch()
        self.assertEqual(b.processed_photos, 4, "every other photo still committed")
        self.assertEqual(b.failed_photos, 1)
        self.assertEqual(b.recognized_photos, 4, "counted once per finalized photo only")
        self.assertEqual(b.faces_detected, 4)

    def test_the_failed_photo_is_recorded_retriable_and_nothing_is_half_written(self):
        self.run_pipeline(["bad.png", "a.png"])
        photos = {p.filename: p for p in self.photos()}
        self.assertIn("bad.png", photos, "recorded, so completeness can see it")
        self.assertIsNone(photos["bad.png"].media_path, "no media_path -> retry-unresolved will pick it up")
        self.assertEqual(self.faces_for("bad.png"), [], "no face rows from the rolled-back commit")
        self.assertEqual(sum(1 for p in self.photos() if p.filename == "bad.png"), 1, "exactly one row, no duplicate")
        self.assertEqual(len(self.faces_for("a.png")), 1)

    def test_the_done_branch_commits_exactly_once_per_photo(self):
        import inspect

        source = inspect.getsource(eps.EventPhotoPipeline._commit_item)
        done_branch = source[source.index('# "done"'):]
        self.assertEqual(done_branch.count("session.commit()"), 1)

    def test_the_worker_rolls_back_the_shared_session_on_failure(self):
        import inspect

        source = inspect.getsource(eps.EventPhotoPipeline._commit_worker)
        self.assertIn("session.rollback()", source)


if __name__ == "__main__":
    unittest.main()
