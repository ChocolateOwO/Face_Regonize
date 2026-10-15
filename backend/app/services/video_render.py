"""Tagged video: an optional extra pass that writes a copy of a source video
with every face boxed and named and a "who was seen" panel on the right.

It is a second, independent pass over the source. It reads the experiment's
frozen identity snapshot and threshold, and it writes exactly one thing: the
MP4. It never touches the experiment's sampled-frame counts, people, previews,
reviews or CSV, so rendering cannot double-count anything.

    decode thread -> [frames, byte-bounded] -> this thread: detect, match,
    track, draw -> [frames, byte-bounded] -> encode thread -> ffmpeg

Inference runs on this thread only, one call at a time through the same
shared lock every other recognition path uses; before each call it yields to a
waiting live request (services/inference_priority.py). Both queues are
bounded by BYTES, not frame count: a 4K frame is ~25 MB.

The partial file is `<name>.partial.mp4`; it becomes `<name>.mp4` only after
ffmpeg finished cleanly and the file was read back with the expected number of
frames. Failure, cancel or shutdown removes the partial file.
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from app.services import inference_priority
from app.services import local_video_experiment as video
from app.services import video_render_media as media
from app.services import video_render_overlay as overlay
from app.services import video_render_tracking as tracking

logger = logging.getLogger(__name__)

READ_BUDGET_BYTES = 96 * 1024 * 1024    # decoded frames waiting for inference
WRITE_BUDGET_BYTES = 160 * 1024 * 1024  # drawn frames waiting for the encoder
MIN_ESTIMATE_BYTES = 64 * 1024 * 1024
ACTIVE = {"queued", "running"}
DETECTORS = {
    "plain": "Plain - the same full-frame detector the experiment uses",
    "tiled": "Tiled - Event Photo crowd scan for small or distant faces; several times the GPU time",
}


class RenderSettings(BaseModel):
    """Frozen when the batch/upload is started (or when a later render of a
    retained local upload is requested)."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    keep_audio: bool = True
    detector: Literal["plain", "tiled"] = "plain"
    # Chosen independently. "gpu" runs on the GPU or the render stops with the
    # reason; it is never swapped for the CPU. See video_render_media.
    decoder: Literal["auto", "gpu", "cpu"] = "auto"
    encoder: Literal["auto", "gpu", "cpu"] = "auto"


class RenderCancelled(Exception):
    pass


class RenderError(Exception):
    """Message is shown to the admin as the reason the render failed."""


def check_modes(settings: dict) -> None:
    """Refuse an explicit GPU choice this computer cannot honour at all, before
    anything is queued. A particular video can still be refused when it is
    rendered (output wider than the GPU encoder takes, codec, rotation)."""
    if "gpu" not in (settings.get("decoder"), settings.get("encoder")):
        return
    available = media.gpu_modes()
    for key, what in (("decoder", "GPU decoding (NVDEC)"), ("encoder", "GPU encoding (h264_nvenc)")):
        ok, reason = available[key]
        if settings.get(key) == "gpu" and not ok:
            raise video.MediaError(f"{what} is not available on this computer: {reason}. Choose Auto or CPU for the tagged video's {key}.")


def new_status(settings: dict, status: str = "queued") -> dict:
    return {"status": status, "detector": settings.get("detector", "plain"), "keep_audio": bool(settings.get("keep_audio", True)),
            "decoder_mode": settings.get("decoder", "auto"), "encoder_mode": settings.get("encoder", "auto"),
            "requested_at": video.now(), "started_at": None, "finished_at": None, "error": None,
            "frames_written": 0, "progress_percent": 0.0, "output_bytes": None}


def output_path(job_id: str, source_key: str) -> Path:
    folder = video.directory(job_id) / "renders"
    if folder.is_symlink() or folder.resolve().parent != video.directory(job_id).resolve():
        raise video.MediaError("Render storage is unavailable.")
    if not source_key.replace("-", "").isalnum():
        raise video.NotFound("Experiment not found")
    return folder / f"{source_key}.mp4"


