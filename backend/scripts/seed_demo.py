"""Seeds a couple of demo participants so the app can be tried out immediately.

Uses the two real test photos already in backend/test_images/ (there's no
legitimate free source of extra face photos to fabricate more), each clearly
flagged is_demo=True. Add more people via the Register page or bulk Import
for a meaningful multi-person recognition test.

Run: .venv\\Scripts\\python.exe scripts\\seed_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app.database.db import engine, init_db
from app.face_recognition.engine import detect_faces
from app.models.models import Person
from app.services import storage_service

TEST_IMAGES_DIR = Path(__file__).resolve().parent.parent / "test_images"

DEMO_PEOPLE = [
    ("DEMO001", "Demo", "One", "demo1@example.com", "you_1.jpg"),
    ("DEMO002", "Demo", "Two", "demo2@example.com", "other_1.jpg"),
]


def main() -> None:
    init_db()
    with Session(engine) as session:
        for participant_id, first, last, email, photo_name in DEMO_PEOPLE:
            existing = session.exec(select(Person).where(Person.participant_id == participant_id)).first()
            if existing:
                print(f"  {participant_id} already exists, skipping")
                continue

            photo_path = TEST_IMAGES_DIR / photo_name
            if not photo_path.exists():
                print(f"  [WARN] {photo_path} not found, skipping {participant_id}")
                continue

            file_bytes = photo_path.read_bytes()
            img = storage_service.decode_image(file_bytes)
            faces = detect_faces(img)
            if not faces:
                print(f"  [WARN] no face detected in {photo_name}, skipping {participant_id}")
                continue
            face = max(faces, key=lambda f: f.det_score)

            image_path = storage_service.save_person_image(participant_id, file_bytes)
            person = Person(
                participant_id=participant_id,
                first_name=first,
                last_name=last,
                email=email,
                image_path=image_path,
                image_source="manual",
                embedding=face.embedding.tobytes(),
                det_score=face.det_score,
                is_demo=True,
            )
            session.add(person)
            session.commit()
            print(f"  seeded {participant_id} ({first} {last})")

    print("Done.")


if __name__ == "__main__":
    main()
