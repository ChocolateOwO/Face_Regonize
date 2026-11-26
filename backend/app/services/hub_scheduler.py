"""Optional GPU leases from the Mice Hub's scheduler, for BACKGROUND face work.

On the one Windows PC, Voice (speech to text, answers) and Reconize share the
GPU. The Hub orders GPU work between programs (its main_python/
hub_api_scheduler.py + tools/jobsched.py, verified 2026-11-26):

    POST <hub>/api/scheduler/acquire {"class", "owner", "wait"}
         200 {"ok": true, "token": "L12"}   - the GPU place is ours
         408 {"ok": false, "error": ...}    - still busy after `wait` seconds
         403 non-loopback caller, 400 bad body
    POST <hub>/api/scheduler/release {"token": "L12"} -> {"ok": true|false}

Loopback only; one GPU slot; a lease not released within 60 s is taken back
by the Hub; lower class priority runs first (live 0, speech 1, normal 5,
background 9) with aging, so background work is delayed, never starved.

How Reconize uses it (MICE_HUB_URL + MICE_HUB_GPU_LEASE=1):

* only engine.hold(BACKGROUND, ...) - Event Photo, video, import, enrollment
  and other background model calls - asks, class "background", around ONE
  model call (detect + embed of one image or tile). Decoding, matching and
  disk/network I/O happen outside, so speech waits at most one model call;
* live work (/api/look, look/batch, kiosk, CCTV stations) never asks and
  never waits for the Hub; the Local Agent process opts out entirely;
* the Hub is asked BEFORE the local inference lock is taken, so a live
  caller is never blocked behind a background caller that waits for the Hub;
  one background caller asks at a time (a local gate, not the inference
  lock), and nested holds in one thread never take a second lease;
* fail-open, like the Hub's own LeaseClient: no Hub, an error, a malformed
  answer or a timeout means "go on without a lease" and do not ask again for
  DOWN_SECONDS; 408 (busy) means go on now and ask again next call;
* every acquired lease is released in a finally block (success, error,
  cancellation); a lost release is reclaimed by the Hub's 60 s expiry.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager

import requests

from app.services import hub_link

logger = logging.getLogger(__name__)

CLASS = "background"
DOWN_SECONDS = 30.0
RELEASE_TIMEOUT = 3.0
_TRUE = {"1", "true", "yes", "on"}

_gate = threading.Lock()
_tls = threading.local()
_stats_lock = threading.Lock()
_down_until = 0.0
_opted_out: str | None = None
_stats = {"acquired": 0, "busy_408": 0, "failed_open": 0, "skipped_down": 0, "released": 0,
          "release_failed": 0, "wait_ms_total": 0.0, "wait_ms_max": 0.0}


def opt_out(reason: str) -> None:
    """This process never asks the Hub (the Local Agent: its scans are live)."""
    global _opted_out
    _opted_out = reason


def wait_seconds() -> float:
    try:
        value = float(os.environ.get("MICE_HUB_LEASE_WAIT", "20"))
    except ValueError:
        value = 20.0
    return max(0.0, min(value, 60.0))  # the Hub clamps to 60 s too


def enabled() -> bool:
    return (_opted_out is None and os.environ.get("MICE_HUB_GPU_LEASE", "0").strip().lower() in _TRUE
            and hub_link.base_url() is not None)


def _count(key: str, value: float = 1) -> None:
    with _stats_lock:
        _stats[key] += value


def _post(path: str, body: dict, timeout: float, post=None):
    post = post or requests.post
    return post(hub_link.base_url() + path, data=json.dumps(body), timeout=timeout, allow_redirects=False,
                headers={"Content-Type": "application/json"})


def acquire(workload: str = "", post=None) -> str | None:
    """A lease token, or None to go on without one. Never raises, never waits
    longer than the configured wait plus 3 s."""
    global _down_until
    if time.monotonic() < _down_until:
        _count("skipped_down")
        return None
    wait = wait_seconds()
    began = time.perf_counter()
    try:
        resp = _post("/api/scheduler/acquire", {"class": CLASS, "owner": f"reconize:{workload}"[:40], "wait": wait},
                     timeout=(1.0, wait + 3.0), post=post)
        waited = (time.perf_counter() - began) * 1000
        with _stats_lock:
            _stats["wait_ms_total"] += waited
            _stats["wait_ms_max"] = max(_stats["wait_ms_max"], waited)
        if resp.status_code == 408:
            _count("busy_408")
            return None
        data = resp.json() if resp.status_code == 200 else None
        token = data.get("token") if isinstance(data, dict) and data.get("ok") else None
        if isinstance(token, str) and token:
            _count("acquired")
            return token
        raise ValueError(f"unexpected answer (HTTP {resp.status_code})")
    except Exception as exc:  # noqa: BLE001 - every failure means: go on without a lease
        _down_until = time.monotonic() + DOWN_SECONDS
        _count("failed_open")
        logger.info("Hub GPU lease unavailable (%s); background work continues without it for %.0f s",
                    type(exc).__name__, DOWN_SECONDS)
        return None


def release(token: str, post=None) -> None:
    try:
        _post("/api/scheduler/release", {"token": token}, timeout=RELEASE_TIMEOUT, post=post)
        _count("released")
    except Exception:  # noqa: BLE001 - the Hub's 60 s expiry takes it back
        _count("release_failed")


@contextmanager
def lease(workload: str = ""):
    """Around ONE background model call. A no-op when disabled, opted out, or
    already inside a lease on this thread."""
    if not enabled() or getattr(_tls, "inside", False):
        yield
        return
    _tls.inside = True
    try:
        with _gate:
            token = acquire(workload)
            try:
                yield
            finally:
                if token:
                    release(token)
    finally:
        _tls.inside = False


def stats() -> dict:
    with _stats_lock:
        out = dict(_stats)
    out.update({"enabled": enabled(), "opted_out": _opted_out, "class": CLASS, "wait_seconds": wait_seconds(),
                "down_for_s": round(max(_down_until - time.monotonic(), 0.0), 1)})
    out["wait_ms_total"] = round(out["wait_ms_total"], 1)
    out["wait_ms_max"] = round(out["wait_ms_max"], 1)
    return out


def reset() -> None:
    """Tests."""
    global _down_until, _opted_out
    _down_until = 0.0
    _opted_out = None
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0 if not isinstance(_stats[k], float) else 0.0
