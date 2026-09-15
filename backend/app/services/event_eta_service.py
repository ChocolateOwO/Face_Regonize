"""Phase G4 — ETA for local Event Photo processing.

Deliberately in-memory and transient: an ETA is only meaningful while a batch
is actually running in THIS process, and a stale persisted estimate read back
after a restart would be worse than none. Nothing here is written to the
database — `eta_seconds` rides along on the batch-status response the
frontend already polls.

Covers LOCAL processing only (decode -> detect -> match -> render), which is
the phase with a known denominator. Drive upload is a separate, explicitly
user-started action with its own progress and is not estimated here.

An exponentially-weighted moving average is used rather than a plain mean
because per-photo cost varies a lot with face count (a 40-face crowd shot
costs several times an empty corridor shot), and a batch's later photos are
usually more representative of what is left than its first ones.
"""
from __future__ import annotations

import threading
import time

# Weight of each new sample. 0.3 keeps roughly the last handful of photos
# dominant without the estimate jittering on a single unusual frame.
_ALPHA = 0.3

# Below this many completed photos the average is too noisy to show a number;
# the UI says "Estimating..." instead of printing a figure that will visibly
# lurch on the next photo.
_WARMUP_PHOTOS = 3


class _BatchEta:
    __slots__ = ("ewma_seconds", "samples", "last_photo_at")

    def __init__(self) -> None:
        self.ewma_seconds: float | None = None
        self.samples = 0
        self.last_photo_at: float | None = None


_lock = threading.Lock()
_batches: dict[str, _BatchEta] = {}


def start(batch_id: str) -> None:
    """Begin (or restart) timing for a batch. Called when local processing
    starts, so the first photo's own duration is measured from that point."""
    with _lock:
        state = _BatchEta()
        state.last_photo_at = time.monotonic()
        _batches[batch_id] = state


def record_photo_done(batch_id: str) -> None:
    """One photo finished its local pipeline (success or terminal failure —
    both consume wall-clock and both reduce the remaining count)."""
    now = time.monotonic()
    with _lock:
        state = _batches.get(batch_id)
        if state is None:
            state = _BatchEta()
            state.last_photo_at = now
            _batches[batch_id] = state
            return
        if state.last_photo_at is None:
            state.last_photo_at = now
            return
        elapsed = now - state.last_photo_at
        state.last_photo_at = now
        if elapsed <= 0:
            return
        state.samples += 1
        state.ewma_seconds = (
            elapsed if state.ewma_seconds is None
            else _ALPHA * elapsed + (1 - _ALPHA) * state.ewma_seconds
        )


def eta_seconds(batch_id: str, remaining_photos: int) -> int | None:
    """Seconds still expected, or None while warming up / when nothing is
    left / when this batch is not running in this process."""
    if remaining_photos <= 0:
        return None
    with _lock:
        state = _batches.get(batch_id)
        if state is None or state.ewma_seconds is None or state.samples < _WARMUP_PHOTOS:
            return None
        return max(1, int(round(state.ewma_seconds * remaining_photos)))


def is_estimating(batch_id: str) -> bool:
    """True when a batch is being timed but has not produced a usable figure
    yet — lets the UI distinguish "warming up" from "no ETA available"."""
    with _lock:
        state = _batches.get(batch_id)
        return state is not None and state.samples < _WARMUP_PHOTOS


def forget(batch_id: str) -> None:
    with _lock:
        _batches.pop(batch_id, None)