def partial_path(final: Path) -> Path:
    return final.with_name(final.stem + ".partial.mp4")


def estimate_output_bytes(width: int, height: int, duration_seconds: float, with_audio: bool) -> int:
    """A deliberately generous guess for the disk reservation (about 4 bits per
    output pixel per second, +25%); rechecked against real bytes while writing."""
    bits_per_second = max(2_000_000, width * height * 4) + (200_000 if with_audio else 0)
    return max(MIN_ESTIMATE_BYTES, int(max(1.0, duration_seconds) * bits_per_second / 8 * 1.25))


class ByteBudgetQueue:
    """FIFO whose capacity is a number of BYTES. One oversized item is always
    admitted when the queue is empty, so a single huge frame cannot deadlock."""

    def __init__(self, budget: int):
        self.budget = budget
        self.used = 0
        self.peak = 0
        self._items: collections.deque = collections.deque()
        self._cond = threading.Condition()

    def put(self, item, nbytes: int, halted: Callable[[], bool]) -> bool:
        with self._cond:
            while self._items and self.used + nbytes > self.budget:
                if halted():
                    return False
                self._cond.wait(0.1)
            self._items.append((item, nbytes))
            self.used += nbytes
            self.peak = max(self.peak, self.used)
            self._cond.notify_all()
            return True

    def get(self, halted: Callable[[], bool]):
        """Next item, or raises RenderCancelled once `halted()` with nothing queued."""
        with self._cond:
            while not self._items:
                if halted():
                    raise RenderCancelled()
                self._cond.wait(0.1)
            item, nbytes = self._items.popleft()
            self.used -= nbytes
            self._cond.notify_all()
            return item

    def clear(self) -> None:
        with self._cond:
            self._items.clear()
            self.used = 0
            self._cond.notify_all()


