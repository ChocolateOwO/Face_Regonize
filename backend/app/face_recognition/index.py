"""In-memory recognition index: registered embeddings kept as one NumPy matrix
so the hot recognition path never touches the database.

Rebuilt from the DB once at startup, then kept in sync incrementally via
upsert()/remove() whenever a Person is created/updated/deleted (People API,
bulk import) — no restart required. Thread-safe: FastAPI runs endpoint code
in a threadpool for sync def handlers, so concurrent requests are possible.
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
                self._matrix = np.vstack([self._matrix, emb]) if self._matrix.shape[0] else emb

    def remove(self, person_id: str) -> None:
        with self._lock:
            if person_id in self._ids:
                i = self._ids.index(person_id)
                del self._ids[i]
                del self._first_names[i]
                del self._full_names[i]
                del self._participant_ids[i]
                self._matrix = np.delete(self._matrix, i, axis=0)

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
