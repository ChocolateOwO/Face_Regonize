"""Phase P1 — preflight() refuses while Event Photo work is in flight.

Three independent signals must each block an update, because none of them
alone sees all in-flight work: the batch's persisted status, the in-memory
PipelineRegistry (worker threads still touching files), and the Drive upload
reservation guard.

preflight() runs many checks before reaching the Event gate (git, GitHub,
disk...). These tests drive the gate directly rather than standing up a git
remote — the gate's own logic is what Phase P1 adds.
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine

from app.models.models import PhotoBatch
from app.services import drive_destination_service, update_service
from app.services.event_pipeline_registry import registry as pipeline_registry


class EventActiveJobGateTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        p = patch.object(update_service, "engine", self.engine)
        p.start()
        self.addCleanup(p.stop)
        # leave the guards clean no matter how a test exits
        self.addCleanup(drive_destination_service._uploading_batches.clear)

    def add_batch(self, status: str, label: str = "Batch A") -> str:
        with Session(self.engine) as session:
            batch = PhotoBatch(label=label, drive_folder_id="f", storage_dir="d", status=status)
            session.add(batch)
            session.commit()
            session.refresh(batch)
            return batch.id

    def run_gate(self):
        return update_service.refuse_if_event_work_in_flight()

    def test_processing_batch_blocks_update(self):
        self.add_batch("processing", "Graduation")
        with self.assertRaises(update_service.UpdateError) as ctx:
            self.run_gate()
        self.assertIn("Graduation", str(ctx.exception))

    def test_uploading_batch_blocks_update(self):
        """A Drive upload mid-flight must not be cut off by a restart — the
        same state retention refuses to delete."""
        self.add_batch("uploading", "Uploading Batch")
        with self.assertRaises(update_service.UpdateError) as ctx:
            self.run_gate()
        self.assertIn("Uploading Batch", str(ctx.exception))

    def test_registered_pipeline_handle_blocks_update_even_when_status_is_terminal(self):
        """The DB row can say 'ready' while workflow_version=2 worker threads
        are still finishing — the registry is the authority on that."""
        batch_id = self.add_batch("ready", "Done On Paper")
        pipeline_registry.register(batch_id)
        self.addCleanup(pipeline_registry.unregister, batch_id)
        with self.assertRaises(update_service.UpdateError) as ctx:
            self.run_gate()
        self.assertIn(batch_id, str(ctx.exception))

    def test_drive_upload_reservation_blocks_update(self):
        self.add_batch("ready")
        drive_destination_service._uploading_batches.add("batch-xyz")
        with self.assertRaises(update_service.UpdateError) as ctx:
            self.run_gate()
        self.assertIn("batch-xyz", str(ctx.exception))

    def test_idle_system_passes_the_event_gate(self):
        """Regression guard: with nothing in flight the gate must not fire —
        this phase must not break the common case."""
        self.add_batch("ready")
        self.add_batch("completed")
        self.assertFalse(pipeline_registry.any_active())
        self.assertFalse(drive_destination_service._uploading_batches)
        self.run_gate()  # must not raise

    def test_preflight_calls_the_gate_before_starting_an_update(self):
        """The gate is worthless if preflight() stops calling it. preflight()
        cannot be driven end to end here (it needs a configured GitHub repo,
        a git remote and disk checks), so this asserts the wiring structurally
        — the same approach other tests in this suite use for code shape."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(update_service.preflight))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("refuse_if_event_work_in_flight", called)


if __name__ == "__main__":
    unittest.main()
