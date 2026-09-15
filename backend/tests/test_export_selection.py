"""Phase O — one ExportSelection for the ZIP download and the Drive upload.

The two regimes are tested separately on purpose:
  * a NEW selection can never contain REVIEW, ORIGINAL or THUMBNAILS;
  * the LEGACY full ZIP (no selection) still carries a historical batch's
    REVIEW/ folder, exactly as before.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from sqlmodel import Session, SQLModel, create_engine

from app.models.models import Person
from app.services import drive_destination_service as dds
from app.services import export_selection_service as ess
from app.services.photo_batch_download_service import output_manifest
from app.services.photo_processing_service import _safe_folder_name

BATCH_ID = "e" * 32


def _img(seed):
    return cv2.imencode(".jpg", np.full((20, 20, 3), seed, np.uint8))[1].tobytes()


class Fixture(unittest.TestCase):
    """A ready batch: p1 (sorted, Alice), p2 (sorted, Bob), p3 (ambience),
    p4 (legacy review) — plus ORIGINAL and THUMBNAILS that must never leak."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-export-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "storage"
        self.root = self.storage / "photo_batches" / BATCH_ID
        self.engine = create_engine(f"sqlite:///{(Path(self.temp.name) / 't.db').as_posix()}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.alice = Person(participant_id="0001", first_name="Alice", last_name="A", image_path="", embedding=b"")
            self.bob = Person(participant_id="0002", first_name="Bob", last_name="B", image_path="", embedding=b"")
            s.add(self.alice)
            s.add(self.bob)
            s.commit()
            self.alice_id, self.bob_id = self.alice.id, self.bob.id
            self.alice_folder = _safe_folder_name("0001", "Alice", "A")
            self.bob_folder = _safe_folder_name("0002", "Bob", "B")

        def put(rel, seed):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_img(seed))

        for i, name in enumerate(("p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"), start=1):
            put(f"MEDIA/{name}", i)
            put(f"ORIGINAL/{name}", 100 + i)
            put(f"THUMBNAILS/{name}.jpg", 200 + i)
        put(f"SORTED/{self.alice_folder}/p1.jpg", 1)
        put(f"SORTED/{self.bob_folder}/p2.jpg", 2)
        put("AMBIENCE/p3.jpg", 3)
        put("REVIEW/p4.jpg", 4)  # a historical, pre-removal REVIEW file

        self.batch = SimpleNamespace(id=BATCH_ID, storage_dir=f"photo_batches/{BATCH_ID}", status="ready",
                                     total_photos=4, processed_photos=4, rejected_photos=0, failed_photos=0,
                                     drive_error=None)
        mk = lambda name, cls, matched: SimpleNamespace(
            batch_id=BATCH_ID, filename=name, media_path=f"photo_batches/{BATCH_ID}/MEDIA/{name}",
            classification=cls, faces_matched=matched)
        self.photos = [mk("p1.jpg", "sorted", 1), mk("p2.jpg", "sorted", 1), mk("p3.jpg", "ambience", 0),
                       mk("p4.jpg", "review", 0)]

    def rel(self, files):
        return sorted(str(f.relative_to(self.root)).replace("\\", "/") for f in files)

    def zip(self, selection, folders=None):
        return self.rel(ess.zip_manifest(self.batch, self.photos, self.storage, selection, folders)[1])


class RuleTests(unittest.TestCase):
    def test_forbidden_roots_are_never_eligible_whatever_is_selected(self):
        everything = ess.ExportSelection(media=True, sorted=True, ambience=True)
        for parts in (("REVIEW", "x.jpg"), ("ORIGINAL", "x.jpg"), ("THUMBNAILS", "x.jpg.jpg"), ("LOGO", "logo.png"),
                      ("MASK", "mask.png"), ("UPLOAD_STAGING", "x.jpg"), ("x.jpg",), ()):
            self.assertFalse(ess.eligible(parts, everything, None), parts)

    def test_query_parsing(self):
        self.assertEqual(ess.parse_query("media,sorted"), ess.ExportSelection(True, True, False, None))
        self.assertEqual(ess.parse_query("ambience", "a, b").person_ids, ("a", "b"))
        for bad in ("", "review", "original", "media,thumbnails"):
            with self.assertRaises(ess.ExportSelectionError, msg=bad):
                ess.parse_query(bad)

    def test_there_is_no_review_option_at_all(self):
        self.assertNotIn("review", ess.CATEGORIES)
        self.assertFalse(hasattr(ess.ExportSelection(), "review"))


