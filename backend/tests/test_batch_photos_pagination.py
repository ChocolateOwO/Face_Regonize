"""Phase I4 — batch-photos pagination + SQL-side person_id filtering.

The person_id filter used to load every face for that person across ALL
batches, then every photo in this batch, and intersect them in Python. These
tests pin both the corrected results and the pagination envelope.
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine

from app.api import photo_batches as pb
from app.models.models import PhotoBatch, PhotoBatchFace, PhotoBatchPhoto


class BatchPhotosTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)

        self.batch = self._batch("Batch A")
        self.other_batch = self._batch("Batch B")

        # 45 photos in batch A (40 media, 5 ambience) so the smallest ALLOWED
        # page size (20) genuinely pages. person-1 appears in 25 of them.
        self.photos = []
        for i in range(45):
            classification = "media" if i < 40 else "ambience"
            self.photos.append(self._photo(self.batch.id, f"photo-{i:02d}.jpg", classification))
        for i in range(25):
            self._face(self.photos[i].id, "person-1")
        self._face(self.photos[30].id, "person-2")

        # Same person in a DIFFERENT batch — must never leak into batch A's results.
        other_photo = self._photo(self.other_batch.id, "other.jpg", "media")
        self._face(other_photo.id, "person-1")

    def _batch(self, label):
        b = PhotoBatch(label=label, drive_folder_id="", storage_dir="d")
        self.session.add(b)
        self.session.commit()
        self.session.refresh(b)
        return b

    def _photo(self, batch_id, filename, classification):
        p = PhotoBatchPhoto(batch_id=batch_id, filename=filename, drive_file_id="",
                            original_path=f"o/{filename}", classification=classification)
        self.session.add(p)
        self.session.commit()
        self.session.refresh(p)
        return p

    def _face(self, photo_id, person_id):
        f = PhotoBatchFace(photo_id=photo_id, person_id=person_id, confidence=0.9, bbox="1,2,3,4")
        self.session.add(f)
        self.session.commit()
        return f

    def call(self, **kwargs):
        params = dict(batch_id=self.batch.id, classification=None, person_id=None,
                      page=1, page_size=50, session=self.session, user=None)
        params.update(kwargs)
        return pb.list_batch_photos(**params)

    # ---- envelope ---------------------------------------------------------

    def test_returns_paginated_envelope(self):
        result = self.call()
        self.assertEqual(set(result), {"items", "total", "page", "page_size"})
        self.assertEqual(result["total"], 45)
        self.assertEqual(len(result["items"]), 45)  # default page size 50 holds them all

    def test_pages_do_not_overlap_or_skip(self):
        seen = []
        for page in (1, 2, 3):
            result = self.call(page=page, page_size=20)
            self.assertEqual(result["page_size"], 20)
            seen.extend(p["id"] for p in result["items"])
        self.assertEqual(len(seen), 45, "every photo appears exactly once across pages")
        self.assertEqual(len(set(seen)), 45, "no photo is repeated between pages")

    def test_total_is_the_filtered_total_not_the_page_size(self):
        result = self.call(page=1, page_size=20)
        self.assertEqual(len(result["items"]), 20)
        self.assertEqual(result["total"], 45, "total must describe the whole filtered set")

    def test_invalid_page_size_falls_back_to_default(self):
        self.assertEqual(self.call(page_size=999)["page_size"], pb.DEFAULT_PAGE_SIZE)

    def test_page_below_one_is_clamped(self):
        self.assertEqual(self.call(page=0)["page"], 1)

    def test_page_past_the_end_is_empty_but_reports_the_real_total(self):
        result = self.call(page=99, page_size=20)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 45)

    # ---- filtering --------------------------------------------------------

    def test_classification_filter(self):
        self.assertEqual(self.call(classification="media")["total"], 40)
        self.assertEqual(self.call(classification="ambience")["total"], 5)

    def test_person_filter_returns_only_that_person_s_photos(self):
        result = self.call(person_id="person-1")
        self.assertEqual(result["total"], 25)
        self.assertEqual({p["filename"] for p in result["items"]},
                         {f"photo-{i:02d}.jpg" for i in range(25)})

    def test_person_filter_is_scoped_to_this_batch(self):
        """The same person also has a face in another batch; it must not leak."""
        result = self.call(person_id="person-1")
        self.assertTrue(all(p["filename"] != "other.jpg" for p in result["items"]))

    def test_person_filter_paginates(self):
        page1 = self.call(person_id="person-1", page=1, page_size=20)
        page2 = self.call(person_id="person-1", page=2, page_size=20)
        self.assertEqual(page1["total"], 25)
        self.assertEqual(len(page1["items"]), 20)
        self.assertEqual(len(page2["items"]), 5)
        self.assertFalse({p["id"] for p in page1["items"]} & {p["id"] for p in page2["items"]})

    def test_person_filter_with_no_matches(self):
        result = self.call(person_id="nobody")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["items"], [])

    def test_unknown_batch_is_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.call(batch_id="does-not-exist")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_item_shape_is_unchanged(self):
        """The per-photo fields the frontend renders must not have moved."""
        item = self.call()["items"][0]
        self.assertEqual(set(item), {
            "id", "filename", "classification", "faces_total", "faces_matched",
            "faces_unknown", "original_path", "media_path", "thumbnail_path",
            "drive_upload_status", "drive_error",
        })

    def test_filtering_happens_in_sql_not_python(self):
        """Guards the actual point of this phase: no full-table load followed
        by an in-Python intersection."""
        import inspect

        source = inspect.getsource(pb.list_batch_photos)
        self.assertNotIn("for p in photos if p.id in photo_ids", source)
        self.assertIn(".in_(", source)
        self.assertIn(".offset(", source)
        self.assertIn(".limit(", source)


if __name__ == "__main__":
    unittest.main()
