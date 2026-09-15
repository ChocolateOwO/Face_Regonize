"""Phase D2 — privacy mask styles: built-in emoji, validated custom PNG, and
fail-closed compositing (the face is always blurred under the image).

test_privacy_mask_style.py pins the dispatch and the blur fallbacks; this file
pins what D2 adds. Every test uses temp dirs and in-memory images only.
"""
from pathlib import Path
import hashlib
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from sqlmodel import Session, SQLModel, create_engine

import app.services.photo_processing_service as pps
from app.services import mask_style_service as mss
from app.services.mask_style_service import MaskError


def _png(arr: np.ndarray, pnginfo=None) -> bytes:
    out = io.BytesIO()
    Image.fromarray(arr).save(out, format="PNG", pnginfo=pnginfo)
    return out.getvalue()


def _rgba(size: int, alpha: int) -> np.ndarray:
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    arr[:, :, :3] = 90
    arr[:, :, 3] = alpha
    return arr


def _disc(size: int = 64) -> np.ndarray:
    """Opaque disc with fully transparent corners (~78% coverage)."""
    arr = _rgba(size, 0)
    yy, xx = np.mgrid[:size, :size]
    inside = (xx - size / 2 + 0.5) ** 2 + (yy - size / 2 + 0.5) ** 2 <= (size / 2) ** 2
    arr[inside, 3] = 255
    return arr


def _face_image(seed: int = 0, size: int = 400) -> np.ndarray:
    img = np.full((size, size, 3), 200, dtype=np.uint8)
    img[100:200, 100:200] = np.random.default_rng(seed).integers(0, 255, (100, 100, 3), dtype=np.uint8)
    return img


BBOX = (100.0, 100.0, 200.0, 200.0)
# The padded region both styles conceal (_blur_region's arithmetic).
PAD = (slice(65, 235), slice(75, 225))


class BuiltinEmojiTests(unittest.TestCase):
    def test_every_builtin_passes_the_same_validation_as_an_upload(self):
        for emoji_id in mss.BUILTIN_EMOJI:
            with self.subTest(emoji_id):
                mss.validate_mask_png(mss.builtin_png(emoji_id))

    def test_builtins_are_deterministic_across_renders(self):
        first = mss.builtin_png("smile")
        mss.builtin_png.cache_clear()
        self.assertEqual(first, mss.builtin_png("smile"))

    def test_an_unknown_builtin_is_refused(self):
        with self.assertRaises(MaskError):
            mss.resolve("emoji:does-not-exist", None)

    def test_the_catalog_offers_every_builtin_as_a_png_data_url(self):
        catalog = mss.builtin_catalog()
        self.assertEqual([c["id"] for c in catalog], [f"emoji:{i}" for i in mss.BUILTIN_EMOJI])
        for c in catalog:
            self.assertTrue(c["data_url"].startswith("data:image/png;base64,"))

    def test_resolve_maps_each_choice(self):
        self.assertEqual(mss.resolve(None, None), ("blur", None, None))
        self.assertEqual(mss.resolve("blur", _png(_disc())), ("blur", None, None), "a stray PNG never changes blur")
        style, png, source = mss.resolve("emoji:heart", None)
        self.assertEqual((style, source), ("image", "emoji:heart"))
        self.assertEqual(png, mss.builtin_png("heart"))
        style, png, source = mss.resolve("image", _png(_disc()))
        self.assertEqual((style, source), ("image", "custom"))
        with self.assertRaises(MaskError):
            mss.resolve("image", None)
        with self.assertRaises(MaskError):
            mss.resolve("sunglasses", None)


