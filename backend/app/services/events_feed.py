"""A tiny in-process pub/sub for the CCTV page's global Recent Detections
feed (§44 — "near real-time... do not query complete history every second").

Not a message queue: nothing is durable, nothing survives a restart, and a
subscriber that connects late simply misses whatever was published before it
joined. That is fine here — this exists to save the CCTV page from polling
recognition history every second, not to be a reliable event log. The
recognition history itself already lives durably in Upload/FaceDetection/
Attendance; this is only the live nudge to refresh the screen.
"""
from __future__ import annotations

import asyncio
import threading

_lock = threading.Lock()
_subscribers: set[asyncio.Queue] = set()
# Subscribers that ALSO asked for live-presence messages (?include=presence).
# Presence is opt-in: every page on the ordinary feed reloads on each message
# and the robot's parser expects only matched events, so neither may receive
# a message type it never asked for.
_presence_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def subscribe(presence: bool = False) -> asyncio.Queue:
    global _loop
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    with _lock:
        _loop = asyncio.get_running_loop()
        _subscribers.add(queue)
        if presence:
            _presence_subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    with _lock:
        _subscribers.discard(queue)
        _presence_subscribers.discard(queue)


def publish(event: dict) -> None:
    """Called from ordinary sync request-handler code (api/nodes.py), so this
    must not assume it is already on the event loop — it schedules delivery
    onto whichever loop a subscriber most recently connected from."""
    with _lock:
        subscribers = list(_subscribers)
        loop = _loop
    _deliver(subscribers, loop, event)


def publish_presence(event: dict) -> None:
    """A live-presence message (services/presence_service.py): delivered only
    to subscribers that opted in, never to the ordinary feed."""
    with _lock:
        subscribers = list(_presence_subscribers)
        loop = _loop
    _deliver(subscribers, loop, event)


def _deliver(subscribers: list, loop, event: dict) -> None:
    if not subscribers or loop is None:
        return

    def _put_all() -> None:
        for q in subscribers:
            if q.full():
                try:
                    q.get_nowait()  # drop the oldest rather than block or grow unbounded
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    loop.call_soon_threadsafe(_put_all)
