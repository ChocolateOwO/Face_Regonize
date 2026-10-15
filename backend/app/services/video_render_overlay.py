"""Drawing for the tagged-video render: boxes, name tags and the right-hand
"who was seen" panel. Pure image code (Pillow + NumPy/OpenCV), no model, no
database, no ffmpeg.

Thai text: this Pillow build has no libraqm, so its basic layout draws a tone
mark at the same height as the upper vowel under it and the tone mark
vanishes into the vowel (ที่, ชื่อ, นี้). Those tone marks are drawn lifted
instead; everything else uses normal layout.

Fonts are discovered, not assumed: RECONIZE_RENDER_FONT /
RECONIZE_RENDER_FONT_BOLD (a .ttf/.ttc path) first, then well-known Thai-capable
fonts on Windows, Linux and macOS. If none can draw Thai, labels fall back to
the participant ID in Latin text and the render says so - a name is never
drawn as empty boxes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

PANEL_W = 440
LINGER_SECONDS = 5.0
GREEN = (46, 204, 113)      # RGB - confirmed registered person
GREY = (150, 150, 150)      # RGB - unknown / not yet confirmed
PANEL_BG = (24, 26, 30)
TEXT = (235, 235, 235)
MUTED = (140, 145, 150)
RULE = (60, 64, 70)
UNKNOWN_LABEL = "Unknown"

THAI_UPPER = set("ัิีึื็ํ")   # ั ิ ี ึ ื ็ ํ
THAI_TONES = set("่้๊๋์")               # ่ ้ ๊ ๋ ์

_REGULAR = ("LeelawUI.ttf", "leelawad.ttf", "tahoma.ttf", "NotoSansThai-Regular.ttf", "NotoSansThaiUI-Regular.ttf",
            "Loma.ttf", "Garuda.ttf", "Thonburi.ttc", "Arial Unicode.ttf")
_BOLD = ("LeelaUIb.ttf", "leelawdb.ttf", "tahomabd.ttf", "NotoSansThai-Bold.ttf", "NotoSansThaiUI-Bold.ttf",
         "Loma-Bold.ttf", "Garuda-Bold.ttf", "Thonburi.ttc")


def _font_dirs() -> list[Path]:
    dirs = []
    for root in (os.environ.get("WINDIR"), os.environ.get("SystemRoot")):
        if root:
            dirs.append(Path(root) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft/Windows/Fonts")
    dirs += [Path(p) for p in ("/usr/share/fonts/truetype/noto", "/usr/share/fonts/truetype/tlwg", "/usr/share/fonts/opentype/noto",
                               "/usr/share/fonts/noto", "/usr/share/fonts", "/Library/Fonts", "/System/Library/Fonts/Supplemental",
                               str(Path.home() / ".fonts"))]
    seen, out = set(), []
    for d in dirs:
        if d not in seen and d.is_dir():
            seen.add(d)
            out.append(d)
    return out


def supports_thai(font: ImageFont.FreeTypeFont) -> bool:
    """A font without Thai draws the same "missing glyph" box for every Thai
    letter; a real one draws different shapes for different letters."""
    try:
        a, b, c = (bytes(font.getmask(ch)) for ch in ("ก", "ม", "อ"))  # ก ม อ
        return bool(a) and len({a, b, c}) == 3
    except Exception:  # noqa: BLE001 - an unusable font is simply not a candidate
        return False


def _load(path: Path, size: int) -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:  # noqa: BLE001
        return None


def find_font_file(bold: bool) -> Path | None:
    configured = os.environ.get("RECONIZE_RENDER_FONT_BOLD" if bold else "RECONIZE_RENDER_FONT", "").strip()
    candidates = [Path(configured)] if configured else []
    names = _BOLD + _REGULAR if bold else _REGULAR
    for directory in _font_dirs():
        candidates += [directory / name for name in names]
    for path in candidates:
        if path.is_file():
            font = _load(path, 20)
            if font is not None and supports_thai(font):
                return path
    return None


@dataclass
class Fonts:
    label: ImageFont.ImageFont
    title: ImageFont.ImageFont
    name: ImageFont.ImageFont
    small: ImageFont.ImageFont
    thai: bool
    description: str


def load_fonts() -> Fonts:
    regular, bold = find_font_file(False), find_font_file(True)
    regular = regular or bold
    bold = bold or regular
    if regular is None:
        base = ImageFont.load_default()
        sized = (lambda s: ImageFont.load_default(size=s)) if "size" in ImageFont.load_default.__code__.co_varnames else (lambda s: base)
        return Fonts(sized(20), sized(30), sized(24), sized(17), False,
                     "fallback: no Thai-capable font found; labels show participant IDs")
    return Fonts(_load(bold, 20), _load(bold, 30), _load(bold, 24), _load(regular, 17), True,
                 bold.name if bold == regular else f"{bold.name} / {regular.name}")


def draw_text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill) -> None:
    """Text with Thai tone marks above an upper vowel drawn one step higher."""
    x, y = xy
    lift = round(getattr(font, "size", 16) * 0.24)
    base, marks = [], []
    for i, ch in enumerate(text):
        if ch in THAI_TONES and i and text[i - 1] in THAI_UPPER:
            marks.append((font.getlength("".join(base)), ch))
        else:
            base.append(ch)
    draw.text((x, y), "".join(base), font=font, fill=fill)
    for dx, ch in marks:
        draw.text((x + dx, y - lift), ch, font=font, fill=fill)


def text_width(text: str, font) -> int:
    return int(font.getlength("".join(ch for i, ch in enumerate(text)
                                      if not (ch in THAI_TONES and i and text[i - 1] in THAI_UPPER))))


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def even(value: int) -> int:
    return value + (value & 1)


def output_size(width: int, height: int) -> tuple[int, int]:
    """Frame + panel, rounded up to even numbers (yuv420p needs both even)."""
    return even(width + PANEL_W), even(height)


class Painter:
    """Draws one output frame. Name tags and the panel are cached: a tag is
    rendered once per (text, colour) and the panel only when its content
    changes (about once a second)."""

    def __init__(self, width: int, height: int, names: dict[str, tuple[str, str]], fonts: Fonts | None = None):
        self.w, self.h = width, height
        self.out_w, self.out_h = output_size(width, height)
        self.names = names  # person_id -> (display name, participant_id)
        self.fonts = fonts or load_fonts()
        self._tags: dict[tuple[str, tuple], np.ndarray] = {}
        self._panel_key = None
        self._panel = None
        self.panel_renders = 0

    def label_for(self, person_id: str | None) -> str:
        if person_id is None:
            return UNKNOWN_LABEL
        name, participant_id = self.names.get(person_id, ("", ""))
        if self.fonts.thai:
            return name or participant_id or "?"
        return participant_id or "?"

    def tag(self, text: str, color: tuple) -> np.ndarray:
        key = (text, color)
        if key not in self._tags:
            image = Image.new("RGB", (text_width(text, self.fonts.label) + 8, 26), color)
            draw_text(ImageDraw.Draw(image), (4, 1), text, self.fonts.label, (0, 0, 0))
            self._tags[key] = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        return self._tags[key]

    def panel(self, rows: tuple, total_seen: int, now: float) -> np.ndarray:
        """rows: ((person_id, in_frame, seconds_since_seen), ...) oldest first."""
        key = (rows, total_seen, fmt_time(now))
        if key == self._panel_key:
            return self._panel
        f = self.fonts
        image = Image.new("RGB", (self.out_w - self.w, self.out_h), PANEL_BG)
        draw = ImageDraw.Draw(image)
        draw.line([0, 0, 0, self.out_h], fill=RULE, width=2)
        if f.thai:
            title, summary, more = "รายชื่อที่พบ", f"ตอนนี้ {len(rows)} คน · ทั้งหมด {total_seen} คน · {fmt_time(now)}", "+{} คน"
        else:
            title, summary, more = "People seen", f"now {len(rows)} | total {total_seen} | {fmt_time(now)}", "+{} more"
        draw_text(draw, (20, 16), title, f.title, TEXT)
        draw_text(draw, (20, 58), summary, f.small, MUTED)
        draw.line([20, 88, PANEL_W - 20, 88], fill=RULE, width=1)
        row_h, y = 52, 100
        max_rows = max(0, (self.out_h - y - 40) // row_h)
        for person_id, in_frame, ago in rows[:max_rows]:
            name, participant_id = self.names.get(person_id, ("", ""))
            draw.ellipse([20, y + 10, 32, y + 22], fill=GREEN if in_frame else MUTED)
            draw_text(draw, (44, y), (name if f.thai else participant_id) or participant_id or "?", f.name, TEXT if in_frame else MUTED)
            draw_text(draw, (44, y + 28), participant_id if in_frame else f"{participant_id}  ·  {ago}s ago", f.small, MUTED)
            y += row_h
        if len(rows) > max_rows:
            draw_text(draw, (20, y), more.format(len(rows) - max_rows), f.small, MUTED)
        self._panel_key, self._panel = key, cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        self.panel_renders += 1
        return self._panel

    def compose(self, frame: np.ndarray, boxes: list[tuple[tuple, str | None]], rows: tuple, total_seen: int, now: float) -> np.ndarray:
        """boxes: ((x1, y1, x2, y2), confirmed person_id or None) in frame pixels."""
        canvas = np.zeros((self.out_h, self.out_w, 3), dtype=np.uint8)
        canvas[: self.h, : self.w] = frame
        tags = []
        for bbox, person_id in boxes:
            x1, y1, x2, y2 = (int(round(v)) for v in bbox)
            color = GREEN if person_id else GREY
            cv2.rectangle(canvas, (x1, y1), (x2, y2), color[::-1], 2)
            tags.append((x1, y1, y2, self.label_for(person_id), color))
        for x1, y1, y2, text, color in tags:
            patch = self.tag(text, color)
            top = y1 - 28 if y1 >= 28 else y2 + 2
            _paste(canvas, patch, min(max(0, x1), max(0, self.w - patch.shape[1])), top, self.w, self.h)
        canvas[:, self.w:] = self.panel(rows, total_seen, now)[:, : self.out_w - self.w]
        return canvas


def _paste(canvas: np.ndarray, patch: np.ndarray, x: int, y: int, limit_w: int, limit_h: int) -> None:
    ph, pw = patch.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(limit_w, x + pw), min(limit_h, y + ph)
    if x1 > x0 and y1 > y0:
        canvas[y0:y1, x0:x1] = patch[y0 - y:y1 - y, x0 - x:x1 - x]
