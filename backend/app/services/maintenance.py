"""Maintenance mode — the write lock held during the critical update window.

Deliberately NOT a process-wide flag. The backend is stopped and restarted
part-way through an update, so anything kept only in memory would be lost at
exactly the wrong moment: the newly started backend would happily accept
check-ins and imports while migrations were still being verified and while a
rollback might still restore a database snapshot taken before those writes.

The authority is therefore the runtime update_state.json, which lives outside
the repository and outside the database and survives both the restart and any
database rollback. A fresh backend reads that file and knows an update is
still in flight.

The in-memory value here is only a short-TTL cache to keep the middleware from
stat-ing the file on every single request.
"""
from __future__ import annotations

import json
import logging
import time

from app.config import UPDATE_STATE_FILE

logger = logging.getLogger(__name__)

# Statuses that mean "an update is in flight, writes must be refused".
IN_PROGRESS_STATUSES = {
    "preparing",
    "backing_up",
    "updating",
    "restarting",
    "verifying",
    "rolling_back",
}

TERMINAL_STATUSES = {"completed", "failed", "rolled_back", "idle"}

_CACHE_TTL_SECONDS = 2.0
_cached_active: bool = False
_cached_reason: str = ""
_cached_at: float = 0.0


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import ctypes

        # Windows: OpenProcess(SYNCHRONIZE) succeeds only for a live process.
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    except (AttributeError, OSError):
        # Non-Windows fallback.
        import os

        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def read_state() -> dict | None:
    """The raw update state, or None when no update has ever run here."""
    try:
        with open(UPDATE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _compute() -> tuple[bool, str]:
    state = read_state()
    if not state:
        return False, ""
    status = str(state.get("status", ""))
    if status not in IN_PROGRESS_STATUSES:
        return False, ""

    # An in-progress state whose updater is gone means the updater crashed or
    # was killed. Refusing writes forever would leave the whole app wedged
    # read-only with no way back, so treat it as abandoned and let the app
    # run; the admin is told separately that an update did not finish.
    pid = state.get("updater_pid") or 0
    if not _pid_alive(int(pid)):
        logger.warning(
            "Update state is '%s' but updater pid %s is not running — treating the update as abandoned "
            "and releasing maintenance mode.",
            status,
            pid,
        )
        return False, ""

    return True, str(state.get("stage") or "Update in progress")


def is_active() -> bool:
    return _status()[0]


def reason() -> str:
    return _status()[1]


def _status() -> tuple[bool, str]:
    global _cached_active, _cached_reason, _cached_at
    now = time.monotonic()
    if now - _cached_at > _CACHE_TTL_SECONDS:
        _cached_active, _cached_reason = _compute()
        _cached_at = now
    return _cached_active, _cached_reason


def invalidate_cache() -> None:
    """Force the next check to re-read the state file."""
    global _cached_at
    _cached_at = 0.0


def abandoned_update() -> dict | None:
    """An in-progress update whose updater process is dead — surfaced to the
    admin so a crashed update is visible rather than silent."""
    state = read_state()
    if not state:
        return None
    if str(state.get("status", "")) not in IN_PROGRESS_STATUSES:
        return None
    if _pid_alive(int(state.get("updater_pid") or 0)):
        return None
    return state
