"""Cooperative priority on the one shared inference device.

Live callers that answer a person or a robot in real time (the stateless
/api/look) announce themselves here. Long background work that takes the
shared inference lock once per frame (the tagged-video render) asks before
each acquisition and steps aside for a moment while a live caller is waiting.

This is deliberately not a new lock and does not change the shared engine
lock or Event Photo: a plain threading.Lock has no fairness, so a loop that
re-acquires it immediately can starve a waiter; the short pause here is what
lets the waiter in. The pause is bounded, so background work always makes
progress.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

_guard = threading.Lock()
_waiting = 0
_yields = 0
_yield_seconds = 0.0


@contextmanager
def interactive():
    """Wrap a live inference call (waiting for the lock + running)."""
    global _waiting
    with _guard:
        _waiting += 1
    try:
        yield
    finally:
        with _guard:
            _waiting -= 1


def interactive_waiting() -> bool:
    return _waiting > 0


def yield_to_interactive(max_wait: float = 0.3, step: float = 0.002) -> float:
    """Called by background work just before it takes the inference lock.
    Returns the seconds it stood aside (0.0 almost always)."""
    global _yields, _yield_seconds
    if _waiting <= 0:
        return 0.0
    begin = time.perf_counter()
    deadline = begin + max_wait
    while _waiting > 0 and time.perf_counter() < deadline:
        time.sleep(step)
    waited = time.perf_counter() - begin
    with _guard:
        _yields += 1
        _yield_seconds += waited
    return waited


def stats() -> dict:
    return {"interactive_waiting": _waiting, "background_yields": _yields,
            "background_yield_seconds": round(_yield_seconds, 3)}
