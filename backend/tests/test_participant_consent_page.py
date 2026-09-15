"""People + PDPA are one participant page now.

The move is deliberately frontend-only: consent semantics must not change just
because the UI did. These tests pin that — the same endpoints, the same
append-only history, the same "latest answer wins" rule — plus the one small
additive field the merged page needs.
"""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine, select

from app.api.pdpa import person_status
from app.models.models import ConsentRecord, Person


class ConsentStatusShapeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)
        self.person = Person(participant_id="0001", first_name="Ann", last_name="Lee",
                             image_path="", embedding=b"")
        self.session.add(self.person)
        self.session.commit()
        self.session.refresh(self.person)

    def record(self, choice, source, minutes_ago=0):
        self.session.add(ConsentRecord(
            person_id=self.person.id, choice=choice, source=source,
            recorded_at=datetime.now() - timedelta(minutes=minutes_ago)))
        self.session.commit()

    def status(self):
        return person_status(self.person.id, session=self.session, user=None)

    def test_the_page_gets_everything_it_renders(self):
        self.record("consented", "kiosk")
        payload = self.status()
        for field in ("person_id", "participant_id", "first_name", "last_name",
                      "status", "last_updated", "source", "history"):
            self.assertIn(field, payload, field)

    def test_source_reports_where_the_CURRENT_answer_came_from(self):
        self.record("consented", "registration", minutes_ago=10)
        self.record("declined", "kiosk", minutes_ago=1)
        payload = self.status()
        self.assertEqual(payload["status"], "declined")
        self.assertEqual(payload["source"], "kiosk",
                         "a form answer is not the same evidence as the kiosk")

    def test_a_participant_who_never_answered_is_pending_with_no_source(self):
        payload = self.status()
        self.assertEqual(payload["status"], "pending")
        self.assertIsNone(payload["source"])
        self.assertEqual(payload["history"], [])

    def test_latest_answer_wins_regardless_of_who_recorded_it(self):
        self.record("declined", "kiosk", minutes_ago=5)
        self.record("consented", "admin", minutes_ago=1)
        payload = self.status()
        self.assertEqual(payload["status"], "consented")
        self.assertEqual(payload["source"], "admin")

    def test_history_is_newest_first_and_keeps_every_entry(self):
        self.record("consented", "registration", minutes_ago=30)
        self.record("declined", "kiosk", minutes_ago=20)
        self.record("consented", "admin", minutes_ago=10)
        history = self.status()["history"]
        self.assertEqual([h["choice"] for h in history], ["consented", "declined", "consented"])
        self.assertEqual([h["source"] for h in history], ["admin", "kiosk", "registration"])

    def test_an_admin_override_appends_and_never_rewrites(self):
        """ConsentRecord stays append-only: the participant's own earlier
        answers remain visible after an admin sets a different one."""
        self.record("declined", "kiosk", minutes_ago=5)
        before = len(self.session.exec(select(ConsentRecord)).all())
        self.record("consented", "admin")
        rows = self.session.exec(select(ConsentRecord)).all()
        self.assertEqual(len(rows), before + 1)
        self.assertIn("declined", [r.choice for r in rows])


class RouteContractTests(unittest.TestCase):
    """The merged page must reuse the endpoints that already existed."""

    def test_the_per_person_status_route_still_exists(self):
        from app.api.pdpa import router

        self.assertIn("/api/pdpa/status/{person_id}", [r.path for r in router.routes])

    def test_the_record_route_still_exists(self):
        from app.api.pdpa import router

        self.assertIn("/api/pdpa/record", [r.path for r in router.routes])

    def test_no_new_consent_endpoint_was_added_for_the_merged_page(self):
        from app.api.people import router

        self.assertEqual([r.path for r in router.routes if "consent" in r.path or "pdpa" in r.path], [])


if __name__ == "__main__":
    unittest.main()
