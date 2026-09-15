"""Step 10 — I1 People / I2 PDPA / I3 Attendees pagination.

Pins the envelope, real offsets, server-side search/filter, counts over
everyone, invalid page sizes, and — for every endpoint — that callers who do
not pass `page` still get the exact bare list they got before (consumer
compatibility)."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine

from app.api import attendees, pdpa, people
from app.models.models import Attendance, ConsentRecord, Person, User

USER = User(username="admin", password_hash="x")


class ListCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-pagination-")
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine(f"sqlite:///{(Path(self.temp.name) / 't.db').as_posix()}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        base = datetime(2026, 1, 1)
        self.ids = []
        with Session(self.engine) as s:
            for i in range(25):
                p = Person(participant_id=f"{i + 1:04d}", first_name=f"Name{i}", last_name="Alpha" if i % 5 == 0 else "Beta",
                           email=f"p{i}@x.test" if i % 2 else None, image_path="", embedding=b"",
                           created_at=base + timedelta(minutes=i))
                s.add(p)
                s.commit()
                s.refresh(p)
                self.ids.append(p.id)
                if i % 3 == 0:
                    # upload/face-detection ids are placeholders: the listing reads only
                    # person_id and detected_at, and FK enforcement is off.
                    for hours in (1, 2):
                        s.add(Attendance(person_id=p.id, upload_id="u", face_detection_id="f", confidence=0.9,
                                         detected_at=base + timedelta(hours=hours)))
                if i % 4 == 0:
                    s.add(ConsentRecord(person_id=p.id, choice="consented", source="kiosk", recorded_at=base))
                    if i % 8 == 0:
                        s.add(ConsentRecord(person_id=p.id, choice="declined", source="admin",
                                            recorded_at=base + timedelta(days=1)))
            s.commit()

    def session(self):
        s = Session(self.engine)
        self.addCleanup(s.close)
        return s


class PeopleTests(ListCase):
    def test_without_page_the_bare_list_is_unchanged(self):
        out = people.list_people(q=None, page=None, session=self.session(), user=USER)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 25)

    def test_pages_are_real_offsets_in_creation_order(self):
        s = self.session()
        first = people.list_people(q=None, page=1, page_size=20, session=s, user=USER)
        second = people.list_people(q=None, page=2, page_size=20, session=s, user=USER)
        self.assertEqual((first["total"], first["page"], first["page_size"], len(first["items"])), (25, 1, 20, 20))
        self.assertEqual(len(second["items"]), 5)
        self.assertEqual([p["id"] for p in first["items"] + second["items"]], self.ids)

    def test_search_runs_on_the_server_across_name_id_and_email(self):
        s = self.session()
        self.assertEqual(people.list_people(q="alpha", page=1, page_size=50, session=s, user=USER)["total"], 5)
        self.assertEqual(people.list_people(q="0013", page=1, page_size=50, session=s, user=USER)["total"], 1)
        self.assertEqual(people.list_people(q="p3@x", page=1, page_size=50, session=s, user=USER)["total"], 1)

    def test_detection_summaries_are_correct_on_a_page(self):
        row = people.list_people(q="0004", page=1, page_size=50, session=self.session(), user=USER)["items"][0]
        self.assertEqual(row["detection_count"], 2)

    def test_an_invalid_page_size_falls_back_and_page_is_at_least_one(self):
        out = people.list_people(q=None, page=0, page_size=7, session=self.session(), user=USER)
        self.assertEqual((out["page"], out["page_size"]), (1, 50))


class AttendeeTests(ListCase):
    def test_without_page_the_bare_list_is_unchanged(self):
        out = attendees.list_attendees(page=None, session=self.session(), user=USER)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 25)

    def test_in_event_counts_everyone_not_just_the_page(self):
        out = attendees.list_attendees(page=2, page_size=20, session=self.session(), user=USER)
        self.assertEqual((out["total"], len(out["items"]), out["in_event"]), (25, 5, 9))
        self.assertTrue(all(r["status"] in ("in_event", "not_detected") for r in out["items"]))


class PdpaTests(ListCase):
    def test_without_page_the_bare_list_is_unchanged(self):
        out = pdpa.list_status(page=None, session=self.session(), user=USER)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 25)

    def test_counts_cover_everyone_and_use_the_latest_answer(self):
        out = pdpa.list_status(page=1, page_size=20, session=self.session(), user=USER)
        self.assertEqual(out["counts"], {"all": 25, "consented": 3, "declined": 4, "pending": 18})

    def test_status_filter_and_search_apply_before_paging(self):
        s = self.session()
        declined = pdpa.list_status(page=1, page_size=20, status="declined", session=s, user=USER)
        self.assertEqual(declined["total"], 4)
        self.assertTrue(all(r["status"] == "declined" for r in declined["items"]))
        found = pdpa.list_status(page=1, page_size=20, q="name12", session=s, user=USER)
        self.assertEqual([r["participant_id"] for r in found["items"]], ["0013"])
        self.assertEqual(found["counts"]["all"], 25, "tab counts ignore the search")

    def test_paging_slices_the_filtered_rows(self):
        out = pdpa.list_status(page=2, page_size=20, status="pending", session=self.session(), user=USER)
        self.assertEqual((out["total"], len(out["items"])), (18, 0))
        out = pdpa.list_status(page=1, page_size=20, status="pending", session=self.session(), user=USER)
        self.assertEqual(len(out["items"]), 18)


if __name__ == "__main__":
    unittest.main()
