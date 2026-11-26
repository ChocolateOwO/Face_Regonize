"""The same stable person_id in /api/people (list + detail), /api/history and
/api/look, and history times carry their zone offset (Hub contract A6)."""
from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.api import history, people  # noqa: E402
from app.models.models import FaceDetection, Person, Upload, User  # noqa: E402


class StablePersonIdTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            p = Person(participant_id="P-1", first_name="Ann", last_name="A", image_path="",
                       embedding=np.ones(512, np.float32).tobytes())
            u = Upload(filename="CAM-01_scan.jpg", image_path="")
            s.add(p)
            s.add(u)
            s.commit()
            s.refresh(p)
            s.refresh(u)
            s.add(FaceDetection(upload_id=u.id, person_id=p.id, confidence=0.9, status="matched", bbox="0,0,10,10",
                                detected_at=datetime(2026, 11, 26, 10, 0, 0)))
            s.commit()
            self.pid = p.id
        self.user = User(username="a", password_hash="x", role="admin")

    def test_people_list_and_detail_carry_person_id(self):
        with Session(self.engine) as s:
            listed = people._person_out(s, s.get(Person, self.pid))
            detail = people.get_person(self.pid, session=s, user=self.user)
        self.assertEqual(listed["person_id"], self.pid)
        self.assertEqual(listed["id"], self.pid, "the existing field is unchanged")
        self.assertEqual(detail["person_id"], self.pid)

    def test_history_rows_carry_the_same_id_and_an_aware_time(self):
        with Session(self.engine) as s:
            out = history.recognition_history(session=s, user=self.user)
        row = out["items"][0]
        self.assertEqual(row["person_id"], self.pid)
        self.assertRegex(str(row["detected_at"]), r"[+-]\d\d:\d\d$")

    def test_look_contract_names_person_id(self):
        src = (Path(__file__).resolve().parents[1] / "app" / "services" / "look_service.py").read_text(encoding="utf-8")
        self.assertTrue(re.search(r"entry\.update\(person_id=person_id", src))


if __name__ == "__main__":
    unittest.main()
