"""Fair turn-taking for LIVE face inference (/api/look, kiosk, camera nodes).

Every live call asks for a turn here before it touches the live model. One
turn is handed out at a time, round-robin over sources: a source is the
caller's camera (the `source` form field of /api/look, the node id of a camera
node, the kiosk's account). A camera that sends four frames a second gets one
turn per round like a camera that sends one, so a busy camera cannot starve a
quiet one, and a frame never waits behind more than one frame per other
active source.

Bounded on purpose:
  * at most MAX_PENDING turns wait in total, and a source with a drop policy
    (/api/look) may have at most PER_SOURCE_DROP waiting — beyond that the
    request is refused at once with an estimate of when to retry;
  * every turn has a deadline; a turn whose caller has given up (deadline
    passed) is skipped, never served late;
  * nothing about the frame is stored here, only (source, deadline).

After its turn is granted the caller still takes the live lock
(engine.hold(LIVE, ...)). In dual mode nobody else uses that lock; in shared
mode it is the background lock too, and background work steps aside for the
waiting turn (inference_priority.interactive()).
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field

MAX_PENDING = 64
# A drop-policy source (/api/look camera) may have ONE frame waiting besides
# the one being served; a third is refused at once rather than queued stale.
PER_SOURCE_DROP = 1
WINDOW = 512  # recent requests kept for the wait/latency figures


class LiveBusy(RuntimeError):
    """No turn: the queue is full, this source already has a frame waiting,
    or the deadline passed. retry_after is the estimated seconds to wait."""

    def __init__(self, message: str, retry_after: float, reason: str):
        super().__init__(message)
        self.retry_after = retry_after
        self.reason = reason


@dataclass(eq=False)
class Turn:
    source: str
    workload: str
    deadline: float | None
    drop: bool
    queued_at: float = field(default_factory=time.perf_counter)
    granted_at: float | None = None
    state: str = "waiting"  # waiting | granted | expired | done

    def remaining(self) -> float | None:
        """Seconds left before the caller's deadline (None = no deadline)."""
        return None if self.deadline is None else max(self.deadline - time.perf_counter(), 0.0)


class LiveScheduler:
    def __init__(self, max_pending: int = MAX_PENDING, per_source_drop: int = PER_SOURCE_DROP):
        self.max_pending = max_pending
        self.per_source_drop = per_source_drop
        self._cond = threading.Condition()
        self._queues: dict[str, deque[Turn]] = {}
        self._order: deque[str] = deque()  # sources with waiting turns, next first
        self._holder: Turn | None = None
        self._pending = 0
        self._service_ms = 60.0  # EMA of one live turn, seeds the first estimate
        self._counts = {"granted": 0, "expired": 0, "rejected_full": 0, "rejected_source": 0, "cancelled": 0}

    # -- public -----------------------------------------------------------
    def acquire(self, source: str, *, timeout: float | None, drop: bool, workload: str = "live") -> Turn:
        """Wait for this source's turn. timeout=None waits without a deadline
        (but still in fair order). Raises LiveBusy instead of waiting when a
        drop-policy caller would exceed the bounds."""
        now = time.perf_counter()
        turn = Turn(source=source, workload=workload, deadline=None if timeout is None else now + timeout, drop=drop)
        with self._cond:
            if self._pending >= self.max_pending:
                self._counts["rejected_full"] += 1
                raise LiveBusy("Too many live requests are waiting.", self._estimate_locked(self._pending), "queue_full")
            queue = self._queues.get(source)
            if drop and queue is not None and len(queue) >= self.per_source_drop:
                self._counts["rejected_source"] += 1
                raise LiveBusy("This source already has a frame waiting; drop this one.",
                               self._estimate_locked(self._pending), "source_busy")
            if queue is None:
                queue = self._queues[source] = deque()
            if not queue:
                self._order.append(source)
            queue.append(turn)
            self._pending += 1
            self._grant_next_locked()
            while turn.state == "waiting":
                remaining = None if turn.deadline is None else turn.deadline - time.perf_counter()
                if remaining is not None and remaining <= 0:
                    self._remove_locked(turn, "expired")
                    self._counts["expired"] += 1
                    raise LiveBusy("Waited too long for a turn; drop this frame.",
                                   self._estimate_locked(self._pending), "timeout")
                self._cond.wait(remaining)
            if turn.state != "granted":  # expired while waiting for the round to reach it
                raise LiveBusy("Waited too long for a turn; drop this frame.",
                               self._estimate_locked(self._pending), "timeout")
            return turn

    def release(self, turn: Turn) -> None:
        with self._cond:
            if turn.state != "granted":
                return
            turn.state = "done"
            if turn.granted_at is not None:
                took = (time.perf_counter() - turn.granted_at) * 1000
                self._service_ms = self._service_ms * 0.8 + took * 0.2
            if self._holder is turn:
                self._holder = None
            self._grant_next_locked()
            self._cond.notify_all()

    def estimate_wait(self) -> float:
        with self._cond:
            return self._estimate_locked(self._pending)

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "queue": self._pending,
                "sources_waiting": len(self._order),
                "busy_with": self._holder.workload if self._holder else None,
                "typical_turn_ms": round(self._service_ms, 1),
                **self._counts,
            }

    # -- internals (caller holds self._cond) ----------------------------------
    def _estimate_locked(self, ahead: int) -> float:
        running = 1 if self._holder is not None else 0
        return (ahead + running) * self._service_ms / 1000

    def _remove_locked(self, turn: Turn, state: str) -> None:
        queue = self._queues.get(turn.source)
        if queue is not None and turn in queue:
            queue.remove(turn)
            self._pending -= 1
            if not queue:
                del self._queues[turn.source]
                try:
                    self._order.remove(turn.source)
                except ValueError:
                    pass
        turn.state = state

    def _grant_next_locked(self) -> None:
        now = time.perf_counter()
        while self._holder is None and self._order:
            source = self._order.popleft()
            queue = self._queues[source]
            turn = queue.popleft()
            self._pending -= 1
            if queue:
                self._order.append(source)  # back of the round
            else:
                del self._queues[source]
            if turn.deadline is not None and turn.deadline <= now:
                turn.state = "expired"  # its caller is giving up; never serve it late
                self._counts["expired"] += 1
                continue
            turn.state = "granted"
            turn.granted_at = now
            self._holder = turn
            self._counts["granted"] += 1
        self._cond.notify_all()


