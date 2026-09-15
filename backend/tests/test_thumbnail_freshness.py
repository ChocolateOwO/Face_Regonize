"""Phase D1 — a stale thumbnail can neither be recreated on disk nor served
after a photo is re-rendered."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from app.services import photo_thumbnail_service as pts


def _png(seed: int) -> bytes:
    img = np.random.default_rng(seed).integers(0, 255, (300, 400, 3), dtype=np.uint8)
    return cv2.imencode(".png", img)[1].tobytes()


class FreshnessTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-thumbfresh-")
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name)
        self.rel = pts.thumbnail_relpath("photo_batches/b1", "p.png")
        self.media = self.storage / "photo_batches/b1/MEDIA/p.png"
        self.media.parent.mkdir(parents=True)
        self.media.write_bytes(_png(1))
        self.thumb = self.storage / self.rel

    def age(self, path: Path, seconds_ago: int):
        t = path.stat().st_mtime - seconds_ago
        os.utime(path, (t, t))


class StaleThumbnailTests(FreshnessTestCase):
    def test_a_thumbnail_older_than_its_media_is_regenerated(self):
        self.assertTrue(pts.ensure_thumbnail(self.storage, self.rel))
        self.age(self.thumb, 100)
        self.media.write_bytes(_png(2))  # MEDIA re-rendered after the thumbnail
        self.assertTrue(pts.ensure_thumbnail(self.storage, self.rel))
        self.assertEqual(self.thumb.read_bytes(), pts.encode_thumbnail(self.media.read_bytes()))

    def test_a_fresh_thumbnail_is_not_re_encoded(self):
        pts.ensure_thumbnail(self.storage, self.rel)
        stamp = self.thumb.stat().st_mtime_ns
        pts.ensure_thumbnail(self.storage, self.rel)
        self.assertEqual(self.thumb.stat().st_mtime_ns, stamp)

    def test_media_changing_mid_encode_discards_the_result(self):
        """The race that would recreate a stale tile: an on-demand generation
        reads the OLD MEDIA while a re-render replaces it."""
        real = pts.encode_thumbnail

        def racing_encode(data):
            out = real(data)
            self.media.write_bytes(_png(3))  # re-render lands during the encode
            return out

        with patch.object(pts, "encode_thumbnail", side_effect=racing_encode):
            pts.ensure_thumbnail(self.storage, self.rel)
        self.assertFalse(self.thumb.exists(), "a tile of the superseded MEDIA must never be written")


class AtomicWriteTests(FreshnessTestCase):
    def test_concurrent_writers_never_collide_on_a_temp_and_leave_none_behind(self):
        errors = []

        def writer(seed):
            try:
                for _ in range(5):
                    self.assertTrue(pts.write_thumbnail(self.thumb, _png(seed)))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(s,)) for s in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        leftovers = [p.name for p in self.thumb.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertIsNotNone(cv2.imdecode(np.frombuffer(self.thumb.read_bytes(), np.uint8), cv2.IMREAD_COLOR))


class VersionTests(FreshnessTestCase):
    def test_the_url_changes_whenever_media_is_rewritten_even_within_a_second(self):
        first = pts.thumbnail_url("photo_batches/b1", "p.png", self.storage, "photo_batches/b1/MEDIA/p.png")
        self.media.write_bytes(_png(4))
        second = pts.thumbnail_url("photo_batches/b1", "p.png", self.storage, "photo_batches/b1/MEDIA/p.png")
        self.assertNotEqual(first, second)
        self.assertRegex(second, r"\?v=\d+-\d+$")


if __name__ == "__main__":
    unittest.main()
