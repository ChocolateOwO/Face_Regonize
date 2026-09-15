"""PipelineRegistry (Phase B) — tracks active Event Photo pipeline runs.

Consulted by THREE places: cancel-batch and retention cleanup (both need
STOP-before-DELETE — never delete a batch's files while a pipeline is still
writing them), and later Phase P1's update preflight check (never start an
application update while Event Photo work is in flight). Only
workflow_version=2 (pipeline) batches are ever registered here —
workflow_version=1 batches continue to be tracked solely by
photo_processing_service's existing _active_batches set, unchanged.
"""
from __future__ import annotations

import threading


class PipelineHandle:
    """One running pipeline instance for one batch. `finished` is set by the
    pipeline's own run() when it returns, whether it completed normally or
    was cancelled — shutdown_and_join() only ever waits on that, so it can
    never return True while the pipeline is still touching files."""

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        self.cancel_event = threading.Event()
        self._finished = threading.Event()

    def mark_finished(self) -> None:
        self._finished.set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def shutdown_and_join(self, timeout: float = 30) -> bool:
        """Requests cancellation and waits up to `timeout` seconds for the
        pipeline to actually stop. Returns True iff it stopped in time —
        callers (cancel-batch, retention) MUST treat False as "do not
        delete anything yet", the same hard contract the existing
        sequential path's cancel_and_delete_batch already has."""
        self.cancel_event.set()
        return self._finished.wait(timeout=timeout)


class PipelineRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[str, PipelineHandle] = {}

    def register(self, batch_id: str) -> PipelineHandle:
        handle = PipelineHandle(batch_id)
        with self._lock:
            self._handles[batch_id] = handle
        return handle

    def unregister(self, batch_id: str) -> None:
        with self._lock:
            self._handles.pop(batch_id, None)

    def get(self, batch_id: str) -> PipelineHandle | None:
        with self._lock:
            return self._handles.get(batch_id)

    def is_active(self, batch_id: str) -> bool:
        with self._lock:
            return batch_id in self._handles

    def active_batch_ids(self) -> list[str]:
        with self._lock:
            return list(self._handles.keys())

    def any_active(self) -> bool:
        with self._lock:
            return bool(self._handles)

    def shutdown_and_join_if_active(self, batch_id: str, timeout: float = 30) -> bool:
        """True if there was nothing to stop, OR it stopped in time. False
        means a pipeline is still running for this batch after the
        timeout — callers must refuse to proceed (STOP-before-DELETE)."""
        handle = self.get(batch_id)
        if handle is None:
            return True
        return handle.shutdown_and_join(timeout)


registry = PipelineRegistry()
