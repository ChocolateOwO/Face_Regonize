"""Phase F1 — Event-only identity scoring.

The primary regression here is the case the earlier fallback-only design got
wrong: a STRONG trusted Event reference for one person must beat a WEAKER
enrollment-only match for a different person, rather than losing simply
because a kiosk-style single-reference check ran first.
"""
import importlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from sqlmodel import Session, SQLModel, create_engine

from app.models.identity_models import EventIdentitySample, identity_metadata
from app.models.models import Person
from app.services.event_identity_gallery import (
    AGGREGATIONS,
    EventIdentityGallery,
    EventMatch,
)


def vec(*weights: float) -> np.ndarray:
    """A unit-length 512-d vector built from a few basis directions, so
    cosine similarity between two of them is predictable."""
    v = np.zeros(512, dtype=np.float32)
    for i, w in enumerate(weights):
        v[i] = w
    norm = np.linalg.norm(v)
    return (v / norm).astype(np.float32) if norm else v


class GalleryTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        # The identity tables are on their own metadata (production gets them
        # from migration 010 only); an in-memory test DB creates them directly.
        identity_metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)
        self.gallery = EventIdentityGallery()

    def person(self, name: str, embedding: np.ndarray) -> Person:
        p = Person(participant_id=name, first_name=name, last_name="X",
                   image_path="", embedding=embedding.tobytes())
        self.session.add(p)
        self.session.commit()
        self.session.refresh(p)
        return p

    def sample(self, person: Person, embedding: np.ndarray, quality: float = 1.0,
               trust: str = "trusted", source: str = "review_confirmed") -> EventIdentitySample:
        s = EventIdentitySample(person_id=person.id, source=source, trust=trust,
                                embedding=embedding.tobytes(), quality_score=quality)
        self.session.add(s)
        self.session.commit()
        return s


class ScoringTests(GalleryTestCase):
    def test_empty_gallery_returns_no_match(self):
        self.gallery.rebuild(self.session)
        result = self.gallery.score(vec(1.0))
        self.assertIsNone(result.top1_person_id)
        self.assertEqual(result.margin, 0.0)

    def test_enrollment_alone_can_match(self):
        alice = self.person("alice", vec(1.0))
        self.gallery.rebuild(self.session)
        result = self.gallery.score(vec(1.0))
        self.assertEqual(result.top1_person_id, alice.id)
        self.assertAlmostEqual(result.top1_score, 1.0, places=5)

    def test_a_strong_trusted_sample_beats_a_weaker_enrollment_for_another_person(self):
        """THE regression this phase exists for. Bob's enrollment is a
        mediocre match for the probe; Alice's confirmed side-angle sample is a
        strong one. Alice must win — the kiosk-style check clearing on Bob
        first must not decide the outcome."""
        alice = self.person("alice", vec(1.0, 0.0))          # poor for the probe
        bob = self.person("bob", vec(0.75, 0.66))            # moderate for the probe
        probe = vec(0.6, 0.8)
        self.sample(alice, vec(0.62, 0.78))                  # human-confirmed, near-identical

        self.gallery.rebuild(self.session)
        result = self.gallery.score(probe)

        self.assertEqual(result.top1_person_id, alice.id,
                         "the strong trusted sample must win over the weaker enrollment")
        self.assertEqual(result.top2_person_id, bob.id)
        self.assertGreater(result.top1_score, result.top2_score)

    def test_margin_is_exposed_for_ambiguity_detection(self):
        """match_batch() cannot provide this at all — it returns only the
        single best score, which is why this layer exists."""
        self.person("alice", vec(1.0, 0.0))
        self.person("bob", vec(0.99, 0.14))
        self.gallery.rebuild(self.session)
        result = self.gallery.score(vec(1.0, 0.0))
        self.assertIsNotNone(result.top2_person_id)
        self.assertGreater(result.margin, 0.0)
        self.assertLess(result.margin, 0.1, "two near-identical candidates -> small margin")

    def test_single_candidate_margin_is_the_score_itself(self):
        self.person("alice", vec(1.0))
        self.gallery.rebuild(self.session)
        result = self.gallery.score(vec(1.0))
        self.assertIsNone(result.top2_person_id)
        self.assertAlmostEqual(result.margin, result.top1_score, places=5)

    def test_many_references_do_not_inflate_a_person_by_count(self):
        """Scores collapse per PERSON first: five mediocre references must not
        beat one excellent reference."""
        many = self.person("many", vec(0.7, 0.7))
        best = self.person("best", vec(1.0, 0.0))
        for _ in range(5):
            self.sample(many, vec(0.7, 0.7))
        self.gallery.rebuild(self.session)
        result = self.gallery.score(vec(1.0, 0.0))
        self.assertEqual(result.top1_person_id, best.id)


