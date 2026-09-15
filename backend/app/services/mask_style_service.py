"""Phase D2 — privacy mask styles for declined participants.

A batch conceals declined faces with Gaussian blur (the default), a built-in
emoji, or an admin-supplied PNG. Whatever the style, rendering ALWAYS blurs the
padded face region first and only then composites the image on top (see
photo_processing_service._apply_privacy_mask), so a transparent or partially
transparent pixel can only ever reveal blur, never the face.

This module owns the two things that happen before a batch exists:
  * the built-in emoji, drawn with PIL shapes (no emoji-font dependency, so
    every machine renders exactly the same pixels);
  * validation of an uploaded PNG — the decoded format must really be PNG,
    size and pixel counts are bounded (decompression bombs), and an image that
    is mostly transparent is refused because it would conceal nothing beyond
    the blur underneath it.
Accepted PNGs are re-encoded, so what is stored is a plain RGBA PNG with no
ancillary chunks from the upload.
"""
from __future__ import annotations

import base64
from functools import lru_cache
import io
import warnings

import numpy as np
from PIL import Image, ImageDraw

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_MASK_BYTES = 2 * 1024 * 1024
MIN_MASK_SIDE = 16
MAX_MASK_SIDE = 4096
MAX_MASK_PIXELS = 4 * 1024 * 1024
# Share of pixels that must be at least half opaque. Below this the image hides
# little more than the blur it sits on, which is almost certainly a mistake
# (e.g. an exported logo on a transparent canvas).
MIN_OPAQUE_COVERAGE = 0.30

EMOJI_SIZE = 256
BUILTIN_EMOJI: dict[str, str] = {
    "smile": "Smiling face",
    "cool": "Sunglasses face",
    "wink": "Winking face",
    "heart": "Heart",
}

_YELLOW = (255, 204, 77, 255)
_OUTLINE = (201, 140, 22, 255)
_DARK = (60, 40, 20, 255)
_BLACK = (25, 25, 25, 255)
_RED = (231, 76, 90, 255)


class MaskError(ValueError):
    """An unusable mask choice or upload. The message is shown to the admin."""


def _face(draw: ImageDraw.ImageDraw, s: int) -> None:
    draw.ellipse((s * 0.02, s * 0.02, s * 0.98, s * 0.98), fill=_YELLOW, outline=_OUTLINE, width=int(s * 0.03))


def _smile(draw, s):
    draw.arc((s * 0.25, s * 0.30, s * 0.75, s * 0.78), start=20, end=160, fill=_DARK, width=int(s * 0.05))


def _draw(emoji_id: str, s: int) -> Image.Image:
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if emoji_id == "heart":
        r = s * 0.25
        draw.ellipse((s * 0.04, s * 0.10, s * 0.04 + 2 * r, s * 0.10 + 2 * r), fill=_RED)
        draw.ellipse((s * 0.96 - 2 * r, s * 0.10, s * 0.96, s * 0.10 + 2 * r), fill=_RED)
        draw.polygon([(s * 0.05, s * 0.40), (s * 0.95, s * 0.40), (s * 0.50, s * 0.95)], fill=_RED)
        return img
    _face(draw, s)
    if emoji_id == "cool":
        draw.rounded_rectangle((s * 0.16, s * 0.30, s * 0.47, s * 0.50), radius=int(s * 0.06), fill=_BLACK)
        draw.rounded_rectangle((s * 0.53, s * 0.30, s * 0.84, s * 0.50), radius=int(s * 0.06), fill=_BLACK)
        draw.rectangle((s * 0.45, s * 0.34, s * 0.55, s * 0.38), fill=_BLACK)
    elif emoji_id == "wink":
        draw.ellipse((s * 0.30, s * 0.28, s * 0.40, s * 0.44), fill=_DARK)
        draw.arc((s * 0.56, s * 0.30, s * 0.72, s * 0.44), start=200, end=340, fill=_DARK, width=int(s * 0.04))
    else:  # smile
        draw.ellipse((s * 0.30, s * 0.28, s * 0.40, s * 0.44), fill=_DARK)
        draw.ellipse((s * 0.60, s * 0.28, s * 0.70, s * 0.44), fill=_DARK)
    _smile(draw, s)
    return img


@lru_cache(maxsize=None)
def builtin_png(emoji_id: str) -> bytes:
    """Deterministic PNG bytes for one built-in emoji (drawn at 2x, then
    downsampled for smooth edges)."""
    if emoji_id not in BUILTIN_EMOJI:
        raise MaskError("Unknown built-in mask.")
    img = _draw(emoji_id, EMOJI_SIZE * 2).resize((EMOJI_SIZE, EMOJI_SIZE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def builtin_catalog() -> list[dict]:
    return [
        {"id": f"emoji:{emoji_id}", "label": label,
         "data_url": "data:image/png;base64," + base64.b64encode(builtin_png(emoji_id)).decode("ascii")}
        for emoji_id, label in BUILTIN_EMOJI.items()
    ]


def validate_mask_png(data: bytes | None) -> bytes:
    """Return a normalised RGBA PNG, or raise MaskError explaining why the
    upload cannot be used. Never decodes pixels before the size checks."""
    if not data:
        raise MaskError("The mask image is empty.")
    if len(data) > MAX_MASK_BYTES:
        raise MaskError(f"The mask image must be at most {MAX_MASK_BYTES // (1024 * 1024)} MB.")
    if not data.startswith(PNG_SIGNATURE):
        raise MaskError("The mask must be a PNG image.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format != "PNG":
                    raise MaskError("The mask must be a PNG image.")
                width, height = probe.size  # header only — no pixel decode yet
                if min(width, height) < MIN_MASK_SIDE:
                    raise MaskError(f"The mask image must be at least {MIN_MASK_SIDE}×{MIN_MASK_SIDE} pixels.")
                if max(width, height) > MAX_MASK_SIDE or width * height > MAX_MASK_PIXELS:
                    raise MaskError("The mask image is too large. Use a PNG of at most 2048×2048 pixels.")
                probe.verify()
            with Image.open(io.BytesIO(data)) as img:
                rgba = img.convert("RGBA")
    except MaskError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise MaskError("The mask image is too large.") from e
    except Exception as e:  # noqa: BLE001 — any decoder failure means "unusable"
        raise MaskError("The mask image could not be read. It may be corrupt.") from e

    alpha = np.asarray(rgba)[:, :, 3]
    if float((alpha >= 128).mean()) < MIN_OPAQUE_COVERAGE:
        raise MaskError(
            "The mask image is mostly transparent, so it would hide almost nothing. "
            "Use an image that is at least 30% opaque."
        )
    out = io.BytesIO()
    rgba.save(out, format="PNG")
    return out.getvalue()


def resolve(style: str | None, png: bytes | None) -> tuple[str, bytes | None, str | None]:
    """Map the admin's choice to (stored style, PNG bytes, source label).

    "blur" → ("blur", None, None); "emoji:<id>" → ("image", built-in PNG,
    "emoji:<id>"); "image" → ("image", validated upload, "custom").
    Raises MaskError for anything else, before any batch is created."""
    choice = (style or "blur").strip().lower()
    if choice == "blur":
        return "blur", None, None
    if choice.startswith("emoji:"):
        emoji_id = choice.split(":", 1)[1]
        return "image", builtin_png(emoji_id), f"emoji:{emoji_id}"
    if choice == "image":
        if not png:
            raise MaskError("Choose a PNG image for the custom mask.")
        return "image", validate_mask_png(png), "custom"
    raise MaskError("Unknown privacy mask style.")