def render_source(*, source_path: Path, final_path: Path, settings: dict, names: dict, index, threshold: float,
                  detect: Callable, nominal_fps: float, expected_frames: int, duration_seconds: float,
                  cancelled: Callable[[], bool], report: Callable[[dict], None], label: str) -> dict:
    """Render one source. `detect(frame, timings)` returns faces with
    .embedding/.bbox; `report(fields)` persists progress. Returns the final
    status fields; raises nothing - the outcome is in the returned status."""
    status: dict = {}
    partial = partial_path(final_path)
    claim = ("render", str(final_path))
    halt = threading.Event()
    errors: list[BaseException] = []
    decoder = encoder = None
    threads: list[threading.Thread] = []
    queues: list[ByteBudgetQueue] = []
    began = time.perf_counter()
    timings: dict = {}
    frame_no = written = 0

    def tell(**fields) -> None:
        status.update(fields)
        try:
            report(dict(status))
        except Exception:  # noqa: BLE001 - a progress write must never kill the render
            logger.exception("Could not record render progress")

    try:
        tell(status="running", started_at=video.now(), error=None)
        if not media.find_tool("ffmpeg") or not media.find_tool("ffprobe"):
            raise RenderError("FFmpeg (ffmpeg and ffprobe) was not found. Install FFmpeg or set RECONIZE_FFMPEG, then render again.")
        info = media.probe(source_path)
        decoder_mode, encoder_mode = settings.get("decoder") or "auto", settings.get("encoder") or "auto"
        decoder, decode_note = media.open_decoder(source_path, info, decoder_mode)
        first = decoder.read()
        if first is None:
            raise RenderError("The source video has no decodable frames.")
        height, width = first[0].shape[:2]
        if width * height > video.MAX_PIXELS:
            raise RenderError("The source exceeds the 3840x2160 pixel limit.")
        out_w, out_h = overlay.output_size(width, height)
        encoder_name, encoder_note = media.choose_encoder(out_w, out_h, encoder_mode)
        audio_mode, audio_args = media.audio_plan(source_path, info, bool(settings.get("keep_audio", True)))
        painter = overlay.Painter(width, height, names)
        fps = nominal_fps if nominal_fps and nominal_fps > 0 else (info.nominal_fps or 30.0)
        estimate = estimate_output_bytes(out_w, out_h, duration_seconds or (expected_frames / fps if expected_frames else 60),
                                         audio_mode in ("copied", "transcoded"))
        final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            video.claim_disk(claim, estimate, f"the tagged video of {label}", final_path.parent)
        except video.MediaError as exc:
            raise RenderError(str(exc)) from exc
        partial.unlink(missing_ok=True)
        encoder = media.Encoder(source_path, partial, media.EncodePlan(encoder_name, out_w, out_h, audio_mode, audio_args))
        tell(decoder_mode=decoder_mode, decoder=decoder.name, decoder_note=decode_note or None, encoder_mode=encoder_mode, encoder=encoder_name,
             encoder_label=media.ENCODER_LABELS[encoder_name], encoder_note=encoder_note or None, audio=audio_mode,
             audio_source_codec=info.audio_codec, font=painter.fonts.description, source_width=width, source_height=height,
             width=out_w, height=out_h, padded_pixels=[out_w - width - overlay.PANEL_W, out_h - height],
             frames_expected=expected_frames or None)

        read_queue, write_queue = ByteBudgetQueue(READ_BUDGET_BYTES), ByteBudgetQueue(WRITE_BUDGET_BYTES)
        queues.extend((read_queue, write_queue))

        def read_loop() -> None:
            try:
                item = first
                while item is not None and not halt.is_set():
                    if not read_queue.put(item, item[0].nbytes, halt.is_set):
                        return
                    item = decoder.read()
            except BaseException as exc:  # noqa: BLE001 - surfaced on the render thread
                errors.append(exc)
                halt.set()
            finally:
                read_queue.put(None, 0, halt.is_set)  # end marker; skipped only when already halted

        def write_loop() -> None:
            nonlocal written
            try:
                while True:
                    item = write_queue.get(lambda: halt.is_set())
                    if item is None:
                        return
                    encoder.write(*item)
                    written += 1
            except RenderCancelled:
                return
            except BaseException as exc:  # noqa: BLE001 - e.g. ffmpeg died: broken pipe
                errors.append(exc)
                halt.set()

        threads = [threading.Thread(target=read_loop, name="render-decode", daemon=True),
                   threading.Thread(target=write_loop, name="render-encode", daemon=True)]
        for thread in threads:
            thread.start()

        tracks: list[tracking.Track] = []
        first_seen: dict[str, float] = {}
        last_seen: dict[str, float] = {}
        raw_first = previous_out = None
        gaps_min, gaps_max, repaired = None, 0.0, 0
        last_report = last_disk = time.perf_counter()
        draw_seconds = yield_seconds = 0.0
        while True:
            if cancelled():
                raise RenderCancelled()
            if errors:
                raise errors[0]
            item = read_queue.get(lambda: halt.is_set() or cancelled())
            if item is None:
                break
            frame, raw_pts = item
            if frame.shape[0] != height or frame.shape[1] != width:
                raise RenderError("The source changes resolution part-way; it cannot be rendered.")
            # Real source timestamps, shifted so the first frame sits at the
            # stream's own start time. A missing/non-increasing timestamp is
            # replaced by one nominal frame interval and counted.
            if raw_first is None:
                raw_first = raw_pts
            out_pts = (raw_pts - raw_first) + info.video_start
            if previous_out is not None:
                if out_pts <= previous_out:
                    out_pts = previous_out + 1.0 / fps
                    repaired += 1
                gap = out_pts - previous_out
                gaps_min = gap if gaps_min is None else min(gaps_min, gap)
                gaps_max = max(gaps_max, gap)
            previous_out = out_pts
            now = out_pts - info.video_start  # video time since the first frame

            yield_seconds += inference_priority.yield_to_interactive()
            faces = detect(frame, timings)
            matches = index.match_batch(np.stack([f.embedding for f in faces]), threshold) if faces else []
            tracking.update_tracks(tracks, [(tuple(f.bbox), m.person_id, float(m.score)) for f, m in zip(faces, matches)], frame_no)

            draw_begin = time.perf_counter()
            boxes = []
            for track in tracking.drawable(tracks, frame_no):
                person_id = track.identity()
                if person_id:
                    last_seen[person_id] = now
                    first_seen.setdefault(person_id, now)
                boxes.append((track.bbox, person_id))
            visible = sorted((p for p, seen in last_seen.items() if now - seen <= overlay.LINGER_SECONDS), key=lambda p: first_seen[p])
            rows = tuple((p, now - last_seen[p] < (tracking.TRACK_DRAW_GAP + 1) / fps, int(now - last_seen[p])) for p in visible)
            canvas = painter.compose(frame, boxes, rows, len(first_seen), now)
            draw_seconds += time.perf_counter() - draw_begin
            if not write_queue.put((out_pts, canvas), canvas.nbytes, lambda: halt.is_set() or cancelled()):
                continue  # loop top raises the cancel/error
            frame_no += 1

            clock = time.perf_counter()
            if clock - last_disk >= 2.0:
                last_disk = clock
                size = partial.stat().st_size if partial.exists() else 0
                try:
                    video.claim_disk(claim, max(estimate - size, estimate // 10), f"the tagged video of {label}", final_path.parent)
                except video.MediaError as exc:
                    raise RenderError(str(exc)) from exc
            if clock - last_report >= 1.0:
                last_report = clock
                tell(frames_written=written, frames_processed=frame_no,
                     progress_percent=round(min(99.9, 100 * frame_no / expected_frames), 1) if expected_frames else None,
                     seconds=round(clock - began, 1))

        if errors:
            raise errors[0]
        write_queue.put(None, 0, lambda: False)
        threads[1].join()
        if errors:
            raise errors[0]
        encoder.finish()
        summary = media.output_summary(partial)
        if summary.get("video_frames") != frame_no:
            raise RenderError(f"The encoder wrote {summary.get('video_frames')} frames but {frame_no} were rendered; the file was discarded.")
        os.replace(partial, final_path)
        source_vfr = bool(gaps_min is not None and gaps_max > gaps_min * 1.5 + 0.002)
        tell(status="completed", finished_at=video.now(), frames_written=frame_no, frames_processed=frame_no, progress_percent=100.0,
             output_bytes=final_path.stat().st_size, duration_seconds=summary.get("duration"),
             timing="source timestamps preserved (no frames duplicated or dropped)", source_vfr=source_vfr,
             frame_interval_seconds=[round(gaps_min or 0.0, 6), round(gaps_max, 6)], timestamp_repairs=repaired,
             audio_written=summary.get("audio_codec"), people_shown=len(first_seen),
             seconds=round(time.perf_counter() - began, 1),
             stage_seconds={"inference": round(timings.get("inference_seconds", 0.0), 3),
                            "inference_wait": round(timings.get("inference_wait_seconds", 0.0), 3),
                            "yielded_to_live_requests": round(yield_seconds, 3), "draw": round(draw_seconds, 3)},
             queue_peak_bytes={"decoded": read_queue.peak, "drawn": write_queue.peak})
    except RenderCancelled:
        tell(status="cancelled", finished_at=video.now(), error="Rendering was cancelled; the partial file was removed.")
    except (RenderError, media.MediaToolError, video.MediaError) as exc:
        tell(status="failed", finished_at=video.now(), error=str(exc))
    except Exception as exc:  # noqa: BLE001 - recognition results must survive any render failure
        logger.exception("Tagged-video render failed")
        tell(status="failed", finished_at=video.now(),
             error="Rendering failed (" + type(exc).__name__ + "). Recognition results are unaffected; see the backend log.")
    finally:
        halt.set()
        if status.get("status") != "completed" and encoder is not None:
            encoder.abort()  # kills ffmpeg, which also unblocks a pending pipe write
        for queue in queues:
            queue.clear()
        for thread in threads:
            thread.join(timeout=10)
        if decoder is not None:
            try:
                decoder.close()
            except Exception:  # noqa: BLE001
                pass
        if encoder is not None:
            encoder.close()
        if status.get("status") != "completed":
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove partial render %s", partial)
        video.release_disk(claim)
    return dict(status)
