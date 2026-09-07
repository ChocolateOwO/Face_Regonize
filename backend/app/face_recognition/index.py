"""In-memory recognition index: registered embeddings kept as one NumPy matrix
so the hot recognition path never touches the database.

Rebuilt from the DB once at startup, then kept in sync incrementally via
upsert()/remove() whenever a Person is created/updated/deleted (People API,
bulk import) — no restart required. Thread-safe: FastAPI runs endpoint code
in a threadpool for sync def handlers, so concurrent requests are possible.

version() exists for distributed recognition nodes (see api/nodes.py): a
Windows Local Agent keeps its OWN copy of this index and needs a cheap way to
ask "is mine still current?" without re-downloading every embedding on every
heartbeat. It is a plain incrementing counter, bumped on every mutation -
not a hash of the contents - because nothing here needs to detect two
different states landing on the same number, only "has anything changed
since I last asked."
"""
from __future__ import annotations

import threading
from typing import Iterable, NamedTuple

import numpy as np


class MatchResult(NamedTuple):
    person_id: str | None
    first_name: str | None
    full_name: str | None
    participant_id: str | None
    score: float


class RecognitionIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: list[str] = []
        self._first_names: list[str] = []
        self._full_names: list[str] = []
        self._participant_ids: list[str] = []
        self._matrix: np.ndarray = np.zeros((0, 512), dtype=np.float32)
        self._version: int = 0

    def rebuild(self, people: Iterable) -> None:
        people = list(people)
        with self._lock:
            self._ids = [p.id for p in people]
            self._first_names = [p.first_name for p in people]
            self._full_names = [f"{p.first_name} {p.last_name}".strip() for p in people]
            self._participant_ids = [p.participant_id for p in people]
            if people:
                self._matrix = np.stack([np.frombuffer(p.embedding, dtype=np.float32) for p in people])
            else:
                self._matrix = np.zeros((0, 512), dtype=np.float32)
            self._version += 1

    def upsert(self, person) -> None:
        emb = np.frombuffer(person.embedding, dtype=np.float32).reshape(1, -1)
        with self._lock:
            if person.id in self._ids:
                i = self._ids.index(person.id)
                self._first_names[i] = person.first_name
                self._full_names[i] = f"{person.first_name} {person.last_name}".strip()
                self._participant_ids[i] = person.participant_id
                self._matrix[i] = emb[0]
            else:
                self._ids.append(person.id)
                self._first_names.append(person.first_name)
                self._full_names.append(f"{person.first_name} {person.last_name}".strip())
                self._participant_ids.append(person.participant_id)
                # np.frombuffer gives a READ-ONLY view over the immutable bytes,
                # so adopting it directly as the matrix (the empty-index case)
                # makes the whole matrix read-only and the in-place update above
                # fails later. vstack already returns a fresh writable array, so
                # only this branch needs a copy - one row, only when empty.
                self._matrix = np.vstack([self._matrix, emb]) if self._matrix.shape[0] else emb.copy()
            self._version += 1

    def remove(self, person_id: str) -> None:
        with self._lock:
            if person_id in self._ids:
                i = self._ids.index(person_id)
                del self._ids[i]
                del self._first_names[i]
                del self._participant_ids[i]
                del self._full_names[i]
                self._matrix = np.delete(self._matrix, i, axis=0)
                self._version += 1

    def version(self) -> int:
        with self._lock:
            return self._version

    def snapshot(self) -> list[dict]:
        """Everything a distributed node needs to build its OWN local index:
        participant_id, a display name, and the raw embedding as a plain list
        of floats (JSON-serializable). Never returns image paths, emails, or
        anything else Person carries - a node's cache is deliberately narrower
        than the database (see api/nodes.py's recognition-index endpoint)."""
        with self._lock:
            return [
                {
                    "participant_id": self._participant_ids[i],
                    "name": self._full_names[i] or self._first_names[i] or "",
                    "embedding": self._matrix[i].tolist(),
                }
                for i in range(len(self._ids))
            ]

    def match_batch(self, embeddings: np.ndarray, threshold: float) -> list[MatchResult]:
        """embeddings: (K, 512) L2-normalized rows, one per detected face.
        Returns one MatchResult per row — person_id is None when the best
        score is below threshold. A single matrix multiply compares every
        face against every registered person at once (vectorized batch
        comparison), so this scales to hundreds of participants cheaply
        regardless of how many faces (K) are in the frame.
        """
        with self._lock:
            ids, firsts, fulls, participant_ids, matrix = (
                self._ids, self._first_names, self._full_names, self._participant_ids, self._matrix
            )

        if matrix.shape[0] == 0 or embeddings.shape[0] == 0:
            return [MatchResult(None, None, None, None, -1.0) for _ in range(embeddings.shape[0])]

        sims = embeddings @ matrix.T  # (K, N)
        results = []
        for row in sims:
            best_i = int(np.argmax(row))
            best_score = float(row[best_i])
            if best_score >= threshold:
                results.append(MatchResult(ids[best_i], firsts[best_i], fulls[best_i], participant_ids[best_i], best_score))
            else:
                results.append(MatchResult(None, None, None, None, best_score))
        return results

    def size(self) -> int:
        return len(self._ids)


recognition_index = RecognitionIndex()
