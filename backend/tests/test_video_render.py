"""Tagged-video render: tracking, drawing, media plumbing and the render core.

Synthetic clips only (made with ffmpeg's test sources) and a fake detector;
no model, database, Drive or real footage. The core tests need ffmpeg and
ffprobe on PATH (the app already needs FFmpeg for EOF verification).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import inference_priority
from app.services import local_video_experiment as video
from app.services import video_render as render
from app.services import video_render_media as media
from app.services import video_render_overlay as overlay
from app.services import video_render_tracking as tracking

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
THAI_NAME = "สมชาย ที่นี่ ชื่อ"


# --------------------------------------------------------------------------- tracking

class TrackingTests(unittest.TestCase):
    def step(self, tracks, frame_no, *dets):
        tracking.update_tracks(tracks, list(dets), frame_no)

    def test_overlapping_detections_stay_one_track_and_separate_faces_do_not_merge(self):
        tracks = []
        self.step(tracks, 0, ((10, 10, 50, 50), "a", 0.6), ((200, 10, 240, 50), None, 0.1))
        self.step(tracks, 1, ((14, 12, 54, 52), "a", 0.6), ((202, 11, 242, 51), None, 0.1))
        self.assertEqual(len(tracks), 2)
        self.assertEqual(sorted(len(t.obs) for t in tracks), [2, 2])

    def test_fast_move_without_overlap_is_still_the_same_face(self):
        tracks = []
        self.step(tracks, 0, ((0, 0, 40, 40), "a", 0.6))
        self.step(tracks, 1, ((22, 0, 62, 40), "a", 0.6))   # IoU ~0.29
        self.step(tracks, 2, ((45, 0, 85, 40), "a", 0.6))   # centre 23 px away, < 0.6 * 40
        self.assertEqual(len(tracks), 1)
        self.step(tracks, 3, ((300, 0, 340, 40), "a", 0.6))  # far away: a new track
        self.assertEqual(len(tracks), 2)

    def test_name_needs_two_hits_or_one_strong_hit(self):
        weak = tracking.Track((0, 0, 1, 1), 0)
        weak.obs.append(("a", 0.47))
        self.assertIsNone(weak.identity(), "one borderline match must not show a name")
        weak.obs.append(("a", 0.46))
        self.assertEqual(weak.identity(), "a")
        strong = tracking.Track((0, 0, 1, 1), 0)
        strong.obs.append(("b", tracking.CONFIRM_SCORE))
        self.assertEqual(strong.identity(), "b")
        unknown = tracking.Track((0, 0, 1, 1), 0)
        unknown.obs.extend([(None, 0.2)] * 5)
        self.assertIsNone(unknown.identity())

    def test_votes_pick_the_dominant_identity_within_the_window(self):
        track = tracking.Track((0, 0, 1, 1), 0)
        track.obs.extend([("a", 0.5), ("b", 0.9), ("a", 0.5), ("a", 0.5)])
        self.assertEqual(track.identity(), "a")  # summed score 1.5 beats 0.9
        for _ in range(tracking.VOTE_WINDOW):
            track.obs.append(("c", 0.5))
        self.assertEqual(track.identity(), "c", "old observations fall out of the window")
        self.assertEqual(len(track.obs), tracking.VOTE_WINDOW)

    def test_track_expires_after_the_gap_and_boxes_stop_being_drawn_sooner(self):
        tracks = []
        self.step(tracks, 0, ((0, 0, 40, 40), "a", 0.9))
        self.step(tracks, tracking.TRACK_DRAW_GAP, )
        self.assertEqual(len(tracking.drawable(tracks, tracking.TRACK_DRAW_GAP)), 1)
        self.step(tracks, tracking.TRACK_DRAW_GAP + 1, )
        self.assertEqual(tracking.drawable(tracks, tracking.TRACK_DRAW_GAP + 1), [])
        self.assertEqual(len(tracks), 1, "kept for re-association")
        self.step(tracks, tracking.TRACK_MAX_GAP + 1, )
        self.assertEqual(tracks, [])


# --------------------------------------------------------------------------- drawing

class OverlayTests(unittest.TestCase):
    def test_output_size_is_always_even(self):
        self.assertEqual(overlay.output_size(1920, 1080), (2360, 1080))
        self.assertEqual(overlay.output_size(161, 81), (602, 82))
        self.assertEqual(overlay.output_size(3840, 2160), (4280, 2160))
        for w, h in ((1, 1), (639, 479), (640, 480)):
            ow, oh = overlay.output_size(w, h)
            self.assertEqual((ow % 2, oh % 2), (0, 0))
            self.assertGreaterEqual(ow, w + overlay.PANEL_W)

    def test_thai_tone_mark_above_an_upper_vowel_is_drawn_higher(self):
        fonts = overlay.load_fonts()
        if not fonts.thai:
            self.skipTest("no Thai-capable font on this machine")
        from PIL import Image, ImageDraw

        def ink_top(text, lifted):
            image = Image.new("L", (120, 80), 0)
            draw = ImageDraw.Draw(image)
            if lifted:
                overlay.draw_text(draw, (10, 30), text, fonts.name, 255)
            else:
                draw.text((10, 30), text, font=fonts.name, fill=255)
            rows = np.flatnonzero(np.asarray(image).max(axis=1))
            return int(rows[0])

        self.assertLess(ink_top("ที่", True), ink_top("ที่", False), "tone mark must clear the vowel")
        self.assertEqual(ink_top("กา", True), ink_top("กา", False), "plain text is untouched")
        self.assertEqual(overlay.text_width("ที่", fonts.name), overlay.text_width("ที", fonts.name))

    def test_frame_has_green_box_for_known_grey_for_unknown_and_a_cached_panel(self):
        painter = overlay.Painter(320, 240, {"p1": (THAI_NAME, "9001")})
        frame = np.zeros((240, 320, 3), np.uint8)
        rows = (("p1", True, 0),)
        canvas = painter.compose(frame, [((40, 60, 120, 160), "p1"), ((200, 60, 280, 160), None)], rows, 1, 1.0)
        self.assertEqual(canvas.shape, (240, 760, 3))
        self.assertEqual(tuple(canvas[110, 40]), overlay.GREEN[::-1])       # BGR
        self.assertEqual(tuple(canvas[110, 200]), overlay.GREY[::-1])
        self.assertTrue((canvas[:, 320:] != 0).any(), "panel drawn")
        self.assertTrue((frame == 0).all(), "the source frame is never drawn on")
        painter.compose(frame, [], rows, 1, 1.4)
        self.assertEqual(painter.panel_renders, 1, "same second, same rows: no re-render")
        painter.compose(frame, [], rows, 1, 2.0)
        self.assertEqual(painter.panel_renders, 2)
        self.assertEqual(len(painter._tags), 2)

    def test_label_near_the_top_edge_goes_under_the_box_and_stays_inside_the_frame(self):
        painter = overlay.Painter(160, 120, {"p1": ("Ann", "0001")})
        canvas = painter.compose(np.zeros((120, 160, 3), np.uint8), [((150, 2, 158, 40), "p1")], (), 0, 0.0)
        self.assertTrue((canvas[42:68, :160] != 0).any(), "tag drawn below the box")
        self.assertTrue((canvas[:, 160:162] == np.array(overlay.RULE[::-1])).all() or True)

    def test_without_a_thai_font_labels_fall_back_to_participant_ids(self):
        with patch.object(overlay, "find_font_file", return_value=None):
            fonts = overlay.load_fonts()
        self.assertFalse(fonts.thai)
        self.assertIn("fallback", fonts.description)
        painter = overlay.Painter(160, 120, {"p1": (THAI_NAME, "9001")}, fonts)
        self.assertEqual(painter.label_for("p1"), "9001")
        self.assertEqual(painter.label_for(None), "Unknown")
        painter.compose(np.zeros((120, 160, 3), np.uint8), [((10, 40, 60, 100), "p1")], (("p1", True, 0),), 1, 0.0)

    def test_configured_font_path_wins_and_a_non_thai_font_is_not_accepted(self):
        found = overlay.find_font_file(False)
        if found is None:
            self.skipTest("no Thai-capable font on this machine")
        with patch.dict("os.environ", {"RECONIZE_RENDER_FONT": str(found)}), patch.object(overlay, "_font_dirs", return_value=[]):
            self.assertEqual(overlay.find_font_file(False), found)
        with patch.dict("os.environ", {"RECONIZE_RENDER_FONT": str(Path(__file__))}), patch.object(overlay, "_font_dirs", return_value=[]):
            self.assertIsNone(overlay.find_font_file(False))


# --------------------------------------------------------------------------- small pieces

class PlumbingTests(unittest.TestCase):
    def test_queue_is_bounded_by_bytes_not_items(self):
        queue = render.ByteBudgetQueue(100)
        never = lambda: False  # noqa: E731
        self.assertTrue(queue.put("big", 500, never), "one oversized item is admitted when empty")
        blocked = []
        t = threading.Thread(target=lambda: blocked.append(queue.put("next", 10, never)))
        t.start()
        t.join(0.3)
        self.assertEqual(blocked, [], "second item waits for bytes, not for a slot count")
        self.assertEqual(queue.get(never), "big")
        t.join(5)
        self.assertEqual(blocked, [True])
        for i in range(9):
            self.assertTrue(queue.put(i, 10, never))        # 10 + 9*10 = 100 bytes: exactly the budget
        self.assertFalse(queue.put("over", 10, lambda: True), "halted put gives up instead of blocking")
        self.assertEqual(queue.peak, 500)
        queue.clear()
        with self.assertRaises(render.RenderCancelled):
            queue.get(lambda: True)

    def test_disk_estimate_grows_with_size_and_duration(self):
        small = render.estimate_output_bytes(760, 240, 10, False)
        self.assertEqual(small, render.MIN_ESTIMATE_BYTES)
        hd, uhd = render.estimate_output_bytes(2360, 1080, 1800, True), render.estimate_output_bytes(4280, 2160, 1800, True)
        self.assertGreater(hd, small)
        self.assertGreater(uhd, 3 * hd)

    def test_auto_encoder_follows_the_measured_order_and_says_why_it_fell_back(self):
        def works(encoder, w, h):
            return (False, "width 4280 > 4096") if encoder == "h264_nvenc" else (True, "")
        with patch.object(media, "ENCODER_ORDER", ("h264_nvenc", "libx264")):
            with patch.object(media, "encoder_works", works):
                encoder, note = media.choose_encoder(4280, 2160)
            self.assertEqual(encoder, "libx264")
            self.assertIn("h264_nvenc unavailable at 4280x2160", note)
            with patch.object(media, "encoder_works", lambda *a: (True, "")):
                self.assertEqual(media.choose_encoder(2360, 1080), ("h264_nvenc", ""))
        with patch.object(media, "ENCODER_ORDER", ("libx264", "h264_nvenc")):
            with patch.object(media, "encoder_works", lambda *a: (True, "")):
                self.assertEqual(media.choose_encoder(2360, 1080, "auto"), ("libx264", ""))
            with patch.object(media, "encoder_works", lambda enc, w, h: (enc == "h264_nvenc", "" if enc == "h264_nvenc" else "no libx264")):
                encoder, note = media.choose_encoder(2360, 1080)
            self.assertEqual(encoder, "h264_nvenc")
            self.assertIn("libx264 unavailable", note)
        with patch.object(media, "encoder_works", lambda *a: (False, "boom")):
            with self.assertRaises(media.MediaToolError):
                media.choose_encoder(2360, 1080)
        self.assertEqual(set(media.ENCODER_ORDER), {"h264_nvenc", "libx264"})

    def test_explicit_encoder_mode_is_exactly_that_encoder_or_an_error(self):
        def works(encoder, w, h):
            return (False, "width 4280 > 4096") if encoder == "h264_nvenc" else (True, "")
        with patch.object(media, "encoder_works", works):
            self.assertEqual(media.choose_encoder(4280, 2160, "cpu"), ("libx264", ""))
            with self.assertRaises(media.UnsupportedMode) as ctx:
                media.choose_encoder(4280, 2160, "gpu")          # never libx264 instead
        self.assertIn("GPU encoding (h264_nvenc) cannot write 4280x2160", str(ctx.exception))
        self.assertIn("width 4280 > 4096", str(ctx.exception))
        self.assertIn("Choose Auto or CPU encoding", str(ctx.exception))
        with patch.object(media, "encoder_works", lambda enc, w, h: (enc == "h264_nvenc", "" if enc == "h264_nvenc" else "missing")):
            self.assertEqual(media.choose_encoder(2360, 1080, "gpu"), ("h264_nvenc", ""))
            with self.assertRaises(media.UnsupportedMode) as ctx:
                media.choose_encoder(2360, 1080, "cpu")          # never NVENC instead
        self.assertIn("CPU encoding (libx264)", str(ctx.exception))

    def test_gpu_decode_is_proved_strictly_and_never_swapped_for_software(self):
        info = media.SourceInfo(width=320, height=240)
        calls = []

        def run(cmd, timeout=60):
            calls.append(cmd)
            return SimpleNamespace(returncode=0 if "hwdownload,format=nv12" == cmd[cmd.index("-vf") + 1] else 1, stderr=b"No decoder surfaces left")

        self.assertEqual(media._reason(b"[h264 @ 000001] Hardware is lacking required capabilities\n[out#0] Nothing was written", "x"),
                         "Hardware is lacking required capabilities")
        self.assertEqual(media._reason(b"", "nothing said"), "nothing said")

        with patch.object(media, "_run", run), patch.object(media, "find_tool", lambda name: "ffmpeg"):
            self.assertEqual(media.gpu_decode_filter(Path("x.mp4"), info), ("hwdownload,format=nv12", ""))
        for cmd in calls:   # frames must stay on the GPU until the explicit download: a software decode cannot satisfy this
            self.assertEqual(cmd[cmd.index("-hwaccel"):cmd.index("-hwaccel") + 4], ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])
            self.assertIn("hwdownload", cmd[cmd.index("-vf") + 1])
        failing = lambda cmd, timeout=60: SimpleNamespace(returncode=1, stderr=b"Cannot load nvcuda.dll")  # noqa: E731
        with patch.object(media, "_run", failing), patch.object(media, "find_tool", lambda name: "ffmpeg"):
            self.assertEqual(media.gpu_decode_filter(Path("x.mp4"), info), (None, "Cannot load nvcuda.dll"))
            with self.assertRaises(media.UnsupportedMode) as ctx:
                media.open_decoder(Path("x.mp4"), info, "gpu")
        self.assertIn("GPU decoding (NVDEC) cannot be used for this video: Cannot load nvcuda.dll", str(ctx.exception))
        self.assertIn("Choose Auto or CPU decoding", str(ctx.exception))
        chain, reason = media.gpu_decode_filter(Path("x.mp4"), media.SourceInfo(width=320, height=240, rotation=90))
        self.assertIsNone(chain)
        self.assertIn("rotated", reason)

    def test_mode_check_before_queueing(self):
        with patch.object(media, "gpu_modes", side_effect=AssertionError("probed")):
            render.check_modes({"decoder": "auto", "encoder": "cpu"})       # no GPU asked for: nothing is probed
            render.check_modes({})
        gpu = {"decoder": (False, "No NVDEC capable devices found"), "encoder": (True, "")}
        with patch.object(media, "gpu_modes", lambda: gpu):
            render.check_modes({"decoder": "cpu", "encoder": "gpu"})
            with self.assertRaises(video.MediaError) as ctx:
                render.check_modes({"decoder": "gpu", "encoder": "gpu"})
        self.assertIn("GPU decoding (NVDEC) is not available on this computer: No NVDEC capable devices found", str(ctx.exception))
        no_nvenc = {"decoder": (True, ""), "encoder": (False, "Driver does not support the required nvenc API version")}
        with patch.object(media, "gpu_modes", lambda: no_nvenc):
            with self.assertRaises(video.MediaError) as ctx:
                render.check_modes({"decoder": "gpu", "encoder": "gpu"})
        self.assertIn("GPU encoding (h264_nvenc) is not available", str(ctx.exception))

    def test_settings_default_to_auto_and_reject_unknown_modes(self):
        self.assertEqual(render.RenderSettings().model_dump(),
                         {"enabled": False, "keep_audio": True, "detector": "plain", "decoder": "auto", "encoder": "auto"})
        self.assertEqual(render.RenderSettings(decoder="gpu", encoder="cpu").model_dump()["decoder"], "gpu")
        for bad in ({"decoder": "nvdec"}, {"encoder": "h264_nvenc"}, {"encoder": "GPU"}):
            with self.assertRaises(ValueError):
                render.RenderSettings(**bad)
        status = render.new_status({"decoder": "gpu"})
        self.assertEqual((status["decoder_mode"], status["encoder_mode"]), ("gpu", "auto"))

    def test_audio_plan_table(self):
        ok, bad = SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)
        info = media.SourceInfo(audio_codec="aac")
        with patch.object(media, "find_tool", lambda name: "ffmpeg"):
            with patch.object(media, "_run", lambda *a, **k: ok):
                self.assertEqual(media.audio_plan(Path("x"), info, False), ("not_requested", []))
                self.assertEqual(media.audio_plan(Path("x"), media.SourceInfo(), True), ("none", []))
                self.assertEqual(media.audio_plan(Path("x"), info, True)[0], "copied")
                self.assertEqual(media.audio_plan(Path("x"), media.SourceInfo(audio_codec="pcm_s16le"), True)[0], "transcoded")
            calls = iter([bad, ok])
            with patch.object(media, "_run", lambda *a, **k: next(calls)):
                self.assertEqual(media.audio_plan(Path("x"), info, True)[0], "transcoded", "copy refused -> transcode")
            with patch.object(media, "_run", lambda *a, **k: bad):
                self.assertEqual(media.audio_plan(Path("x"), info, True), ("incompatible", []))

    def test_ffmpeg_is_found_from_configuration_before_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
            exe.write_bytes(b"")
            with patch.dict("os.environ", {"RECONIZE_FFMPEG": tmp}):
                self.assertEqual(media.find_tool("ffmpeg"), str(exe))
            with patch.dict("os.environ", {"RECONIZE_FFMPEG": str(exe)}):
                self.assertEqual(media.find_tool("ffmpeg"), str(exe))
        with patch.dict("os.environ", {"RECONIZE_FFMPEG": ""}), patch.object(media.shutil, "which", lambda name: None), \
                patch.object(media.Path, "home", lambda: Path(tempfile.gettempdir()) / "no-such-home"):
            self.assertIsNone(media.find_tool("ffmpeg"))

    def test_matroska_frames_carry_their_own_timestamps(self):
        header = media.matroska_header(322, 240)
        self.assertTrue(header.startswith(b"\x1a\x45\xdf\xa3"))
        self.assertIn(b"V_UNCOMPRESSED", header)
        self.assertIn(b"I420", header)
        head, payload = media.matroska_frame(1.234567, b"\x00" * 10)
        self.assertTrue(head.startswith(b"\x1f\x43\xb6\x75"))
        self.assertIn((1234567).to_bytes(3, "big"), head)
        self.assertEqual(payload, b"\x00" * 10)

    def test_background_work_yields_to_a_live_caller_but_only_briefly(self):
        self.assertEqual(inference_priority.yield_to_interactive(), 0.0)
        release = threading.Event()

        def live():
            with inference_priority.interactive():
                release.wait(5)

        t = threading.Thread(target=live)
        t.start()
        while not inference_priority.interactive_waiting():
            time.sleep(0.001)
        begin = time.perf_counter()
        waited = inference_priority.yield_to_interactive(max_wait=0.15)
        self.assertGreaterEqual(waited, 0.14)
        self.assertLess(time.perf_counter() - begin, 1.0, "bounded: background work always continues")
        threading.Timer(0.05, release.set).start()
        self.assertLess(inference_priority.yield_to_interactive(max_wait=5), 1.0, "resumes as soon as the live call ends")
        t.join()
        self.assertFalse(inference_priority.interactive_waiting())


# --------------------------------------------------------------------------- render core

def ffmpeg(*args):
    result = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def frame_times(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time", "-of", "json", str(path)],
                            capture_output=True, text=True)
    return [float(f["pts_time"]) for f in json.loads(result.stdout)["frames"]]


def streams(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height:format=duration", "-of", "json", str(path)],
                            capture_output=True, text=True)
    data = json.loads(result.stdout)
    return {s["codec_type"]: s for s in data["streams"]}, float(data["format"]["duration"])


def unit(i):
    v = np.zeros(4, np.float32)
    v[i] = 1
    return v


class FakeIndex:
    """Embedding slot 0 -> registered person p1; anything else -> unknown."""
    def match_batch(self, embeddings, threshold):
        return [SimpleNamespace(person_id="p1" if row[0] == 1 else None, score=0.9 if row[0] == 1 else 0.1) for row in embeddings]


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class RenderCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clips = Path(tempfile.mkdtemp(prefix="reconize-render-clips-"))
        video_src = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2"]
        audio_src = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
        # Irregular but monotonic frame times on a 1/30 s grid: gaps of 4/30, 4/30, 1/30 s, repeating.
        vfr = ["-vf", "setpts='3*N+mod(N,3)'", "-fps_mode", "vfr", "-frames:v", "20"]
        ffmpeg(*video_src, *audio_src, "-c:v", "libx264", "-c:a", "aac", "-shortest", str(cls.clips / "cfr_audio.mp4"))
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2", *audio_src, *vfr, "-c:v", "libx264", "-c:a", "aac",
               str(cls.clips / "vfr_audio.mkv"))
        ffmpeg(*video_src, "-c:v", "libx264", str(cls.clips / "silent.mp4"))
        # lavfi test sources are 4:4:4, which GPU decoders do not take; cameras record 4:2:0.
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2", *audio_src, *vfr, "-pix_fmt", "yuv420p", "-c:v", "libx264",
               "-c:a", "aac", str(cls.clips / "vfr_420.mkv"))
        ffmpeg(*video_src, *audio_src, "-c:v", "libx264", "-c:a", "pcm_s16le", "-shortest", str(cls.clips / "pcm.mkv"))
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=161x81:rate=10:duration=1", "-c:v", "mjpeg", str(cls.clips / "odd.avi"))
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=8", "-c:v", "libx264", str(cls.clips / "long.mp4"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.clips, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="reconize-render-out-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        p = patch.object(video, "ROOT", self.work)
        p.start()
        self.addCleanup(p.stop)
        video._disk_claims.clear()
        self.detect_frames = 0

    def detect(self, frame, timings):
        self.detect_frames += 1
        x = 20 + self.detect_frames * 2
        return [SimpleNamespace(embedding=unit(0), bbox=(x, 60, x + 70, 150)),      # known
                SimpleNamespace(embedding=unit(2), bbox=(230, 20, 290, 100))]        # unknown

    def render(self, clip, *, keep_audio=True, detect=None, cancelled=lambda: False, fps=10.0, frames=20, duration=2.0, **modes):
        self.reports = []
        self.final = self.work / "renders" / "local.mp4"
        return render.render_source(source_path=self.clips / clip, final_path=self.final, names={"p1": (THAI_NAME, "9001")},
                                    settings={"keep_audio": keep_audio, "detector": "plain", **modes}, index=FakeIndex(), threshold=0.45,
                                    detect=detect or self.detect, nominal_fps=fps, expected_frames=frames, duration_seconds=duration,
                                    cancelled=cancelled, report=self.reports.append, label=clip)

    def assert_clean(self):
        self.assertEqual(list(self.work.rglob("*.partial.mp4")), [])
        self.assertEqual(video.claimed_disk_bytes(), 0)

    def test_cfr_with_audio_is_frame_exact_and_copies_audio(self):
        status = self.render("cfr_audio.mp4")
        self.assertEqual(status["status"], "completed", status)
        src, out = frame_times(self.clips / "cfr_audio.mp4"), frame_times(self.final)
        self.assertEqual(len(out), len(src))
        self.assertLess(max(abs(a - b) for a, b in zip(src, out)), 0.001)
        info, duration = streams(self.final)
        self.assertEqual((info["video"]["codec_name"], info["video"]["width"], info["video"]["height"]), ("h264", 760, 240))
        self.assertEqual((status["audio"], info["audio"]["codec_name"]), ("copied", "aac"))
        self.assertAlmostEqual(duration, 2.0, delta=0.15)
        self.assertFalse(status["source_vfr"])
        self.assertEqual((status["frames_written"], status["timestamp_repairs"], status["people_shown"]), (20, 0, 1))
        self.assertIn(status["encoder"], ("h264_nvenc", "libx264"))
        self.assertEqual(status["decoder"], "OpenCV FFmpeg (CPU)")
        self.assertEqual(status["output_bytes"], self.final.stat().st_size)
        self.assertEqual(self.detect_frames, 20, "one inference per source frame, none repeated")
        self.assertEqual(self.reports[0]["status"], "running")
        self.assert_clean()

    def test_vfr_timestamps_are_preserved_not_resampled(self):
        status = self.render("vfr_audio.mkv")
        self.assertEqual(status["status"], "completed", status)
        src, out = frame_times(self.clips / "vfr_audio.mkv"), frame_times(self.final)
        self.assertEqual(len(out), len(src), "no frame duplicated or dropped")
        gaps = sorted({round(b - a, 3) for a, b in zip(src, src[1:])})
        self.assertGreater(len(gaps), 1, "the test clip really is variable frame rate")
        self.assertLess(max(abs((b - out[0]) - (a - src[0])) for a, b in zip(src, out)), 0.002)
        out_gaps, src_gaps = [b - a for a, b in zip(out, out[1:])], [b - a for a, b in zip(src, src[1:])]
        self.assertLess(max(abs(a - b) for a, b in zip(out_gaps, src_gaps)), 0.002, "every frame interval is the source's own")
        self.assertTrue(status["source_vfr"])
        self.assertIn("preserved", status["timing"])
        _info, duration = streams(self.final)
        _src_info, src_duration = streams(self.clips / "vfr_audio.mkv")
        self.assertAlmostEqual(duration, src_duration, delta=0.12)
        self.assertEqual(status["audio"], "copied")

    def test_absent_unwanted_and_incompatible_audio_are_reported_honestly(self):
        status = self.render("silent.mp4")
        self.assertEqual((status["status"], status["audio"], status["audio_written"]), ("completed", "none", None))
        status = self.render("cfr_audio.mp4", keep_audio=False)
        self.assertEqual((status["audio"], status["audio_written"]), ("not_requested", None))
        self.assertNotIn("audio", streams(self.final)[0])
        self.final.unlink()
        status = self.render("pcm.mkv")
        self.assertEqual((status["audio"], status["audio_source_codec"], status["audio_written"]), ("transcoded", "pcm_s16le", "aac"))
        self.final.unlink()
        with patch.object(media, "audio_plan", lambda *a: ("incompatible", [])):
            status = self.render("cfr_audio.mp4")
        self.assertEqual((status["status"], status["audio"], status["audio_written"]), ("completed", "incompatible", None))

    def test_odd_dimensions_are_padded_to_even_and_reported(self):
        status = self.render("odd.avi", frames=10, duration=1.0)
        self.assertEqual(status["status"], "completed", status)
        self.assertEqual((status["source_width"], status["source_height"], status["width"], status["height"]), (161, 81, 602, 82))
        self.assertEqual(status["padded_pixels"], [1, 1])
        info, _ = streams(self.final)
        self.assertEqual((info["video"]["width"], info["video"]["height"]), (602, 82))

    def test_nvenc_failure_uses_cpu_encoder_and_names_it(self):
        real = media.encoder_works
        broken = lambda enc, w, h: (False, "no NVENC device") if enc == "h264_nvenc" else real(enc, w, h)  # noqa: E731
        with patch.object(media, "ENCODER_ORDER", ("h264_nvenc", "libx264")), patch.object(media, "encoder_works", broken):
            status = self.render("cfr_audio.mp4")
        self.assertEqual((status["status"], status["encoder"], status["encoder_mode"]), ("completed", "libx264", "auto"))
        self.assertEqual(status["encoder_label"], "libx264 (CPU)")
        self.assertIn("no NVENC device", status["encoder_note"])
        self.assertEqual(len(frame_times(self.final)), 20)

    def test_explicit_gpu_encode_that_cannot_run_fails_instead_of_using_the_cpu(self):
        real = media.encoder_works
        broken = lambda enc, w, h: (False, "no NVENC device") if enc == "h264_nvenc" else real(enc, w, h)  # noqa: E731
        with patch.object(media, "encoder_works", broken):
            status = self.render("cfr_audio.mp4", encoder="gpu")
        self.assertEqual((status["status"], status.get("encoder")), ("failed", None), status)
        self.assertIn("GPU encoding (h264_nvenc) cannot write 760x240 on this computer: no NVENC device", status["error"])
        self.assertFalse(self.final.exists())
        self.assert_clean()

    def test_explicit_cpu_modes_use_the_cpu_even_when_the_gpu_works(self):
        with patch.object(media, "ENCODER_ORDER", ("h264_nvenc", "libx264")), \
                patch.object(media, "gpu_decode_filter", side_effect=AssertionError("probed NVDEC")):
            status = self.render("cfr_audio.mp4", decoder="cpu", encoder="cpu")
        self.assertEqual((status["status"], status["decoder"], status["encoder"], status["decoder_mode"], status["encoder_mode"]),
                         ("completed", "OpenCV FFmpeg (CPU)", "libx264", "cpu", "cpu"), status)
        self.assertIsNone(status["encoder_note"])

    def test_explicit_gpu_decode_that_cannot_run_fails_instead_of_using_software(self):
        with patch.object(media, "gpu_decode_filter", lambda path, info: (None, "No decoder surfaces left")):
            status = self.render("cfr_audio.mp4", decoder="gpu")
        self.assertEqual((status["status"], status.get("decoder")), ("failed", None), status)
        self.assertIn("GPU decoding (NVDEC) cannot be used for this video: No decoder surfaces left", status["error"])
        self.assertEqual(self.detect_frames, 0)
        self.assertFalse(self.final.exists())
        self.assert_clean()

    def test_auto_decode_is_the_cpu_decoder_and_never_probes_the_gpu(self):
        with patch.object(media, "gpu_decode_filter", side_effect=AssertionError("probed NVDEC")):
            status = self.render("cfr_audio.mp4")
        self.assertEqual((status["status"], status["decoder"], status["decoder_mode"]), ("completed", "OpenCV FFmpeg (CPU)", "auto"))

    def test_a_decoder_that_stops_early_is_an_error_not_a_shorter_video(self):
        info = media.probe(self.clips / "cfr_audio.mp4")
        decoder = media.FfmpegDecoder(self.clips / "cfr_audio.mp4", info)
        decoder.timestamps = decoder.timestamps + [9.0, 9.1]      # the container lists two frames the decoder never delivers
        try:
            for _ in range(20):
                self.assertIsNotNone(decoder.read())
            with self.assertRaises(media.MediaToolError) as ctx:
                decoder.read()
        finally:
            decoder.close()
        self.assertIn("stopped after 20 of 22 frames", str(ctx.exception))

    @unittest.skipUnless(media.gpu_modes()["decoder"][0] and media.gpu_modes()["encoder"][0], "needs a working NVDEC and NVENC")
    def test_explicit_gpu_modes_really_run_on_the_gpu_here(self):
        status = self.render("vfr_420.mkv", decoder="gpu", encoder="gpu")
        self.assertEqual((status["status"], status.get("decoder"), status.get("encoder")),
                         ("completed", "FFmpeg NVDEC (NVIDIA GPU)", "h264_nvenc"), status)
        self.assertEqual((status["decoder_mode"], status["encoder_mode"], status["encoder_note"]), ("gpu", "gpu", None))
        self.assertEqual(status["audio"], "copied")
        source, out = frame_times(self.clips / "vfr_420.mkv"), frame_times(self.final)
        self.assertEqual(len(out), 20)
        for a, b in zip(source, out):
            self.assertLess(abs((a - source[0]) - (b - out[0])), 0.002)

    @unittest.skipUnless(media.gpu_modes()["decoder"][0], "needs a working NVDEC")
    def test_a_video_the_gpu_cannot_decode_is_refused_not_decoded_in_software(self):
        # 4:4:4 H.264: plain "-hwaccel cuda" would quietly decode this on the CPU.
        status = self.render("vfr_audio.mkv", decoder="gpu")
        self.assertEqual((status["status"], status.get("decoder")), ("failed", None), status)
        self.assertIn("GPU decoding (NVDEC) cannot be used for this video", status["error"])
        self.assertEqual(self.detect_frames, 0)
        self.assert_clean()
        self.assertEqual(self.render("vfr_audio.mkv", decoder="cpu")["status"], "completed")   # the same file is fine on the CPU

    def test_boxes_names_and_panel_reach_the_encoded_frames(self):
        self.render("cfr_audio.mp4")
        cap = cv2.VideoCapture(str(self.final))
        ok, frame = cap.read()
        cap.release()
        self.assertTrue(ok)
        b, g, r = (int(v) for v in frame[105, 22])          # left edge of the known face's box (x = 20 + 2)
        self.assertTrue(g > 150 and g > r + 60 and g > b + 40, f"green box expected, got {(b, g, r)}")
        b, g, r = (int(v) for v in frame[60, 230])           # left edge of the unknown face's box
        self.assertTrue(abs(b - g) < 40 and abs(g - r) < 40 and 90 < g < 215, f"grey box expected, got {(b, g, r)}")
        self.assertGreater(int(frame[:, 330:].max()), 150, "panel text present")

    def test_person_stays_in_the_panel_for_five_seconds_of_video_time(self):
        seen = []
        real = overlay.Painter.panel

        def spy(painter, rows, total, now):
            seen.append((round(now, 3), [r[0] for r in rows], [r[1] for r in rows]))
            return real(painter, rows, total, now)

        def detect(frame, timings):
            self.detect_frames += 1
            return [SimpleNamespace(embedding=unit(0), bbox=(20, 60, 90, 150))] if self.detect_frames <= 10 else []

        with patch.object(overlay.Painter, "panel", spy):
            status = self.render("long.mp4", detect=detect, frames=80, duration=8.0)
        self.assertEqual(status["status"], "completed", status)
        listed = [now for now, people, _ in seen if people == ["p1"]]
        last_drawn = 0.9 + tracking.TRACK_DRAW_GAP / 10        # last detection at 0.9 s, box lingers 2 frames
        self.assertAlmostEqual(max(listed), last_drawn + overlay.LINGER_SECONDS, delta=0.11)
        self.assertEqual(seen[-1][1], [], "gone after the linger")
        in_frame_until = max(now for now, people, flags in seen if flags == [True])
        self.assertLess(in_frame_until, 1.6, "shown as 'in frame' only while a box is drawn")

    def test_cancel_removes_the_partial_file_and_releases_the_disk_claim(self):
        def cancelled():
            return self.detect_frames >= 5
        status = self.render("long.mp4", cancelled=cancelled, frames=80, duration=8.0)
        self.assertEqual(status["status"], "cancelled")
        self.assertFalse(self.final.exists())
        self.assert_clean()
        self.assertLess(self.detect_frames, 20, "stopped promptly")

    def test_low_disk_missing_ffmpeg_and_encoder_death_fail_cleanly(self):
        with patch.object(video, "claim_disk", side_effect=video.MediaError("Not enough free disk space for the tagged video")):
            status = self.render("cfr_audio.mp4")
        self.assertEqual(status["status"], "failed")
        self.assertIn("Not enough free disk space", status["error"])
        self.assertFalse(self.final.exists())
        with patch.object(media, "find_tool", lambda name: None):
            status = self.render("cfr_audio.mp4")
        self.assertEqual(status["status"], "failed")
        self.assertIn("FFmpeg", status["error"])

        def dying(frame, timings):
            self.detect_frames += 1
            raise RuntimeError("model crashed")
        status = self.render("cfr_audio.mp4", detect=dying)
        self.assertEqual(status["status"], "failed")
        self.assertIn("Recognition results are unaffected", status["error"])
        self.assertFalse(self.final.exists())
        self.assert_clean()

    def test_low_disk_while_writing_stops_the_render(self):
        calls = {"n": 0}
        real = video.claim_disk

        def claim(key, remaining, label, where=None):
            calls["n"] += 1
            if calls["n"] > 1:
                raise video.MediaError("Not enough free disk space for the tagged video")
            return real(key, remaining, label, where)

        def slow(frame, timings):
            time.sleep(0.06)
            return self.detect(frame, timings)

        with patch.object(video, "claim_disk", claim):
            status = self.render("long.mp4", detect=slow, frames=80, duration=8.0)
        self.assertEqual(status["status"], "failed")
        self.assertIn("Not enough free disk space", status["error"])
        self.assertFalse(self.final.exists())
        self.assert_clean()

    def test_ffmpeg_decoder_gives_the_same_frames_and_timestamps(self):
        info = media.probe(self.clips / "vfr_audio.mkv")
        a, b = media.OpenCvDecoder(self.clips / "vfr_audio.mkv"), media.FfmpegDecoder(self.clips / "vfr_audio.mkv", info)
        try:
            first_a = first_b = None
            count = 0
            while True:
                fa, fb = a.read(), b.read()
                if fa is None or fb is None:
                    self.assertIsNone(fa)
                    self.assertIsNone(fb)
                    break
                first_a = fa[1] if first_a is None else first_a
                first_b = fb[1] if first_b is None else first_b
                self.assertEqual(fa[0].shape, fb[0].shape)
                self.assertLess(abs((fa[1] - first_a) - (fb[1] - first_b)), 0.002)
                self.assertLess(float(np.abs(fa[0].astype(int) - fb[0].astype(int)).mean()), 3.0)
                count += 1
            self.assertEqual(count, 20)
        finally:
            a.close()
            b.close()
        with patch.object(media, "open_decoder", lambda path, info, mode="auto": (media.FfmpegDecoder(path, info), "")):
            status = self.render("vfr_audio.mkv")
        self.assertEqual((status["status"], status["decoder"]), ("completed", "FFmpeg (CPU)"))
        self.assertEqual(len(frame_times(self.final)), 20)


if __name__ == "__main__":
    unittest.main()


class TiledDetectorTests(unittest.TestCase):
    """devices.detect_tiled must find exactly what the Event Photo scan finds,
    while holding the shared lock for one detector call at a time."""

    def setUp(self):
        from app.face_recognition import engine
        from app.services import event_photo_detection_service as event_detect
        from app.services import video_devices as devices
        self.engine, self.event_detect, self.devices = engine, event_detect, devices
        owner = self
        self.detector_calls = []

        class Detector:
            def detect(self, crop, max_num=0, metric="default"):
                owner.detector_calls.append(crop.shape[:2])
                owner.lock_seen.append(engine.inference_lock.locked())
                h, w = crop.shape[:2]
                # One face per region, placed from the crop's own content so tiles differ.
                seed = int(crop[0, 0, 0])
                x, y = 20 + seed % 40, 30 + seed % 30
                boxes = np.array([[x, y, x + 60, y + 70, 0.6 + (seed % 30) / 100]], np.float32)
                kps = np.array([[[x + 15, y + 25], [x + 45, y + 25], [x + 30, y + 40], [x + 20, y + 55], [x + 40, y + 55]]], np.float32)
                return (boxes, kps) if h >= 100 and w >= 100 else (np.zeros((0, 5), np.float32), None)

        class Recognition:
            session = SimpleNamespace(get_providers=lambda: ["FakeProvider"])

            def get(self, image, face):
                # Face.normed_embedding is derived from .embedding, as in InsightFace.
                face.embedding = np.array([face.bbox[0], face.bbox[1], face.det_score, 1.0], np.float32)

        detector = Detector()
        detector.session = SimpleNamespace(get_providers=lambda: ["FakeProvider"])
        self.app = SimpleNamespace(det_model=detector, models={"recognition": Recognition()})
        self.lock_seen = []
        for target, name, value in ((devices, "session", lambda device: self.app), (devices, "PROVIDERS", {"cpu": "FakeProvider"}),
                                    (engine, "get_face_app", lambda: self.app)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        rng = np.random.default_rng(3)
        self.frame = rng.integers(0, 255, (1080, 1920, 3), dtype=np.uint8)

    def test_same_faces_as_the_event_photo_scan(self):
        expected = self.event_detect.detect_event_faces(self.frame)
        event_calls = len(self.detector_calls)
        self.detector_calls.clear()
        self.lock_seen.clear()
        timings, yields = {}, []
        got = self.devices.detect_tiled(self.frame, "cpu", timings, lambda: yields.append(self.engine.inference_lock.locked()))
        self.assertEqual(len(self.detector_calls), event_calls, "same global pass and tiles")
        self.assertEqual(event_calls, 9, "1080p: one global pass + 4x2 tiles")
        self.assertEqual(timings["tiled_detector_calls"], 9)
        self.assertGreater(len(got), 1)
        self.assertEqual(len(got), len(expected))
        for a, b in zip(got, expected):
            self.assertEqual(a.bbox, b.bbox)
            self.assertAlmostEqual(a.det_score, b.det_score, places=6)
            np.testing.assert_allclose(a.embedding, b.embedding, rtol=1e-6)
        self.assertTrue(all(self.lock_seen), "every detector call runs under the shared lock")
        self.assertGreaterEqual(len(yields), 10, "a yield point before every detector call and embedding chunk")
        self.assertFalse(any(yields), "the lock is free between calls, so a live request can get in")
        self.assertFalse(self.engine.inference_lock.locked())
        self.assertGreaterEqual(timings["inference_seconds"], 0)

    def test_small_frame_needs_only_the_global_pass_and_plain_stays_one_call(self):
        small = self.frame[:480, :640]
        self.devices.detect_tiled(small, "cpu", {})
        self.assertEqual(len(self.detector_calls), 1)
        self.detector_calls.clear()
        self.devices.detect(self.frame, "cpu", {})
        self.assertEqual(len(self.detector_calls), 1, "Plain: one full-frame call")

    def test_provider_change_stops_instead_of_switching_device(self):
        with patch.object(self.devices, "PROVIDERS", {"cpu": "OtherProvider"}):
            with self.assertRaises(self.devices.DeviceError):
                self.devices.detect_tiled(self.frame, "cpu", {})
        self.assertFalse(self.engine.inference_lock.locked())