class ZipTests(Fixture):
    def test_media_only(self):
        self.assertEqual(self.zip(ess.ExportSelection(media=True, sorted=False)),
                         ["MEDIA/p1.jpg", "MEDIA/p2.jpg", "MEDIA/p3.jpg", "MEDIA/p4.jpg"])

    def test_sorted_for_chosen_participants_only(self):
        self.assertEqual(self.zip(ess.ExportSelection(media=False, sorted=True), {self.bob_folder}),
                         [f"SORTED/{self.bob_folder}/p2.jpg"])

    def test_ambience_is_selectable(self):
        self.assertEqual(self.zip(ess.ExportSelection(media=False, sorted=False, ambience=True)), ["AMBIENCE/p3.jpg"])

    def test_a_new_selection_never_contains_review_original_or_thumbnails(self):
        got = self.zip(ess.ExportSelection(media=True, sorted=True, ambience=True))
        self.assertFalse([p for p in got if p.split("/")[0] in ("REVIEW", "ORIGINAL", "THUMBNAILS")])
        self.assertIn("AMBIENCE/p3.jpg", got)

    def test_the_legacy_full_zip_still_keeps_historical_review(self):
        legacy = self.rel(output_manifest(self.batch, self.photos, self.storage)[1])
        self.assertIn("REVIEW/p4.jpg", legacy)
        self.assertFalse([p for p in legacy if p.split("/")[0] in ("ORIGINAL", "THUMBNAILS")])

    def test_an_empty_match_is_a_clear_error(self):
        with self.assertRaises(ess.ExportSelectionError):
            self.zip(ess.ExportSelection(media=False, sorted=True), {"nobody"})

    def test_selection_does_not_bypass_completeness_validation(self):
        self.batch.processed_photos = 3
        with self.assertRaises(Exception):
            self.zip(ess.ExportSelection())

    def test_folder_names_come_from_the_participants(self):
        with Session(self.engine) as s:
            self.assertEqual(ess.sorted_folder_names(s, [self.alice_id]), {self.alice_folder})
            self.assertIsNone(ess.sorted_folder_names(s, None))
            self.assertEqual(ess.sorted_folder_names(s, []), set())


class ZipArchiveTests(Fixture):
    def names(self, folders):
        import os
        import zipfile

        from app.services.photo_batch_download_service import build_output_zip

        if folders == "legacy":
            root, files = output_manifest(self.batch, self.photos, self.storage)
            archive = build_output_zip(root, files)
        else:
            root, files = ess.zip_manifest(self.batch, self.photos, self.storage,
                                           ess.ExportSelection(media=True, sorted=False), None)
            archive = build_output_zip(root, files, folders=sorted({f.relative_to(root).parts[0] for f in files}))
        try:
            with zipfile.ZipFile(archive) as z:
                return z.namelist()
        finally:
            os.unlink(archive)

    def test_a_media_only_zip_has_no_empty_sorted_folder(self):
        names = self.names("selected")
        self.assertTrue(all(n.startswith("MEDIA/") for n in names), names)
        self.assertIn("MEDIA/", names)

    def test_the_legacy_zip_keeps_its_folder_entries(self):
        names = self.names("legacy")
        for entry in ("SORTED/", "REVIEW/", "MEDIA/"):
            self.assertIn(entry, names)


class DriveTests(Fixture):
    def names(self, files):
        return sorted(f"{f.category}:{f.path.name}" for f in files)

    def test_drive_uses_the_same_rule(self):
        files = dds._iter_upload_files(self.root, True, True, True, {self.alice_folder})
        self.assertEqual(self.names(files), [f"{self.alice_folder}:p1.jpg", "AMBIENCE:p3.jpg", "MEDIA:p1.jpg",
                                             "MEDIA:p2.jpg", "MEDIA:p3.jpg", "MEDIA:p4.jpg"])

    def test_drive_default_is_unchanged_and_never_review_or_original(self):
        files = dds._iter_upload_files(self.root, True, True)
        cats = {f.category for f in files}
        self.assertEqual(cats, {"MEDIA", self.alice_folder, self.bob_folder})
        self.assertFalse({"REVIEW", "ORIGINAL", "THUMBNAILS", "AMBIENCE"} & cats)


class RouteTests(unittest.TestCase):
    def test_the_download_route_keeps_the_legacy_regime_without_a_selection(self):
        import inspect

        from app.api.photo_batches import download_photo_batch

        source = inspect.getsource(download_photo_batch)
        self.assertIn("if selection is None:", source)
        self.assertIn("output_manifest(batch, photos, STORAGE_PATH)", source)
        self.assertIn("zip_manifest", source)

    def test_no_route_parameter_shadows_sqlmodel_select(self):
        import inspect

        from app.api.photo_batches import download_photo_batch

        self.assertNotIn("select", inspect.signature(download_photo_batch).parameters)


if __name__ == "__main__":
    unittest.main()
