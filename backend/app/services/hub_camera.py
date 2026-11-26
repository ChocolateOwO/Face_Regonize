"""Hub cameras for the CCTV station: the Hub's camera list and its MJPEG
stream, relayed to the browser on Reconize's own origin.

Why a relay: on a PC where the Mice Hub already holds a camera, Windows lets
only one program open it, so the station must not open it again. The Hub
shares every camera at GET /api/dev/cam.stream?dev=<dev> (multipart/x-mixed-
replace, boundary "mice", one image/jpeg part per frame, or 503 + Retry-After
while the camera is busy). A page on Reconize's origin cannot read another
origin's pixels, so the backend reads that stream from the trusted Hub
(services/hub_link.py) and passes validated JPEG frames through unchanged.

The relay never decodes or runs inference: the station page turns the frames
into its camera's MediaStream and scans it under the existing Detection
Session rules, exactly like a local camera.

Bounds: connect 3 s, read 10 s (a Hub that goes quiet ends the stream and the
page reconnects with backoff), at most MAX_FRAME_BYTES per frame and
MAX_BUFFER_BYTES buffered; anything else is dropped as an invalid frame.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

import requests

from app.services import hub_link

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 10.0
LIST_TIMEOUT = 3.0
CHUNK = 64 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_BUFFER_BYTES = 2 * MAX_FRAME_BYTES
MAX_LIST_BYTES = 256 * 1024
# The Hub's camera ids: "pc:<webcam name>", "cam:<id>", "wifi:<address>".
DEV_RE = re.compile(r"^(pc|cam|wifi):[^\x00-\x1f\x7f]{1,120}$")

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


class HubCameraError(Exception):
    def __init__(self, message: str, status: int = 502, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def valid_dev(dev: str | None) -> bool:
    return bool(dev) and DEV_RE.match(dev) is not None


def _base() -> str:
    base = hub_link.base_url()
    if not base:
        raise HubCameraError("The Mice Hub is not configured on this PC (MICE_HUB_URL).", status=404)
    return base


def list_cameras(get=requests.get) -> list[dict]:
    """The Hub's cameras as [{dev, name, kind, live}] - nothing else is passed on."""
    try:
        resp = get(_base() + "/api/cameras", timeout=LIST_TIMEOUT, allow_redirects=False, stream=True)
    except requests.RequestException as exc:
        raise HubCameraError(f"The Mice Hub did not answer ({type(exc).__name__}).", status=503) from exc
    try:
        if resp.status_code != 200:
            raise HubCameraError(f"The Mice Hub answered HTTP {resp.status_code} for its camera list.", status=502)
        body = resp.raw.read(MAX_LIST_BYTES + 1, decode_content=True)
        if len(body) > MAX_LIST_BYTES:
            raise HubCameraError("The Mice Hub's camera list is too large.", status=502)
        import json

        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HubCameraError("The Mice Hub's camera list could not be read.", status=502) from exc
    finally:
        resp.close()
    rows = data.get("cameras", data.get("items")) if isinstance(data, dict) else data
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        dev = str(row.get("dev") or "")
        if not valid_dev(dev):
            continue
        out.append({"dev": dev, "name": str(row.get("name") or dev)[:120], "kind": str(row.get("kind") or "")[:40],
                    "live": bool(row.get("live", True))})
    return out


class MjpegParser:
    """Bytes in, complete JPEG frames out. Uses each part's Content-Length when
    present and the JPEG start/end markers otherwise, so it accepts the Hub's
    framing and plain multipart MJPEG alike. A part that is not a JPEG, or is
    larger than MAX_FRAME_BYTES, is dropped and counted in `invalid`."""

    def __init__(self, max_frame: int = MAX_FRAME_BYTES, max_buffer: int = MAX_BUFFER_BYTES):
        self.buf = bytearray()
        self.max_frame, self.max_buffer = max_frame, max_buffer
        self.invalid = 0
        self.frames = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf += chunk
        out: list[bytes] = []
        while True:
            frame = self._next()
            if frame is None:
                break
            out.append(frame)
        if len(self.buf) > self.max_buffer:  # garbage without a frame end: start over
            self.invalid += 1
            del self.buf[:]
        return out

    def _next(self) -> bytes | None:
        buf = self.buf
        start = buf.find(SOI)
        header_end = buf.find(b"\r\n\r\n")
        # A part header announcing its length, before the JPEG starts.
        if header_end != -1 and (start == -1 or header_end < start):
            header = bytes(buf[:header_end]).lower()
            m = re.search(rb"content-length:\s*(\d+)", header)
            if m:
                length = int(m.group(1))
                body_at = header_end + 4
                if length > self.max_frame:
                    self.invalid += 1
                    del buf[:body_at]
                    return self._next()
                if len(buf) < body_at + length:
                    return None
                frame = bytes(buf[body_at:body_at + length])
                del buf[:body_at + length]
                if frame.startswith(SOI) and frame.rstrip(b"\r\n").endswith(EOI):
                    self.frames += 1
                    return frame
                self.invalid += 1
                return self._next()
            if start == -1:
                del buf[:header_end + 4]
                return self._next()
        if start == -1:
            # Keep only a possible half marker at the end.
            if len(buf) > 1:
                del buf[:-1]
            return None
        end = buf.find(EOI, start + 2)
        if end == -1:
            if len(buf) - start > self.max_frame:
                self.invalid += 1
                del buf[:start + 2]
            return None
        frame = bytes(buf[start:end + 2])
        del buf[:end + 2]
        if len(frame) > self.max_frame:
            self.invalid += 1
            return self._next()
        self.frames += 1
        return frame


def open_stream(dev: str, get=requests.get):
    """Connect to the Hub's stream for `dev`. Returns the open response, or
    raises HubCameraError (404 no Hub, 400 bad camera id, 503 busy/unreachable,
    502 anything else)."""
    if not valid_dev(dev):
        raise HubCameraError("Not a Hub camera id.", status=400)
    try:
        resp = get(_base() + "/api/dev/cam.stream", params={"dev": dev}, stream=True,
                   timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=False)
    except requests.RequestException as exc:
        raise HubCameraError(f"The Mice Hub did not answer ({type(exc).__name__}).", status=503, retry_after=2) from exc
    if resp.status_code != 200:
        retry = resp.headers.get("Retry-After")
        resp.close()
        status = 503 if resp.status_code in (408, 429, 503) else 502
        raise HubCameraError(f"The Mice Hub could not stream this camera (HTTP {resp.status_code}).", status=status,
                             retry_after=int(retry) if retry and retry.isdigit() else None)
    ctype = resp.headers.get("Content-Type", "").lower()
    if not ctype.startswith("multipart/x-mixed-replace"):
        resp.close()
        raise HubCameraError("The Mice Hub did not answer with an MJPEG stream.", status=502)
    return resp


def frames(resp, parser: MjpegParser | None = None) -> Iterator[bytes]:
    """Valid JPEG frames from an open stream until it ends or stalls."""
    import urllib3

    parser = parser or MjpegParser()
    try:
        while True:
            # read1: whatever has arrived (at most CHUNK), so each frame is
            # passed on as soon as it is complete, not when 64 KB piled up.
            chunk = resp.raw.read1(CHUNK)
            if not chunk:
                return  # the Hub ended the stream
            yield from parser.feed(chunk)
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError):
        return  # dropped or stalled: the caller (the page) reconnects
    finally:
        resp.close()
