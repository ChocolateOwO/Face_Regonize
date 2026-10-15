"""FFmpeg plumbing for the tagged-video render: discovery, probing, encoder and
audio preflight, decoding with real timestamps, and an encoder pipe that keeps
each frame's own timestamp.

Timing. A raw-frame pipe carries no timestamps, so frames are wrapped in a
minimal Matroska stream (uncompressed I420, one cluster per frame, microsecond
timestamps) and ffmpeg encodes with -fps_mode passthrough. Every output frame
therefore has the timestamp of the source frame it was drawn from: variable
frame rate stays variable, nothing is duplicated or dropped, and the source's
audio - copied or transcoded from the original file on the same clock - stays
in sync.

Modes. Decoder and encoder are chosen independently: auto, gpu or cpu.
  gpu   NVDEC decode / h264_nvenc encode. Proved with a real decode of this
        file / a real encode at the ACTUAL output size before the render
        starts (NVENC H.264 refuses widths above 4096, e.g. a 3840 px source
        plus the panel). If the GPU cannot do it the render stops with that
        reason - an explicit GPU choice never falls back to the CPU.
  cpu   OpenCV FFmpeg decode / libx264 encode.
  auto  the faster path measured on this project's hardware (see
        docs/tagged-video.md): CPU decode, and the encoders in ENCODER_ORDER,
        falling back to the next one when the first cannot encode this size.

Nothing here opens the application database or a model.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# The two encoders are set to the SAME picture quality, so choosing between
# them is a question of time and bytes only. Measured against a lossless
# reference of a tagged 1080p clip (docs/tagged-video.md): libx264 veryfast
# crf 20 = VMAF 96.2 / SSIM 0.9857; h264_nvenc p4 cq 29 = VMAF 97.2 / SSIM
# 0.9857 in 23% fewer bytes. (cq 23, the earlier value, was a higher quality
# than libx264's and cost almost three times the bytes.)
ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "29", "-b:v", "0"],
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
}
ENCODER_LABELS = {"h264_nvenc": "h264_nvenc (NVIDIA GPU)", "libx264": "libx264 (CPU)"}
MODES = ("auto", "gpu", "cpu")
ENCODER_FOR_MODE = {"gpu": "h264_nvenc", "cpu": "libx264"}
# What Auto tries, in order. Decided by measurement, not by habit: at equal
# quality h264_nvenc writes the smaller file (-23%) with less CPU work, and
# is 18-23% faster end to end whenever the CPU is busy - another video worker
# decoding, or a slowed CPU. Only when the render is the one thing the
# computer is doing is libx264 faster (16%). libx264 remains the fallback
# (e.g. an output wider than 4096 px) and the explicit CPU choice.
ENCODER_ORDER = ("h264_nvenc", "libx264")
MP4_COPY_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}


class MediaToolError(RuntimeError):
    pass


class UnsupportedMode(MediaToolError):
    """An explicitly chosen GPU/CPU path cannot be used; never silently swapped."""


# --- discovery -------------------------------------------------------------

def find_tool(name: str) -> str | None:
    """ffmpeg/ffprobe from RECONIZE_FFMPEG (the executable or its folder), then
    PATH, then the WinGet install location. None when not installed."""
    exe = name + (".exe" if os.name == "nt" else "")
    configured = os.environ.get("RECONIZE_FFMPEG", "").strip().strip('"')
    if configured:
        base = Path(configured)
        candidate = base / exe if base.is_dir() else base.with_name(exe)
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    winget = Path.home() / "AppData/Local/Microsoft/WinGet"
    for pattern in (f"Links/{exe}", f"Packages/Gyan.FFmpeg*/*/bin/{exe}"):
        hit = next(iter(winget.glob(pattern)), None) if winget.is_dir() else None
        if hit:
            return str(hit)
    return None


def _run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def _reason(stderr: bytes, fallback: str) -> str:
    """ffmpeg's FIRST complaint, without its "[component @ address]" prefix.
    The first line names the cause; the last is usually a generic summary."""
    for line in stderr.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            return (line.split("] ", 1)[1] if line.startswith("[") and "] " in line else line)[:200]
    return fallback


# --- probing ---------------------------------------------------------------

@dataclass
class SourceInfo:
    width: int = 0
    height: int = 0
    video_codec: str = ""
    video_start: float = 0.0
    nominal_fps: float = 0.0
    average_fps: float = 0.0
    rotation: int = 0
    audio_codec: str | None = None
    audio_start: float | None = None
    duration: float | None = None


def _rate(text: str | None) -> float:
    try:
        num, den = (text or "0/1").split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def probe(path: Path) -> SourceInfo:
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        raise MediaToolError("ffprobe was not found. Install FFmpeg (ffmpeg and ffprobe) or set RECONIZE_FFMPEG.")
    result = _run([ffprobe, "-v", "error", "-show_entries",
                   "stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,start_time,duration:"
                   "stream_side_data=rotation:stream_tags=rotate:format=duration", "-of", "json", str(path)])
    if result.returncode != 0:
        raise MediaToolError("ffprobe could not read the source video.")
    data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    info = SourceInfo(duration=_float((data.get("format") or {}).get("duration")))
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info.video_codec:
            info.width, info.height = int(stream.get("width") or 0), int(stream.get("height") or 0)
            info.video_codec = stream.get("codec_name") or ""
            info.video_start = _float(stream.get("start_time"), 0.0) or 0.0
            info.nominal_fps, info.average_fps = _rate(stream.get("r_frame_rate")), _rate(stream.get("avg_frame_rate"))
            rotation = (stream.get("tags") or {}).get("rotate")
            for side in stream.get("side_data_list") or []:
                rotation = side.get("rotation", rotation)
            info.rotation = int(_float(rotation, 0) or 0) % 360
        elif stream.get("codec_type") == "audio" and info.audio_codec is None:
            info.audio_codec = stream.get("codec_name") or "unknown"
            info.audio_start = _float(stream.get("start_time"), 0.0)
    if not info.video_codec:
        raise MediaToolError("The source has no video stream.")
    return info


def frame_timestamps(path: Path) -> list[float]:
    """Presentation timestamps of every video frame (packet scan, no decode)."""
    ffprobe = find_tool("ffprobe")
    result = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                   "-of", "csv=p=0", str(path)], timeout=600)
    if result.returncode != 0:
        raise MediaToolError("ffprobe could not list the source frame timestamps.")
    values = []
    for line in result.stdout.decode("ascii", "replace").split():
        value = _float(line.strip(","))
        if value is not None:
            values.append(value)
    return sorted(values)


def output_summary(path: Path) -> dict:
    """What was actually written: frame count, duration, streams."""
    ffprobe = find_tool("ffprobe")
    result = _run([ffprobe, "-v", "error", "-count_packets", "-show_entries",
                   "stream=codec_type,codec_name,width,height,nb_read_packets,start_time,duration:format=duration",
                   "-of", "json", str(path)], timeout=600)
    if result.returncode != 0:
        raise MediaToolError("The rendered file could not be read back.")
    data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    out = {"duration": _float((data.get("format") or {}).get("duration")), "video_frames": None, "audio_codec": None}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            out.update(video_frames=int(stream.get("nb_read_packets") or 0), video_codec=stream.get("codec_name"),
                       width=stream.get("width"), height=stream.get("height"), video_start=_float(stream.get("start_time"), 0.0))
        elif stream.get("codec_type") == "audio":
            out.update(audio_codec=stream.get("codec_name"), audio_start=_float(stream.get("start_time"), 0.0))
    return out


# --- encoder and audio preflight --------------------------------------------

_encoder_cache: dict[tuple[str, int, int], tuple[bool, str]] = {}
_encoder_lock = threading.Lock()


def encoder_works(encoder: str, width: int, height: int) -> tuple[bool, str]:
    """A short REAL encode at the real output size. (ok, reason-if-not)."""
    key = (encoder, width, height)
    with _encoder_lock:
        if key in _encoder_cache:
            return _encoder_cache[key]
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg not found"
    try:
        result = _run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                       f"color=c=black:s={width}x{height}:r=30:d=0.4", *ENCODERS[encoder], "-pix_fmt", "yuv420p",
                       "-frames:v", "6", "-f", "null", "-"], timeout=60)
        ok = result.returncode == 0
        reason = "" if ok else _reason(result.stderr, "encoder failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, reason = False, type(exc).__name__
    with _encoder_lock:
        _encoder_cache[key] = (ok, reason)
    return ok, reason


def choose_encoder(width: int, height: int, mode: str = "auto") -> tuple[str, str]:
    """(encoder actually usable at this size, note explaining any fallback).
    An explicit mode uses exactly its encoder or raises UnsupportedMode."""
    if mode in ENCODER_FOR_MODE:
        encoder = ENCODER_FOR_MODE[mode]
        ok, reason = encoder_works(encoder, width, height)
        if ok:
            return encoder, ""
        other = "CPU" if mode == "gpu" else "GPU"
        hint = " (the GPU's H.264 encoder takes at most 4096 px width)" if encoder == "h264_nvenc" and width > 4096 else ""
        raise UnsupportedMode(f"{mode.upper()} encoding ({encoder}) cannot write {width}x{height} on this computer: {reason}{hint}. "
                              f"Choose Auto or {other} encoding and render again.")
    notes = []
    for encoder in ENCODER_ORDER:
        ok, reason = encoder_works(encoder, width, height)
        if ok:
            return encoder, "; ".join(notes)
        notes.append(f"{encoder} unavailable at {width}x{height}: {reason}")
    raise MediaToolError("No working H.264 encoder (tried h264_nvenc and libx264): " + "; ".join(notes))


def audio_plan(source: Path, info: SourceInfo, keep_audio: bool) -> tuple[str, list[str]]:
    """How the source's audio goes into the MP4, proved on one second of it:
    ('not_requested'|'none'|'copied'|'transcoded'|'incompatible', ffmpeg args)."""
    if not keep_audio:
        return "not_requested", []
    if info.audio_codec is None:
        return "none", []
    ffmpeg = find_tool("ffmpeg")
    plans = ([("copied", ["-c:a", "copy"])] if info.audio_codec in MP4_COPY_AUDIO else []) + [
        ("transcoded", ["-c:a", "aac", "-b:a", "160k"])]
    for name, args in plans:
        try:
            result = _run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-t", "1", "-i", str(source), "-map", "0:a:0",
                           "-vn", *args, "-f", "mp4", "-movflags", "frag_keyframe+empty_moov", "-y", os.devnull], timeout=60)
            if result.returncode == 0:
                return name, args
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "incompatible", []


# --- decoding with real timestamps ------------------------------------------

class OpenCvDecoder:
    """The decoder the experiment itself uses. CAP_PROP_POS_MSEC is the frame's
    container timestamp (checked against ffprobe); only its constant offset is
    normalised by the caller."""
    name = "OpenCV FFmpeg (CPU)"

    def __init__(self, path: Path):
        self.cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
        if not self.cap.isOpened():
            raise MediaToolError("The source video can no longer be opened.")

    def read(self):
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        return frame, self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

    def close(self):
        self.cap.release()


# NVDEC, strictly: frames must stay on the GPU until an explicit download. If
# ffmpeg could not start the hardware decoder it would otherwise decode in
# software without saying so; with these filters that case is an error. The
# first form also converts 10-bit sources on the GPU.
NVDEC_INPUT = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
NVDEC_FILTERS = ("scale_cuda=format=nv12,hwdownload,format=nv12", "hwdownload,format=nv12")


def gpu_decode_filter(path: Path, info: SourceInfo) -> tuple[str | None, str]:
    """A real NVDEC decode of the first frames of THIS file.
    (filter chain that works, "") or (None, reason)."""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None, "ffmpeg not found"
    if info.rotation:
        return None, "the video is stored rotated, which the GPU decode path does not handle"
    reason = "NVDEC could not decode this video"
    for chain in NVDEC_FILTERS:
        try:
            result = _run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *NVDEC_INPUT, "-i", str(path), "-map", "0:v:0", "-an", "-sn",
                           "-vf", chain, "-frames:v", "3", "-f", "null", "-"], timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            reason = type(exc).__name__
            continue
        if result.returncode == 0:
            return chain, ""
        reason = _reason(result.stderr, reason)
    return None, reason


class FfmpegDecoder:
    """ffmpeg decodes to raw BGR on a pipe (on the GPU via NVDEC when given a
    `gpu_filter` from gpu_decode_filter); timestamps come from a packet scan of
    the same stream. Not used for rotated sources: the frame size would have
    to follow the display matrix."""

    def __init__(self, path: Path, info: SourceInfo, gpu_filter: str | None = None):
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            raise MediaToolError("ffmpeg not found")
        if info.rotation:
            raise MediaToolError("rotated source")
        self.name = "FFmpeg NVDEC (NVIDIA GPU)" if gpu_filter else "FFmpeg (CPU)"
        self.w, self.h = info.width, info.height
        self.timestamps = frame_timestamps(path)
        self.index = 0
        self.errors = tempfile.TemporaryFile()
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error"] + (NVDEC_INPUT if gpu_filter else []) + [
            "-i", str(path), "-map", "0:v:0", "-an", "-sn"] + (["-vf", gpu_filter] if gpu_filter else []) + [
            "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self.errors, creationflags=NO_WINDOW,
                                     bufsize=self.w * self.h * 3)

    def _stopped_early(self) -> str | None:
        """Why the stream ended before every listed frame was delivered."""
        code = self.proc.wait()
        if code == 0 and self.index >= len(self.timestamps):
            return None
        try:
            self.errors.seek(0)
            lines = self.errors.read(64 * 1024).decode("utf-8", "replace").strip().splitlines()
        except (OSError, ValueError):
            lines = []
        return lines[-1][:200] if lines else f"exit code {code}"

    def read(self):
        size = self.w * self.h * 3
        data = self.proc.stdout.read(size)
        if len(data) < size:
            reason = self._stopped_early()
            if reason:
                raise MediaToolError(f"{self.name} stopped after {self.index} of {len(self.timestamps)} frames ({reason}). "
                                     "Render again with CPU decoding.")
            return None
        if self.index >= len(self.timestamps):
            raise MediaToolError("The decoder produced more frames than the container lists.")
        frame = np.frombuffer(data, np.uint8).reshape(self.h, self.w, 3)
        pts = self.timestamps[self.index]
        self.index += 1
        return frame, pts

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for stream in (self.proc.stdout, self.errors):
            try:
                stream.close()
            except OSError:
                pass


def open_decoder(path: Path, info: SourceInfo, mode: str = "auto"):
    """(decoder, note). gpu = NVDEC, proved on this file first, or
    UnsupportedMode - never a silent software decode. cpu and auto = OpenCV's
    FFmpeg decoder: NVDEC measured no faster here, because inference, not
    decoding, sets the pace (docs/tagged-video.md)."""
    if mode == "gpu":
        chain, reason = gpu_decode_filter(path, info)
        if not chain:
            raise UnsupportedMode(f"GPU decoding (NVDEC) cannot be used for this video: {reason}. "
                                  "Choose Auto or CPU decoding and render again.")
        return FfmpegDecoder(path, info, chain), ""
    return OpenCvDecoder(path), ""


_machine_cache: dict[str, tuple[bool, str]] = {}


def gpu_modes() -> dict[str, tuple[bool, str]]:
    """Whether this computer can GPU-decode and GPU-encode at all, each proved
    once with a real 720p H.264 sample: {"decoder": (ok, reason), "encoder": ...}.
    A particular video can still be refused later (size, codec, rotation)."""
    with _encoder_lock:
        if _machine_cache:
            return dict(_machine_cache)
    ffmpeg = find_tool("ffmpeg")
    encoder = encoder_works("h264_nvenc", 1280, 720) if ffmpeg else (False, "ffmpeg not found")
    decoder = (False, "ffmpeg not found")
    if ffmpeg:
        with tempfile.TemporaryDirectory(prefix="reconize-nvdec-") as folder:
            sample = Path(folder) / "sample.mp4"
            try:
                made = _run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=0.5",
                             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-y", str(sample)], timeout=60)
                if made.returncode == 0:
                    chain, reason = gpu_decode_filter(sample, SourceInfo(width=1280, height=720))
                    decoder = (bool(chain), reason)
                else:
                    decoder = (False, "could not prepare a test sample")
            except (OSError, subprocess.TimeoutExpired) as exc:
                decoder = (False, type(exc).__name__)
    with _encoder_lock:
        _machine_cache.update(decoder=decoder, encoder=encoder)
        return dict(_machine_cache)


# --- encoding with per-frame timestamps -------------------------------------

def _vint(n: int) -> bytes:
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return (n | (1 << (7 * length))).to_bytes(length, "big")
    raise ValueError(n)


def _el(element_id: bytes, payload: bytes) -> bytes:
    return element_id + _vint(len(payload)) + payload


def _uint(n: int) -> bytes:
    return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")


def matroska_header(width: int, height: int) -> bytes:
    """EBML header + an unknown-length Segment with one uncompressed I420 video
    track; timestamps are in microseconds."""
    ebml = _el(b"\x1a\x45\xdf\xa3", _el(b"\x42\x86", _uint(1)) + _el(b"\x42\xf7", _uint(1)) + _el(b"\x42\xf2", _uint(4))
               + _el(b"\x42\xf3", _uint(8)) + _el(b"\x42\x82", b"matroska") + _el(b"\x42\x87", _uint(2)) + _el(b"\x42\x85", _uint(2)))
    info = _el(b"\x15\x49\xa9\x66", _el(b"\x2a\xd7\xb1", _uint(1000)) + _el(b"\x4d\x80", b"reconize") + _el(b"\x57\x41", b"reconize"))
    video = _el(b"\xe0", _el(b"\xb0", _uint(width)) + _el(b"\xba", _uint(height)) + _el(b"\x2e\xb5\x24", b"I420"))
    track = _el(b"\xae", _el(b"\xd7", _uint(1)) + _el(b"\x73\xc5", _uint(1)) + _el(b"\x83", _uint(1)) + _el(b"\x9c", _uint(0))
                + _el(b"\x86", b"V_UNCOMPRESSED") + video)
    return ebml + b"\x18\x53\x80\x67" + b"\x01\xff\xff\xff\xff\xff\xff\xff" + info + _el(b"\x16\x54\xae\x6b", track)


def matroska_frame(pts_seconds: float, i420: bytes) -> tuple[bytes, bytes]:
    """(cluster header, payload) for one key frame at this timestamp. Returned
    in two parts so a 12 MB 4K frame is not copied just to prepend a header."""
    block_head = b"\x81" + struct.pack(">h", 0) + b"\x80"
    block = b"\xa3" + _vint(len(block_head) + len(i420)) + block_head
    timestamp = _el(b"\xe7", _uint(max(0, round(pts_seconds * 1_000_000))))
    return b"\x1f\x43\xb6\x75" + _vint(len(timestamp) + len(block) + len(i420)) + timestamp + block, i420


@dataclass
class EncodePlan:
    encoder: str
    width: int
    height: int
    audio_mode: str = "not_requested"
    audio_args: list[str] = field(default_factory=list)
    audio_offset: float = 0.0


class Encoder:
    """ffmpeg reading timestamped raw frames on stdin and writing the MP4."""

    def __init__(self, source: Path, output: Path, plan: EncodePlan):
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            raise MediaToolError("ffmpeg was not found. Install FFmpeg or set RECONIZE_FFMPEG.")
        with_audio = plan.audio_mode in ("copied", "transcoded")
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "matroska", "-i", "pipe:0"]
        if with_audio:
            cmd += ["-i", str(source)]
        cmd += ["-map", "0:v:0"] + (["-map", "1:a:0"] if with_audio else ["-an"])
        cmd += [*ENCODERS[plan.encoder], "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", *plan.audio_args,
                "-movflags", "+faststart", "-f", "mp4", str(output)]
        self.errors = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=self.errors, creationflags=NO_WINDOW)
        self.proc.stdin.write(matroska_header(plan.width, plan.height))

    def write(self, pts_seconds: float, bgr: np.ndarray) -> None:
        head, payload = matroska_frame(pts_seconds, cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420).tobytes())
        self.proc.stdin.write(head)
        self.proc.stdin.write(payload)

    def finish(self) -> None:
        self.proc.stdin.close()
        code = self.proc.wait()
        if code != 0:
            raise MediaToolError("ffmpeg failed while encoding: " + self.error_text())

    def abort(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def error_text(self) -> str:
        try:
            self.errors.seek(0)
            lines = self.errors.read(64 * 1024).decode("utf-8", "replace").strip().splitlines()
            return lines[-1][:300] if lines else "no error text"
        except (OSError, ValueError):
            return "no error text"

    def close(self) -> None:
        try:
            self.errors.close()
        except OSError:
            pass
