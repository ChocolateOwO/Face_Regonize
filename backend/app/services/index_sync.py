"""Keeps the in-memory recognition index consistent with the database.

The database is the durable source of truth; the recognition index is a
derived cache, rebuilt from the database at every startup
(main.py -> recognition_index.rebuild(...)). It has no persistence of its own.

Participant mutations therefore commit first and update the index second. That
ordering is deliberate and correct - putting the index first would let RAM hold
data no committed transaction ever produced - but it leaves one gap: if the
index mutation fails after the commit, the database moves on while RAM keeps
the old state, and recognition silently disagrees with the database until the
backend restarts.

This module closes that gap. When an index mutation fails, the index is
rebuilt from the committed database state, which is by definition the truth.
Recovery only ever runs on the exceptional path; normal create/update/delete
keep their cheap incremental upsert/remove.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlmodel import Session, select

from app.face_recognition.index import recognition_index
from app.models.models import Person

logger = logging.getLogger(__name__)

# What the API should report when the index mutation failed but the rebuild
# put things right. The database change is committed and the index now agrees
# with it, so the request genuinely did what the caller asked - reporting a
# failure would invite a retry of an operation that already succeeded.
# Set to True to surface the incident to the caller as a 500 instead.
FAIL_REQUEST_AFTER_SUCCESSFUL_RECOVERY = False


class IndexResyncFailed(RuntimeError):
    """The index mutation failed AND rebuilding from the database failed.

    The database is still correct, but the in-memory index can no longer be
    trusted, so this is never swallowed - the caller turns it into a 500 and
    the incident is logged. A backend restart rebuilds the index from the
    database and clears the condition.
    """


def apply_index_change(session: Session, mutate: Callable[[], None], *, what: str) -> str:
    """Run an index mutation that follows an already-committed DB change.

    Returns "ok" on the normal path, or "recovered" when the mutation failed
    and a rebuild from the database restored agreement. Raises
    IndexResyncFailed when the rebuild also fails.
    """
    try:
        mutate()
        return "ok"
    except Exception as mutation_error:  # noqa: BLE001 - any index failure must be recoverable
        logger.exception(
            "Recognition index update failed after the database was committed (%s). "
            "Rebuilding the index from the database.", what
        )
        try:
            recognition_index.rebuild(session.exec(select(Person)).all())
        except Exception as rebuild_error:  # noqa: BLE001
            logger.critical(
                "Recognition index rebuild ALSO failed after %s. The database is correct but the "
                "in-memory index is unreliable until the backend is restarted.", what
            )
            raise IndexResyncFailed(
                "The change was saved, but the recognition index could not be updated or rebuilt. "
                "Restart the backend to reload it from the database."
            ) from rebuild_error

        logger.warning(
            "Recognition index rebuilt from the database after a failed %s; "
            "database and index agree again (%d entries).", what, recognition_index.size()
        )
        if FAIL_REQUEST_AFTER_SUCCESSFUL_RECOVERY:
            raise IndexResyncFailed(
                "The change was saved and the recognition index was rebuilt to match."
            ) from mutation_error
        return "recovered"
