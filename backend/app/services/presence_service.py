"""Who is standing at a specific camera RIGHT NOW - live, per node, in memory.

A robot greeting needs "who is at CAM-02 this second", including "somebody I
do not know". That is not history: an old recognition row proves nothing
about now. So presence is built only from live observations that an existing
node workflow reports while it is scanning, and it is deliberately forgetful:

    - kept in this process's memory only - nothing is written to the
      database or to disk, and no image is kept;
    - an observation is discarded after MAX_WINDOW_SECONDS;
    - after a backend restart there is no presence until a node reports again.

Sources (api/nodes.py): the browser of a CCTV station forwards what its Local
Agent just saw (POST /api/node/observation), and Central's own inference for
a phone/tablet node (POST /api/node/recognize-central, when it records).
The stateless POST /api/look never reports here.

One reporter per node. A node's observations are bound to whoever is
reporting them: while one reporter (account + client address + page instance)
keeps reporting, anyone else's observations for that node are refused. Two
stations that were given the same node name therefore cannot mix their faces,
and nothing can be injected into a camera that is live. The binding lapses
REPORTER_LEASE_SECONDS after the reporter's last observation.

Presence events. Besides the query (GET /api/node/{id}/present) a client may
opt in to "presence" messages on the events feed. They are throttled per
node: sent when the number of unknown faces changes, and at most every
PRESENCE_EVENT_SECONDS while unknown faces remain.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime

MAX_WINDOW_SECONDS = 30
DEFAULT_WINDOW_SECONDS = 10
MIN_WINDOW_SECONDS = 1
MAX_FACES = 100
MAX_OBSERVATIONS = 300  # per node; a station scans a few times a second
PRESENCE_EVENT_SECONDS = 2.0
REPORTER_LEASE_SECONDS = 10.0  # a station scans a few times a second; ten silent seconds means it has stopped


class ReporterConflict(Exception):
    """Another reporter currently holds this node."""


_lock = threading.Lock()
_reporters: dict[str, tuple[str, float]] = {}  # node_id -> (reporter, monotonic time of its last observation)
_nodes: dict[str, deque] = {}          # node_id -> observations, oldest first
_last_event: dict[str, tuple[float, int]] = {}  # node_id -> (monotonic time, unknown count) of the last presence event


def _clock() -> float:
    return time.monotonic()


def clamp_window(seconds) -> int:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = DEFAULT_WINDOW_SECONDS
    return max(MIN_WINDOW_SECONDS, min(MAX_WINDOW_SECONDS, value))


def normalized_box(bbox, width: float, height: float) -> list[float] | None:
    """[x1, y1, x2, y2] in pixels -> 0-1, clipped. None when unusable."""
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if not (width > 0 and height > 0) or any(v != v or v in (float("inf"), float("-inf")) for v in (x1, y1, x2, y2)):
        return None
    clip = lambda v, limit: min(max(v / limit, 0.0), 1.0)  # noqa: E731
    box = [round(clip(x1, width), 4), round(clip(y1, height), 4), round(clip(x2, width), 4), round(clip(y2, height), 4)]
    return box if box[2] > box[0] and box[3] > box[1] else None


def _area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1]) if box else 0.0


def claim(node_id: str, reporter: str) -> None:
    """Take or renew the node for `reporter`; ReporterConflict while a
    different reporter's lease is still running."""
    now = _clock()
    with _lock:
        holder = _reporters.get(node_id)
        if holder and holder[0] != reporter and now - holder[1] <= REPORTER_LEASE_SECONDS:
            raise ReporterConflict(node_id)
        _reporters[node_id] = (reporter, now)


def record(node_id: str, image_w: int, image_h: int, faces: list[dict]) -> dict:
    """Store one observation for a node. Each face: {"box": normalized or None,
    "participant_id": str or None, "person_id", "name", "confidence"}; a face
    without participant_id is an unknown face. Returns what a presence event
    should say now, or {} when the throttle says "nothing new"."""
    now = _clock()
    kept = [dict(face) for face in faces[:MAX_FACES]]
    unknown = sum(1 for face in kept if not face.get("participant_id"))
    observation = {"at": now, "wall": datetime.now().isoformat(), "image": {"w": int(image_w), "h": int(image_h)}, "faces": kept}
    with _lock:
        queue = _nodes.setdefault(node_id, deque(maxlen=MAX_OBSERVATIONS))
        queue.append(observation)
        while queue and now - queue[0]["at"] > MAX_WINDOW_SECONDS:
            queue.popleft()
        last_time, last_unknown = _last_event.get(node_id, (None, 0))
        changed = unknown != last_unknown
        due = unknown > 0 and (last_time is None or now - last_time >= PRESENCE_EVENT_SECONDS)
        if not (changed or due):
            return {}
        _last_event[node_id] = (now, unknown)
    return {"type": "presence", "node_id": node_id, "status": "unknown" if unknown else "clear",
            "faces": len(kept), "unknown": unknown, "at": observation["wall"]}


def present(node_id: str, seconds=DEFAULT_WINDOW_SECONDS) -> dict:
    """Live presence at one node over the last `seconds` (1-30). Empty when the
    node has not reported inside the window - never filled from history."""
    window = clamp_window(seconds)
    now = _clock()
    with _lock:
        queue = _nodes.get(node_id)
        if queue:
            while queue and now - queue[0]["at"] > MAX_WINDOW_SECONDS:
                queue.popleft()
        recent = [o for o in (queue or ()) if now - o["at"] <= window]
    result = {"node_id": node_id, "seconds": window, "as_of": datetime.now().isoformat(), "observed_at": None, "image": None,
              "faces_now": 0, "unknown_now": 0, "people": []}
    if not recent:
        return result
    latest = recent[-1]
    people: dict[str, dict] = {}
    for observation in recent:  # oldest -> newest, so the newest sighting of a person wins
        for face in observation["faces"]:
            participant_id = face.get("participant_id")
            if participant_id:
                people[participant_id] = {"participant_id": participant_id, "person_id": face.get("person_id"),
                                          "name": face.get("name"), "call_as": None, "last_seen": observation["wall"],
                                          "box": face.get("box"), "confidence": face.get("confidence")}
    result.update(observed_at=latest["wall"], image=latest["image"], faces_now=len(latest["faces"]),
                  unknown_now=sum(1 for face in latest["faces"] if not face.get("participant_id")),
                  # Largest face first: the closest person is the one to greet.
                  people=sorted(people.values(), key=lambda p: (-_area(p["box"]), p["participant_id"])))
    return result


def reset() -> None:
    with _lock:
        _nodes.clear()
        _last_event.clear()
        _reporters.clear()