class LookStats:
    """Rolling figures for /api/look, for /api/health. Bounded memory; no
    source names, images or identities are kept."""

    def __init__(self, window: int = WINDOW):
        self._lock = threading.Lock()
        self._wait = deque(maxlen=window)
        self._took = deque(maxlen=window)
        self._ok = 0
        self._busy = 0
        self._busy_since: float | None = None
        self._longest_busy_s = 0.0

    def ok(self, wait_ms: float, took_ms: float) -> None:
        with self._lock:
            self._ok += 1
            self._wait.append(wait_ms)
            self._took.append(took_ms)
            if self._busy_since is not None:
                self._longest_busy_s = max(self._longest_busy_s, time.monotonic() - self._busy_since)
                self._busy_since = None

    def busy(self) -> None:
        with self._lock:
            self._busy += 1
            if self._busy_since is None:
                self._busy_since = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            wait = sorted(self._wait)
            took = sorted(self._took)
            current = time.monotonic() - self._busy_since if self._busy_since is not None else 0.0
            return {
                "requests_ok": self._ok,
                "requests_busy": self._busy,
                "window": len(took),
                "avg_wait_ms": round(sum(wait) / len(wait), 1) if wait else None,
                "p95_wait_ms": _pct(wait, 95),
                "p50_ms": _pct(took, 50),
                "p95_ms": _pct(took, 95),
                "p99_ms": _pct(took, 99),
                "busy_streak_s": round(current, 2),
                "longest_busy_streak_s": round(max(self._longest_busy_s, current), 2),
            }


@contextmanager
def live_turn(*, source: str, workload: str, timeout: float | None, drop: bool):
    """Wait for this source's fair turn, then run the enclosed
    engine.detect_faces() call(s) on the LIVE lane:

        with live_turn(source=..., workload="look", timeout=0.5, drop=True) as turn:
            faces = engine.detect_faces(img, timings, lock_timeout=turn.remaining())

    Raises LiveBusy when no turn is available in time or within the bounds.
    In shared mode the live caller is announced to background work from the
    moment it queues (inference_priority.interactive()), so the shared lock
    is normally free by the time its turn comes."""
    from app.face_recognition import engine
    from app.services import inference_priority

    announce = nullcontext() if engine.is_dual() else inference_priority.interactive()
    with announce:
        turn = scheduler.acquire(source, timeout=timeout, drop=drop, workload=workload)
        try:
            with engine.use(engine.LIVE, workload):
                yield turn
        finally:
            scheduler.release(turn)


def inference_ms(timings: dict | None) -> float:
    if not timings:
        return 0.0
    return float(timings.get("detection_ms", 0.0)) + float(timings.get("embedding_ms", 0.0))


def _pct(values: list[float], p: int) -> float | None:
    if not values:
        return None
    k = min(len(values) - 1, max(0, math.ceil(p / 100 * len(values)) - 1))
    return round(values[k], 1)


scheduler = LiveScheduler()
look_stats = LookStats()