class UploadValidationTests(unittest.TestCase):
    def refused(self, data):
        with self.assertRaises(MaskError):
            mss.validate_mask_png(data)

    def test_fully_transparent_is_refused(self):
        self.refused(_png(_rgba(64, 0)))

    def test_mostly_transparent_is_refused(self):
        arr = _rgba(100, 0)
        arr[:20, :50, 3] = 255  # 10% coverage
        self.refused(_png(arr))

    def test_partially_transparent_edges_are_accepted_and_normalised(self):
        out = mss.validate_mask_png(_png(_disc()))
        with Image.open(io.BytesIO(out)) as img:
            self.assertEqual((img.format, img.mode, img.size), ("PNG", "RGBA", (64, 64)))

    def test_an_opaque_png_without_alpha_is_accepted(self):
        mss.validate_mask_png(_png(np.full((32, 32, 3), 120, dtype=np.uint8)))

    def test_corrupt_png_is_refused(self):
        self.refused(mss.PNG_SIGNATURE + b"definitely not the rest of a png")

    def test_a_truncated_png_is_refused(self):
        data = _png(_disc(128))
        self.refused(data[: len(data) // 2])

    def test_format_is_checked_from_the_bytes_not_the_name(self):
        ok, jpeg = cv2.imencode(".jpg", np.full((64, 64, 3), 100, dtype=np.uint8))
        self.refused(jpeg.tobytes())
        gif = io.BytesIO()
        Image.new("P", (64, 64)).save(gif, format="GIF")
        self.refused(gif.getvalue())

    def test_zero_size_and_missing_uploads_are_refused(self):
        self.refused(b"")
        self.refused(None)

    def test_a_tiny_image_is_refused(self):
        self.refused(_png(_rgba(8, 255)))

    def test_too_many_pixels_is_refused_before_decoding(self):
        big = _png(np.zeros((2100, 2100), dtype=np.uint8))  # 4.41 MP, compresses to a few KB
        self.assertLess(len(big), mss.MAX_MASK_BYTES)
        self.refused(big)

    def test_an_over_long_side_is_refused(self):
        self.refused(_png(np.zeros((16, 4097), dtype=np.uint8)))

    def test_too_many_bytes_is_refused_without_opening(self):
        with patch.object(mss.Image, "open", side_effect=AssertionError("must not decode")):
            self.refused(mss.PNG_SIGNATURE + b"\0" * mss.MAX_MASK_BYTES)

    def test_ancillary_chunks_from_the_upload_are_not_stored(self):
        info = PngInfo()
        info.add_text("Comment", "uploader-supplied-metadata")
        out = mss.validate_mask_png(_png(_disc(), pnginfo=info))
        self.assertNotIn(b"uploader-supplied-metadata", out)


class CompositingTests(unittest.TestCase):
    def blurred_only(self, img):
        out = img.copy()
        pps._blur_region(out, BBOX)
        return out

    def test_blur_style_is_byte_identical_to_plain_blur(self):
        img = _face_image()
        for config in (None, ("blur", None)):
            with self.subTest(config=config):
                a = img.copy()
                pps._apply_privacy_mask(a, BBOX, config)
                self.assertTrue(np.array_equal(a, self.blurred_only(img)))

    def test_transparent_mask_pixels_reveal_only_blur(self):
        img = _face_image(1)
        ring = _disc(64)
        ring[24:40, 24:40, 3] = 0  # a transparent hole over the middle of the face
        mask = cv2.imdecode(np.frombuffer(_png(ring), np.uint8), cv2.IMREAD_UNCHANGED)

        out = img.copy()
        pps._apply_privacy_mask(out, BBOX, ("image", mask))
        blur = self.blurred_only(img)

        resized_alpha = cv2.resize(mask, (150, 170), interpolation=cv2.INTER_AREA)[:, :, 3]
        clear = resized_alpha == 0
        self.assertTrue(clear.any())
        self.assertTrue(np.array_equal(out[PAD][clear], blur[PAD][clear]),
                        "where the mask is transparent the pixel must be the blurred one")
        face = (slice(100, 200), slice(100, 200))
        self.assertLess(float((out[face] == img[face]).all(axis=2).mean()), 0.05,
                        "the original face pixels must not survive")

    def test_padded_coverage_equals_blur_coverage(self):
        img = _face_image(2)
        mask = cv2.imdecode(np.frombuffer(mss.builtin_png("smile"), np.uint8), cv2.IMREAD_UNCHANGED)
        out = img.copy()
        pps._apply_privacy_mask(out, BBOX, ("image", mask))
        changed = np.argwhere((out != img).any(axis=2))
        blur_changed = np.argwhere((self.blurred_only(img) != img).any(axis=2))
        for axis in (0, 1):
            self.assertLessEqual(changed[:, axis].min(), blur_changed[:, axis].min())
            self.assertGreaterEqual(changed[:, axis].max(), blur_changed[:, axis].max())

    def test_a_compositing_error_leaves_the_blur_in_place(self):
        img = _face_image(3)
        out = img.copy()
        with patch.object(pps, "_apply_mask_image", side_effect=RuntimeError("boom")):
            pps._apply_privacy_mask(out, BBOX, ("image", np.zeros((16, 16, 4), np.uint8)))
        self.assertTrue(np.array_equal(out, self.blurred_only(img)))

    def test_resolution_is_preserved(self):
        img = np.full((1234, 987, 3), 150, dtype=np.uint8)
        mask = cv2.imdecode(np.frombuffer(mss.builtin_png("cool"), np.uint8), cv2.IMREAD_UNCHANGED)
        pps._apply_privacy_mask(img, (900.0, 1100.0, 987.0, 1234.0), ("image", mask))  # box at the image edge
        self.assertEqual(img.shape, (1234, 987, 3))


class RenderTests(unittest.TestCase):
    """End to end through the regeneration renderer: ORIGINAL is read, never
    written, and the output keeps full resolution."""

    def test_render_with_an_emoji_keeps_original_untouched_and_full_size(self):
        from app.models.models import PhotoBatchFace, PhotoBatchPhoto
        from app.services.photo_regeneration_service import render_photo_bytes

        with tempfile.TemporaryDirectory(prefix="reconize-d2-render-") as tmp:
            original = Path(tmp) / "ORIGINAL" / "p.png"
            original.parent.mkdir()
            original.write_bytes(cv2.imencode(".png", _face_image(4, size=640))[1].tobytes())
            before = hashlib.sha256(original.read_bytes()).hexdigest()

            photo = PhotoBatchPhoto(filename="p.png")
            face = PhotoBatchFace(bbox="100,100,200,200", person_id="p1", consent_status_at_processing="declined")
            mask = cv2.imdecode(np.frombuffer(mss.builtin_png("wink"), np.uint8), cv2.IMREAD_UNCHANGED)
            out = render_photo_bytes(photo, [face], original.read_bytes(), None, ("image", mask))

            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), before)
            decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape, (640, 640, 3))
            src = cv2.imdecode(np.frombuffer(original.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            self.assertFalse(np.array_equal(decoded[100:200, 100:200], src[100:200, 100:200]))


class CreateBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-d2-create-")
        self.addCleanup(self.temp.cleanup)
        self.photo_batches_dir = Path(self.temp.name) / "storage" / "photo_batches"
        self.engine = create_engine(f"sqlite:///{(Path(self.temp.name) / 't.db').as_posix()}",
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        for target, value in (("engine", self.engine), ("PHOTO_BATCHES_DIR", self.photo_batches_dir)):
            p = patch.object(pps, target, value)
            p.start()
            self.addCleanup(p.stop)

    def create(self, choice, png=None):
        style, mask_png, source = mss.resolve(choice, png)
        with Session(self.engine) as session:
            return pps.create_local_batch(session, "d2", 3, "user-1",
                                          mask_style=style, mask_png=mask_png, mask_source=source)

    def test_the_default_writes_no_mask_config(self):
        batch = self.create("blur")
        self.assertFalse((self.photo_batches_dir / batch.id / "MASK").exists())
        self.assertEqual(pps._load_mask_config(self.photo_batches_dir / batch.id), ("blur", None))

    def test_a_builtin_emoji_is_stored_batch_locally_and_loads(self):
        batch = self.create("emoji:smile")
        mask_dir = self.photo_batches_dir / batch.id / "MASK"
        self.assertEqual(json.loads((mask_dir / "config.json").read_text(encoding="utf-8")),
                         {"style": "image", "source": "emoji:smile"})
        self.assertEqual((mask_dir / "mask.png").read_bytes(), mss.builtin_png("smile"))
        style, mask = pps._load_mask_config(self.photo_batches_dir / batch.id)
        self.assertEqual(style, "image")
        self.assertEqual(mask.shape, (mss.EMOJI_SIZE, mss.EMOJI_SIZE, 4))

    def test_a_custom_png_is_stored_normalised(self):
        batch = self.create("image", _png(_disc()))
        stored = (self.photo_batches_dir / batch.id / "MASK" / "mask.png").read_bytes()
        self.assertEqual(stored, mss.validate_mask_png(_png(_disc())))


class ApiWiringTests(unittest.TestCase):
    def test_both_create_routes_validate_the_mask_before_creating_a_batch(self):
        import inspect

        from app.api import photo_batches as api

        for fn, create in ((api.create_photo_batch, "create_batch("), (api.create_local_photo_batch, "create_local_batch(")):
            source = inspect.getsource(fn)
            with self.subTest(fn.__name__):
                self.assertIn("_resolve_mask(", source)
                self.assertLess(source.index("_resolve_mask("), source.index(create))
                self.assertIn("mask_png=mask_png", source)

    def test_mask_styles_is_registered_before_the_batch_id_route(self):
        from app.api.photo_batches import router

        get_paths = [r.path for r in router.routes if "GET" in getattr(r, "methods", set())]
        self.assertLess(get_paths.index("/api/photo-batches/mask-styles"),
                        get_paths.index("/api/photo-batches/{batch_id}"))


if __name__ == "__main__":
    unittest.main()