class TrustTierTests(GalleryTestCase):
    def test_only_trusted_samples_are_loaded(self):
        alice = self.person("alice", vec(1.0, 0.0))
        self.sample(alice, vec(0.0, 1.0), trust="candidate")
        self.gallery.rebuild(self.session)
        # enrollment only: 1 row, not 2
        self.assertEqual(self.gallery.size, 1)

    def test_candidate_evidence_can_never_win_a_match(self):
        alice = self.person("alice", vec(1.0, 0.0))
        bob = self.person("bob", vec(0.9, 0.43))
        self.sample(alice, vec(0.0, 1.0), trust="candidate")  # perfect for the probe, untrusted
        self.gallery.rebuild(self.session)
        result = self.gallery.score(vec(0.0, 1.0))
        self.assertNotEqual(result.top1_score, 1.0,
                            "an untrusted sample must not be able to produce a perfect match")

    def test_rebuild_bumps_the_version_for_invalidation(self):
        before = self.gallery.version
        self.gallery.rebuild(self.session)
        self.assertGreater(self.gallery.version, before)

    def test_malformed_embeddings_are_skipped_not_fatal(self):
        alice = self.person("alice", vec(1.0))
        self.session.add(EventIdentitySample(person_id=alice.id, source="review_confirmed",
                                             trust="trusted", embedding=b"too-short"))
        self.session.commit()
        self.gallery.rebuild(self.session)  # must not raise
        self.assertEqual(self.gallery.size, 1)


class AggregationTests(GalleryTestCase):
    """All three strategies are implemented rather than one being silently
    picked; the production default still needs a labeled evaluation set."""

    def test_all_documented_strategies_are_selectable(self):
        self.person("alice", vec(1.0))
        self.gallery.rebuild(self.session)
        for aggregation in AGGREGATIONS:
            result = self.gallery.score(vec(1.0), aggregation=aggregation)
            self.assertIsInstance(result, EventMatch)

    def test_unknown_strategy_is_rejected(self):
        self.gallery.rebuild(self.session)
        with self.assertRaises(ValueError):
            self.gallery.score(vec(1.0), aggregation="vibes")

    def test_quality_weighting_demotes_a_low_quality_sample(self):
        alice = self.person("alice", vec(1.0, 0.0))
        self.sample(alice, vec(0.0, 1.0), quality=0.1)  # great match, poor quality
        self.gallery.rebuild(self.session)
        probe = vec(0.0, 1.0)
        plain = self.gallery.score(probe, aggregation="max").top1_score
        weighted = self.gallery.score(probe, aggregation="quality_weighted_max").top1_score
        self.assertLess(weighted, plain)

    def test_top_k_is_more_conservative_than_max(self):
        """Averaging the best two dilutes a single lucky reference — the
        false-positive-safety direction the plan asks the first policy to lean."""
        alice = self.person("alice", vec(1.0, 0.0))
        self.sample(alice, vec(0.6, 0.8))
        self.gallery.rebuild(self.session)
        probe = vec(1.0, 0.0)
        by_max = self.gallery.score(probe, aggregation="max").top1_score
        by_topk = self.gallery.score(probe, aggregation="top_k", top_k=2).top1_score
        self.assertLess(by_topk, by_max)


class IsolationTests(GalleryTestCase):
    def test_the_kiosk_index_is_not_touched(self):
        """RecognitionIndex must be read-only to this phase — kiosk behaviour
        and legacy workflow_version batches are unaffected."""
        import inspect

        from app.services import event_identity_gallery as gallery_module

        import ast

        # Parse rather than grep: the module's own docstring legitimately
        # explains its relationship to RecognitionIndex, and prose must not be
        # mistaken for a dependency.
        tree = ast.parse(inspect.getsource(gallery_module))

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertFalse(
            [m for m in imported if m.startswith("app.face_recognition")],
            f"this layer must not import the kiosk stack; imported: {sorted(imported)}",
        )

        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("match_batch", called)

    def test_migration_007_is_registered_and_is_a_noop(self):
        import sqlite3

        m007 = importlib.import_module("app.migrations.007_adaptive_identity")
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        # Left byte-identical (Dummy recorded it as applied); the tables now
        # come from migration 010 — see test_identity_metadata.py.
        self.assertIsNone(m007.upgrade(conn))


if __name__ == "__main__":
    unittest.main()
