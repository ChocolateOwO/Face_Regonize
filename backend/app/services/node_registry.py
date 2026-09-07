"""Live presence for distributed camera nodes — Windows PC stations and
mobile devices — kept entirely in memory.

WHY IN MEMORY, NOT A DATABASE TABLE
    A heartbeat arrives every few seconds from every online node. Writing a
    row per heartbeat would fill the database with rows nobody ever reads
    again (the same reasoning that keeps the ffmpeg preview stills off disk
    and the multi-camera recognition stats out of any table). Presence is
    inherently "what is true right now", not history, so it lives in a plain
    dict guarded by a lock — the same pattern camera_recognition.py already
    uses for its own per-camera stats.

    A node's durable CONFIGURATION (its name, which activity it covers, which
    camera, its mode) is different: that is written rarely, by a person, and
    should survive a refresh or a backend restart. That half lives in the
    CameraNode database table (api/nodes.py). This module only ever answers
    "is this node alive, and what is it doing right now".

ONLINE / OFFLINE
    A node is ONLINE if it has heartbeated within HEARTBEAT_TIMEOUT_SEC.
    There is no separate "going offline" event to miss or race — every read
    just compares the last-seen timestamp against now, so a node that stops
    heartbeating (page closed, crashed, network dropped) is reported OFFLINE
    within one timeout window with no special-casing anywhere.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

# How long without a heartbeat before a node is reported OFFLINE. Short enough
# that "PC B unplugged its camera" is noticed quickly, long enough that one
# missed heartbeat over a flaky Wi-Fi does not flap the status.
HEARTBEAT_TIMEOUT_SEC = 10.0

_lock = threading.Lock()
_nodes: dict[str, dict] = {}  # node_id -> live state


def touch(
    node_id: str,
    *,
    display_name: str = "",
    activity_id: Optional[str] = None,
    activity_name: str = "",
    camera_label: str = "",
    mode: str = "",
    inference_mode: str = "",
    recording: Optional[bool] = None,
    local_agent_ok: Optional[bool] = None,
    faces_seen: Optional[int] = None,
    index_version: Optional[int] = None,
) -> dict:
    """Record a heartbeat (or the initial registration) for one node.

    Every field is optional past node_id: a bare heartbeat only needs to say
    "I am still here", and should not have to re-send configuration that has
    not changed just to avoid overwriting it with blanks.
    """
    now = time.monotonic()
    with _lock:
        node = _nodes.setdefault(node_id, {
            "node_id": node_id,
            "display_name": "",
            "activity_id": None,
            "activity_name": "",
            "camera_label": "",
            "mode": "always",
            "inference_mode": "local",
            "recording": False,
            "local_agent_ok": None,
            "faces_seen": 0,
            "index_version": None,
            "first_seen": now,
        })
        node["last_seen"] = now
        if display_name:
            node["display_name"] = display_name
        if activity_id is not None:
            node["activity_id"] = activity_id
        if activity_name:
            node["activity_name"] = activity_name
        if camera_label:
            node["camera_label"] = camera_label
        if mode:
            node["mode"] = mode
        if inference_mode:
            node["inference_mode"] = inference_mode
        if recording is not None:
            node["recording"] = recording
        if local_agent_ok is not None:
            node["local_agent_ok"] = local_agent_ok
        if faces_seen is not None:
            node["faces_seen"] = faces_seen
        if index_version is not None:
            node["index_version"] = index_version
        return dict(node)


def _is_online(node: dict, now: float) -> bool:
    return (now - node["last_seen"]) <= HEARTBEAT_TIMEOUT_SEC


def status_one(node_id: str) -> dict | None:
    now = time.monotonic()
    with _lock:
        node = _nodes.get(node_id)
        if not node:
            return None
        out = dict(node)
    out["online"] = _is_online(out, now)
    out["last_seen_sec_ago"] = round(now - out["last_seen"], 1)
    return out


def status_all() -> list[dict]:
    now = time.monotonic()
    with _lock:
        snapshot = [dict(n) for n in _nodes.values()]
    out = []
    for node in snapshot:
        node["online"] = _is_online(node, now)
        node["last_seen_sec_ago"] = round(now - node["last_seen"], 1)
        out.append(node)
    out.sort(key=lambda n: n["node_id"])
    return out


def forget(node_id: str) -> bool:
    """Explicit removal (a node un-registering itself), not used for ordinary
    offline detection — an offline node stays listed, as OFFLINE, so the
    operator can see it was here and dropped rather than it simply vanishing."""
    with _lock:
        return _nodes.pop(node_id, None) is not None
