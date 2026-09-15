"""Phase D1 — per-batch privacy mask style (Blur / custom image).

The decision of WHETHER to conceal a face is unchanged by this phase and is
covered by test_event_photo_explicit_deny.py. These tests cover only the
dispatch and, most importantly, that every failure mode degrades to the
STRONGER default (blur) and never to "no mask" — the faces this covers are
participants who explicitly declined consent.
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

import app.services.photo_processing_service as pps


def _solid(value: int = 200, size: int = 400) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.uint8)


class LoadMaskConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-mask-")
        self.addCleanup(self.temp.cleanup)
        self.batch_dir = Path(self.temp.name)
        (self.batch_dir / "MASK").mkdir()

    def write(self, config: str | None, png: bytes | None):
        if config is not None:
            (self.batch_dir / "MASK" / "config.json").write_text(config, encoding="utf-8")
        if png is not None:
            (self.batch_dir / "MASK" / "mask.png").write_bytes(png)

    def test_no_config_at_all_is_blur(self):
        self.assertEqual(pps._load_mask_config(self.batch_dir), ("blur", None))

    def test_explicit_blur_style(self):
        self.write('{"style": "blur"}', None)
        self.assertEqual(pps._load_mask_config(self.batch_dir), ("blur", None))

    def test_valid_image_style_loads_the_mask(self):
        png = cv2.imencode(".png", np.zeros((32, 32, 4), dtype=np.uint8))[1].tobytes()
        self.write('{"style": "image"}', png)
        style, mask = pps._load_mask_config(self.batch_dir)
        self.assertEqual(style, "image")
        self.assertIsNotNone(mask)

    def test_corrupt_json_falls_back_to_blur(self):
        self.write("{not json at all", None)
        self.assertEqual(pps._load_mask_config(self.batch_dir), ("blur", None))

    def test_image_style_with_missing_png_falls_back_to_blur(self):
        self.write('{"style": "image"}', None)
        self.assertEqual(pps._load_mask_config(self.batch_dir), ("blur", None))

    def test_image_style_with_corrupt_png_falls_back_to_blur(self):
        self.write('{"style": "image"}', b"this is not a png")
        self.assertEqual(pps._load_mask_config(self.batch_dir), ("blur", None))

    def test_unknown_style_falls_back_to_blur(self):
        self.write('{"style": "sunglasses"}', None)
        self.assertEqual(pps._load_mask_config(self.batch_dir), ("blur", None))


class ApplyPrivacyMaskTests(unittest.TestCase):
    """The face region must ALWAYS end up altered, whichever style ran."""

    bbox = (100.0, 100.0, 200.0, 200.0)

    def region(self, img):
        return img[60:240, 70:230].copy()

    def test_blur_style_alters_the_face_region(self):
        img = _solid()
        img[100:200, 100:200] = np.random.default_rng(0).integers(0, 255, (100, 100, 3), dtype=np.uint8)
        before = self.region(img)
        pps._apply_privacy_mask(img, self.bbox, ("blur", None))
        self.assertFalse(np.array_equal(before, self.region(img)))

    def test_image_style_covers_the_face_region(self):
        img = _solid()
        mask = np.zeros((16, 16, 4), dtype=np.uint8)
        mask[:, :, 3] = 255  # fully opaque black square
        pps._apply_privacy_mask(img, self.bbox, ("image", mask))
        covered = img[110:190, 110:190]
        self.assertTrue((covered == 0).all(), "opaque mask must fully cover the face")

    def test_unusable_mask_image_still_conceals_via_blur(self):
        """A mask that cannot be composited must never leave the face as-is."""
        img = _solid()
        img[100:200, 100:200] = np.random.default_rng(1).integers(0, 255, (100, 100, 3), dtype=np.uint8)
        before = self.region(img)
        broken = np.zeros((0, 0, 4), dtype=np.uint8)  # cv2.resize will refuse this
        pps._apply_privacy_mask(img, self.bbox, ("image", broken))
        self.assertFalse(np.array_equal(before, self.region(img)), "must have fallen back to blur")

    def test_none_config_conceals_via_blur(self):
        img = _solid()
        img[100:200, 100:200] = np.random.default_rng(2).integers(0, 255, (100, 100, 3), dtype=np.uint8)
        before = self.region(img)
        pps._apply_privacy_mask(img, self.bbox, None)
        self.assertFalse(np.array_equal(before, self.region(img)))

    def test_mask_covers_at_least_the_blurred_area(self):
        """Both styles must conceal the same padded region, or switching style
        would quietly reduce coverage."""
        blurred = _solid()
        blurred[100:200, 100:200] = 10
        pps._apply_privacy_mask(blurred, self.bbox, ("blur", None))

        masked = _solid()
        masked[100:200, 100:200] = 10
        mask = np.zeros((16, 16, 4), dtype=np.uint8)
        mask[:, :, 3] = 255
        pps._apply_privacy_mask(masked, self.bbox, ("image", mask))

        changed_blur = np.argwhere((blurred != _solid()).any(axis=2))
        changed_mask = np.argwhere((masked != _solid()).any(axis=2))
        self.assertGreater(len(changed_mask), 0)
        # the image mask's altered bounding box must not be smaller than blur's
        for axis in (0, 1):
            self.assertLessEqual(changed_mask[:, axis].min(), changed_blur[:, axis].min())
            self.assertGreaterEqual(changed_mask[:, axis].max(), changed_blur[:, axis].max())


if __name__ == "__main__":
    unittest.main()
