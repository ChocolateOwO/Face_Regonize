"""Phase L1 — Persistent participant-ID high-water allocator.

Replaces the old live-MAX()-scan (which let a deleted top-numbered
participant's ID be reissued) with a persistent Setting-backed counter.
Runs the real import_service functions against an isolated temp DB.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine, select

import app.services.import_service as import_service
from app.models.models import Person, Setting


class ParticipantIdCounterTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-participant-id-test-")
        self.addCleanup(self.temp.cleanup)
        db_path = Path(self.temp.name) / "test.db"
        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

    def _add_person(self, participant_id: str):
        with Session(self.engine) as session:
            person = Person(
                participant_id=participant_id, first_name="X", last_name="", image_path="",
                embedding=b"\x00" * (512 * 4),
            )
            session.add(person)
            session.commit()
            session.refresh(person)
            return person

    def _setting_value(self):
        with Session(self.engine) as session:
            row = session.get(Setting, import_service._PARTICIPANT_ID_HIGH_WATER_KEY)
            return row.value if row else None


class BlankFieldAndSeedingTests(ParticipantIdCounterTestCase):
    def test_first_use_seeds_from_live_scan_result(self):
        self._add_person("0005")
        self._add_person("0003")
        with Session(self.engine) as session:
            next_id = import_service.next_auto_id_start(session)
        self.assertEqual(next_id, 6)  # seeded from MAX=5, then +1
        self.assertEqual(self._setting_value(), "5")

    def test_blank_field_gets_correct_next_id_not_a_rescan(self):
        self._add_person("0007")
        with Session(self.engine) as session:
            first = import_service.next_auto_id_start(session)
        self.assertEqual(first, 8)
        # Deleting a person afterward must NOT change the next suggestion —
        # a fresh MAX() scan would recompute a lower number; the persistent
        # counter must not.
        with Session(self.engine) as session:
            p = session.exec(select(Person).where(Person.participant_id == "0007")).first()
            session.delete(p)
            session.commit()
        with Session(self.engine) as session:
            second = import_service.next_auto_id_start(session)
        self.assertEqual(second, 8)

    def test_preview_alone_never_advances_the_counter(self):
        """Calling next_auto_id_start() repeatedly (as a preview would)
        must not itself consume/advance anything."""
        self._add_person("0002")
        with Session(self.engine) as session:
            for _ in range(5):
                self.assertEqual(import_service.next_auto_id_start(session), 3)
        self.assertEqual(self._setting_value(), "2")


class DeletedTopIdNeverReissuedTests(ParticipantIdCounterTestCase):
    def test_delete_then_reissue_regression(self):
        """The specific bug this phase fixes: create up to 0006, delete it,
        confirm the next blank-field add is 0007, not 0006."""
        for n in ("0004", "0005", "0006"):
            self._add_person(n)
        with Session(self.engine) as session:
            import_service.next_auto_id_start(session)  # seeds the counter at 6

        with Session(self.engine) as session:
            p = session.exec(select(Person).where(Person.participant_id == "0006")).first()
            session.delete(p)
            session.commit()

        with Session(self.engine) as session:
            next_id = import_service.next_auto_id_start(session)
        self.assertEqual(next_id, 7, "a deleted top-numbered ID must never be reissued")


class BumpHighWaterTests(ParticipantIdCounterTestCase):
    def test_manual_custom_id_above_mark_advances_counter(self):
        self._add_person("0002")
        with Session(self.engine) as session:
            import_service.next_auto_id_start(session)  # seed at 2
            import_service.bump_high_water_if_greater(session, "0050")
        self.assertEqual(self._setting_value(), "50")
        with Session(self.engine) as session:
            self.assertEqual(import_service.next_auto_id_start(session), 51)

    def test_manual_custom_id_below_mark_does_not_move_backward(self):
        self._add_person("0010")
        with Session(self.engine) as session:
            import_service.next_auto_id_start(session)  # seed at 10
            import_service.bump_high_water_if_greater(session, "0003")
        self.assertEqual(self._setting_value(), "10")

    def test_alphabetic_custom_id_does_not_disturb_the_counter(self):
        with Session(self.engine) as session:
            import_service.next_auto_id_start(session)  # seed at 0
            import_service.bump_high_water_if_greater(session, "VIP-GUEST")
        self.assertEqual(self._setting_value(), "0")

    def test_bump_with_no_setting_row_yet_falls_back_to_live_scan(self):
        self._add_person("0020")
        with Session(self.engine) as session:
            # No next_auto_id_start() call yet — Setting row does not exist.
            import_service.bump_high_water_if_greater(session, "0025")
        self.assertEqual(self._setting_value(), "25")

    def test_concurrent_style_sequential_calls_never_collide(self):
        """SQLite is single-writer; simulate several rapid allocations and
        confirm every issued number is unique and strictly increasing."""
        issued = []
        with Session(self.engine) as session:
            for _ in range(20):
                next_id = import_service.next_auto_id_start(session)
                participant_id = f"{next_id:04d}"
                import_service.bump_high_water_if_greater(session, participant_id)
                issued.append(next_id)
        self.assertEqual(issued, list(range(1, 21)))
        self.assertEqual(len(issued), len(set(issued)))


if __name__ == "__main__":
    unittest.main()
