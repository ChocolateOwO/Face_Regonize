"""Phase I4/J1 — grid thumbnails.

The measured problem was payload size: a 50-photo page pulled ~270 MB of
full-resolution artifacts. These tests pin the two rules that make the fix
safe as well as fast — thumbnails are derived from the PRIVACY-RENDERED bytes,
never the original, and there is exactly ONE per photo however many SORTED
folders it belongs to — plus the cache-header split that keeps the documented
`people/` face-leak protection intact.
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from PIL import Image

from app.api.files import _IMMUTABLE, _REVALIDATE, _cache_control
from app.services import photo_thumbnail_service as pts


def _png(seed: int, height: int = 900, width: int = 1600) -> bytes:
    img = np.random.default_rng(seed).integers(0, 255, (height, width, 3), dtype=np.uint8)
    return cv2.imencode(".png", img)[1].tobytes()


class ThumbnailEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-thumb-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name)

    def test_a_thumbnail_is_written_and_is_bounded_to_320px(self):
        dest = self.storage / "t.jpg"
        self.assertTrue(pts.write_thumbnail(dest, _png(1)))
        with Image.open(dest) as img:
            self.assertLessEqual(max(img.size), 320)

    def test_a_thumbnail_is_dramatically_smaller_than_the_source(self):
        """The whole point of the phase — asserted as a ratio, not a fixed
        byte count, so it stays meaningful as test images change."""
        source = _png(2)
        dest = self.storage / "t.jpg"
        pts.write_thumbnail(dest, source)
        self.assertLess(dest.stat().st_size * 20, len(source))

    def test_aspect_ratio_is_preserved(self):
        dest = self.storage / "t.jpg"
        pts.write_thumbnail(dest, _png(3, height=600, width=1200))
        with Image.open(dest) as img:
            self.assertAlmostEqual(img.size[0] / img.size[1], 2.0, places=1)

    def test_undecodable_bytes_fail_softly(self):
        """A missing thumbnail degrades the grid to full resolution, which is
        slow but correct. Failing the photo's write would not be."""
        dest = self.storage / "t.jpg"
        self.assertFalse(pts.write_thumbnail(dest, b"not an image"))
        self.assertFalse(dest.exists())

    def test_no_temp_file_is_left_behind(self):
        dest = self.storage / "t.jpg"
        pts.write_thumbnail(dest, _png(4))
        self.assertEqual([p.name for p in self.storage.iterdir()], ["t.jpg"])


