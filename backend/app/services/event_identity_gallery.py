"""Event-only identity scoring (Phase F1).

A SEPARATE layer from the kiosk's RecognitionIndex, not an extension of it.
`RecognitionIndex.match_batch()` returns only the single best match — no
runner-up, no margin — so top1/top2/margin scoring necessarily has to be new
code. `index.py` and `Person.embedding` are read-only here; kiosk recognition
and legacy `workflow_version` batches are unaffected by anything in this file.

What counts as evidence
-----------------------
TRUSTED (may support an Event-only auto-match):
  * the participant's enrollment embedding (`Person.embedding`)
  * every human-confirmed `EventIdentitySample`

CANDIDATE (never auto-matches, only ever a Review suggestion): model-only
predictions. Those are never persisted as samples at all, so there is nothing
here that could later be mistaken for trusted evidence.

All trusted evidence for every candidate participant is evaluated TOGETHER, in
one pass, every time — deliberately NOT gated behind "did a kiosk-style single
reference already clear 0.45". A fallback-only design would let a weak
enrollment-only match win over a strong confirmed multi-angle reference simply
by running first.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np
from sqlmodel import Session, select

from app.models.identity_models import EventIdentitySample
from app.models.models import Person

# How several trusted references for ONE person collapse into that person's
# single score. The plan leaves this an explicit benchmarking choice rather
# than a silent pick, so all three are implemented and selectable.
AGGREGATIONS = ("max", "quality_weighted_max", "top_k")
DEFAULT_AGGREGATION = "max"
DEFAULT_TOP_K = 2


@dataclass
class EventMatch:
    """Everything the Review decision matrix needs, which match_batch cannot
    provide: a runner-up and therefore a margin."""

    top1_person_id: str | None
    top1_score: float
    top2_person_id: str | None
    top2_score: float

    @property
    def margin(self) -> float:
        """How much better the winner is than the next candidate. A small
        margin is the signal that a face is genuinely ambiguous rather than
        confidently unknown."""
        if self.top1_person_id is None:
            return 0.0
        if self.top2_person_id is None:
            return self.top1_score
        return self.top1_score - self.top2_score


def _unpack(blob: bytes) -> np.ndarray | None:
    """None for anything that is not a usable 512-d float32 vector. One bad
    row must never take down a gallery rebuild — np.frombuffer raises rather
    than returning short when the byte length is not a multiple of 4, so the
    length is checked before decoding rather than after."""
    if not blob or len(blob) != 512 * 4:
        return None
    return np.frombuffer(blob, dtype=np.float32)


class EventIdentityGallery:
    """An in-process matrix of TRUSTED references, rebuilt from the database.

    Same lock-protected shape as RecognitionIndex (a proven-safe pattern) but a
    wholly separate instance holding different rows, so a rebuild here can
    never disturb kiosk matching.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._matrix: np.ndarray = np.zeros((0, 512), dtype=np.float32)
        self._person_ids: list[str] = []
        self._qualities: list[float] = []
        self._version = 0

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def size(self) -> int:
        with self._lock:
            return self._matrix.shape[0]

    def rebuild(self, session: Session) -> None:
        """Load enrollment embeddings AND every trusted sample. Both are
        evidence; neither is privileged over the other by construction."""
        vectors: list[np.ndarray] = []
        person_ids: list[str] = []
        qualities: list[float] = []

        for person in session.exec(select(Person)).all():
            vector = _unpack(person.embedding)
            if vector is not None:
                vectors.append(vector)
                person_ids.append(person.id)
                # Enrollment is a deliberate, curated reference photo, so it
                # carries full weight unless a sample proves better.
                qualities.append(1.0)

        for sample in session.exec(
            select(EventIdentitySample).where(EventIdentitySample.trust == "trusted")
        ).all():
            vector = _unpack(sample.embedding)
            if vector is not None:
                vectors.append(vector)
                person_ids.append(sample.person_id)
                qualities.append(float(sample.quality_score) if sample.quality_score else 0.5)

        matrix = np.stack(vectors) if vectors else np.zeros((0, 512), dtype=np.float32)
        with self._lock:
            self._matrix = matrix.astype(np.float32)
            self._person_ids = person_ids
            self._qualities = qualities
            self._version += 1

    def score(self, embedding: np.ndarray, aggregation: str = DEFAULT_AGGREGATION,
              top_k: int = DEFAULT_TOP_K) -> EventMatch:
        """Score ONE face against all trusted evidence.

        The per-reference similarities are collapsed per PERSON first — a
        participant with five references must not out-rank one with a single
        better reference simply by having more rows.
        """
        if aggregation not in AGGREGATIONS:
            raise ValueError(f"Unknown aggregation: {aggregation}")

        with self._lock:
            matrix, person_ids, qualities = self._matrix, list(self._person_ids), list(self._qualities)

        if matrix.shape[0] == 0:
            return EventMatch(None, -1.0, None, -1.0)

        sims = matrix @ np.asarray(embedding, dtype=np.float32).reshape(-1)

        per_person: dict[str, list[tuple[float, float]]] = {}
        for person_id, similarity, quality in zip(person_ids, sims, qualities):
            per_person.setdefault(person_id, []).append((float(similarity), float(quality)))

        scored: list[tuple[str, float]] = []
        for person_id, pairs in per_person.items():
            scored.append((person_id, _aggregate(pairs, aggregation, top_k)))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        top1_id, top1 = scored[0]
        top2_id, top2 = scored[1] if len(scored) > 1 else (None, -1.0)
        return EventMatch(top1_id, top1, top2_id, top2)


def _aggregate(pairs: list[tuple[float, float]], aggregation: str, top_k: int) -> float:
    """pairs: (similarity, quality) for one person's trusted references."""
    similarities = [s for s, _ in pairs]
    if aggregation == "max":
        # Simplest: the best single reference wins.
        return max(similarities)
    if aggregation == "quality_weighted_max":
        # A low-quality sample cannot outscore a clean one on luck alone.
        return max(s * q for s, q in pairs)
    # top_k: average of the best k, trading a little responsiveness to one
    # lucky match for more stability.
    best = sorted(similarities, reverse=True)[:max(1, top_k)]
    return sum(best) / len(best)


# One process-wide gallery, mirroring how recognition_index is used — but a
# separate object holding separate rows.
event_identity_gallery = EventIdentityGallery()