class ThumbnailSourceTests(unittest.TestCase):
    """Where a thumbnail comes FROM is a privacy question, not a detail."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-thumb-src-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name)
        self.batch_dir = "photo_batches/b1"
        (self.storage / self.batch_dir / "ORIGINAL").mkdir(parents=True)
        (self.storage / self.batch_dir / "MEDIA").mkdir(parents=True)
        # Deliberately different images so the assertion below can tell which
        # one a thumbnail was actually derived from.
        (self.storage / self.batch_dir / "ORIGINAL" / "p.png").write_bytes(_png(10))
        (self.storage / self.batch_dir / "MEDIA" / "p.png").write_bytes(_png(11))

    @property
    def thumb_rel(self):
        return pts.thumbnail_relpath(self.batch_dir, "p.png")

    def ensure(self, thumbnail_rel=None):
        return pts.ensure_thumbnail(self.storage, thumbnail_rel or self.thumb_rel)

    def test_it_is_derived_from_the_rendered_media_copy(self):
        self.assertTrue(self.ensure())
        made = (self.storage / self.thumb_rel).read_bytes()

        from_media = self.storage / "media-ref.jpg"
        pts.write_thumbnail(from_media, (self.storage / self.batch_dir / "MEDIA" / "p.png").read_bytes())
        from_original = self.storage / "original-ref.jpg"
        pts.write_thumbnail(from_original, (self.storage / self.batch_dir / "ORIGINAL" / "p.png").read_bytes())

        self.assertEqual(made, from_media.read_bytes())
        self.assertNotEqual(made, from_original.read_bytes(),
                            "a thumbnail of ORIGINAL would put an unmasked declined face in the grid")

    def test_the_source_always_resolves_to_media_never_to_original(self):
        self.assertEqual(pts.source_relpath(self.thumb_rel), f"{self.batch_dir}/MEDIA/p.png")

    def test_a_path_outside_the_thumbnail_shape_resolves_to_nothing(self):
        for path in ("photo_batches/b1/MEDIA/p.png",
                     "people/abc/profile.jpg",
                     "photo_batches/b1/THUMBNAILS/p.png",          # not a .jpg
                     "photo_batches/b1/THUMBNAILS/nested/p.png.jpg"):
            self.assertIsNone(pts.source_relpath(path), path)

    def test_there_is_no_thumbnail_without_a_rendered_copy(self):
        missing = pts.thumbnail_relpath(self.batch_dir, "gone.png")
        self.assertFalse(self.ensure(missing))
        self.assertFalse((self.storage / missing).exists())

    def test_it_is_generated_on_first_use_then_reused(self):
        self.assertTrue(self.ensure())
        path = self.storage / self.thumb_rel
        self.assertTrue(path.is_file())
        stamp = path.stat().st_mtime_ns
        self.assertTrue(self.ensure())
        self.assertEqual(path.stat().st_mtime_ns, stamp, "an existing thumbnail is not re-encoded")

    def test_one_central_thumbnail_per_photo_not_one_per_sorted_folder(self):
        self.assertEqual(self.thumb_rel, f"{self.batch_dir}/THUMBNAILS/p.png.jpg")
        self.ensure()
        found = list((self.storage / self.batch_dir).rglob("*.jpg"))
        self.assertEqual(len(found), 1, "a photo in N person folders still gets exactly one thumbnail")

    def test_thumbnails_live_outside_every_fan_out_destination(self):
        self.ensure()
        for folder in ("MEDIA", "SORTED", "AMBIENCE"):
            folder_path = self.storage / self.batch_dir / folder
            if folder_path.exists():
                self.assertEqual(list(folder_path.rglob("*.jpg")), [])

    def test_the_url_is_built_without_generating_anything(self):
        """The photo LIST endpoint must stay fast: building a page's URLs used
        to encode 50-100 thumbnails inline and took seconds."""
        url = pts.thumbnail_url(self.batch_dir, "p.png", self.storage, f"{self.batch_dir}/MEDIA/p.png")
        self.assertRegex(url, r"^photo_batches/b1/THUMBNAILS/p\.png\.jpg\?v=\d+-\d+$")
        self.assertFalse((self.storage / self.thumb_rel).exists(), "nothing was generated")

    def test_the_url_is_versioned_by_the_artifact_it_derives_from(self):
        import os

        media = self.storage / self.batch_dir / "MEDIA" / "p.png"
        first = pts.thumbnail_url(self.batch_dir, "p.png", self.storage, f"{self.batch_dir}/MEDIA/p.png")
        os.utime(media, (0, 0))
        second = pts.thumbnail_url(self.batch_dir, "p.png", self.storage, f"{self.batch_dir}/MEDIA/p.png")
        self.assertNotEqual(first, second,
                            "a re-rendered photo must not reuse a cached tile of the old render")

    def test_there_is_no_url_without_a_finalized_artifact(self):
        self.assertIsNone(pts.thumbnail_url(self.batch_dir, "p.png", self.storage, None))
        self.assertIsNone(pts.thumbnail_url(self.batch_dir, "x.png", self.storage,
                                            f"{self.batch_dir}/MEDIA/x.png"))


class CacheHeaderTests(unittest.TestCase):
    def test_thumbnails_are_cached_immutably(self):
        self.assertEqual(_cache_control("photo_batches/b1/THUMBNAILS/p.png.jpg"), _IMMUTABLE)

    def test_participant_photos_keep_their_revalidation(self):
        """Documented face-leak: participant ids get reused and the profile
        photo lives at a fixed url. This must never be weakened."""
        self.assertEqual(_cache_control("people/abc/profile.jpg"), _REVALIDATE)

    def test_full_resolution_batch_artifacts_still_revalidate(self):
        """They have no version stamp, so a re-render must be able to replace
        what the browser shows."""
        for path in ("photo_batches/b1/MEDIA/p.png",
                     "photo_batches/b1/ORIGINAL/p.png",
                     "photo_batches/b1/SORTED/0001_A/p.png"):
            self.assertEqual(_cache_control(path), _REVALIDATE, path)

    def test_a_thumbnails_lookalike_outside_photo_batches_is_not_trusted(self):
        self.assertEqual(_cache_control("people/THUMBNAILS/p.jpg"), _REVALIDATE)

    def test_backslash_separators_are_normalised(self):
        self.assertEqual(_cache_control(r"photo_batches\b1\THUMBNAILS\p.png.jpg"), _IMMUTABLE)


class WriteSiteTests(unittest.TestCase):
    """Both processing paths must produce thumbnails — the concurrent pipeline
    is the one that is actually enabled in production."""

    def test_the_sequential_path_writes_a_thumbnail(self):
        import inspect

        import app.services.photo_processing_service as pps

        self.assertIn("photo_thumbnail_service", inspect.getsource(pps._process_accepted_photo))

    def test_the_pipeline_path_writes_a_thumbnail_for_faced_and_ambience_photos(self):
        import inspect

        from app.services.event_pipeline_service import EventPhotoPipeline

        source = inspect.getsource(EventPhotoPipeline._render_worker)
        self.assertEqual(source.count("self._write_thumbnail("), 2,
                         "both the ambience branch and the faced branch")

    def test_the_photo_list_endpoint_does_not_generate_thumbnails(self):
        """Generating a page's worth inline cost ~6s for 50 photos. The URL is
        built cheaply and the file route fills it in per image instead."""
        import inspect

        from app.api.photo_batches import list_batch_photos

        source = inspect.getsource(list_batch_photos)
        self.assertIn("thumbnail_url", source)
        self.assertNotIn("ensure_thumbnail", source)

    def test_the_file_route_fills_a_missing_thumbnail_in(self):
        import inspect

        from app.api.files import get_file

        self.assertIn("ensure_thumbnail", inspect.getsource(get_file))


if __name__ == "__main__":
    unittest.main()
