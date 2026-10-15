"""Tagged-video render inside the real video scheduler: mocked Drive, synthetic
clips, fake detector/matcher, real ffmpeg. No real Drive, model or footage."""
import copy
import csv
import hashlib
import io
import json
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_drive_video_batch as fixture
import test_local_video_experiment as local_fixture
from test_drive_video_batch import ACCOUNT, PARENT
from app.auth.deps import get_current_user
from app.models.models import User
from app.services import drive_video_batch as batch, drive_video_source as drive
from app.services import local_video_experiment as video
from app.services import video_devices as devices, video_render as render, video_scheduler as scheduler
from app.services import video_render_media as media
from app.services.scan_settings import ScanSettings

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
NAMES = {"person-a": ("สมชาย ใจดี", "0001"), "person-b": ("Synthetic Beta", "0002")}
RENDER = {"enabled": True, "keep_audio": True, "detector": "plain"}
COUNT_FIELDS = ("sampled_frames", "decoded_frames", "unknown_detections", "frames_with_unknown", "matched_face_detections")
CSV_COUNT_COLUMNS = ("record_type", "identity_key", "name", "detection_count", "first_timestamp_seconds", "last_timestamp_seconds",
                     "source_video_key", "source_filename", "status", "decoded_frame_count", "sampled_frame_count",
                     "detected_face_detections", "matched_face_detections", "unknown_face_detections")


def wait_idle(test, *job_ids, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        states = [video.get(j) for j in job_ids]
        if all(s["status"] not in video.ACTIVE for s in states) and not scheduler._workers:
            return states
        time.sleep(0.02)
    test.fail("video workers did not finish")


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class DriveBatchRenderTests(unittest.TestCase):
    for _name, _method in fixture.DriveBatchTests.__dict__.items():
        if callable(_method) and not _name.startswith("test_"):
            locals()[_name] = _method

    def submit(self, render_settings=None, keys=None, name="Render seven"):
        self.patch(scheduler, "names_snapshot", lambda job_id: dict(NAMES))
        return batch.create(name, PARENT, keys or list(self.items), ACCOUNT["account_id"], ScanSettings(), "auto", render_settings)

    def counts(self, state):
        return ({f: state[f] for f in COUNT_FIELDS}, [(p["person_id"], p["detection_count"]) for p in state["people"]],
                [(s["source_video_key"], s["status"], {f: s[f] for f in COUNT_FIELDS}) for s in state["videos"]])

    def csv_counts(self, state, job_ids):
        rows = list(csv.DictReader(io.StringIO(video.to_csv(state))))
        out = [tuple(r[c] for c in CSV_COUNT_COLUMNS) for r in rows if r["record_type"] != "metadata"]
        for job_id in job_ids:  # identity keys are derived from the experiment id
            out = [tuple(v.replace(job_id, "") for v in row) for row in out]
        return [row[:1] + row[2:] for row in out], [r["key"] for r in rows if r["record_type"] == "metadata"]

    def test_seven_sources_get_seven_separate_videos_and_recognition_is_unchanged(self):
        plain = wait_idle(self, self.submit(None, name="No render")["id"])[0]
        tagged = wait_idle(self, self.submit(RENDER, name="No render")["id"])[0]
        self.assertEqual((plain["status"], tagged["status"]), ("completed", "completed"))
        self.assertEqual(self.counts(tagged), self.counts(plain), "rendering adds nothing to any recognition count")
        self.assertEqual(self.csv_counts(tagged, [tagged["id"]]), self.csv_counts(plain, [plain["id"]]), "same CSV rows and columns")
        self.assertNotIn("render", plain["videos"][0])
        self.assertNotIn("render_settings", plain)

        digests = set()
        for i, source in enumerate(tagged["videos"], start=1):
            record = source["render"]
            self.assertEqual((source["source_video_key"], record["status"]), (f"video-{i:03d}", "completed"), record)
            path = render.output_path(tagged["id"], source["source_video_key"])
            self.assertTrue(path.is_file())
            self.assertEqual(record["output_bytes"], path.stat().st_size)
            self.assertEqual((record["frames_written"], record["detector"], record["width"], record["height"]), (6, "plain", 600, 80))
            self.assertIn(record["encoder"], ("h264_nvenc", "libx264"))
            self.assertEqual(record["audio"], "none")
            self.assertLessEqual(source["finished_at"], record["finished_at"])
            digests.add(hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(len(digests), 7, "seven different files, none overwritten")
        self.assertEqual(sorted(p.name for p in (video.directory(tagged["id"]) / "renders").iterdir()),
                         [f"video-{i:03d}.mp4" for i in range(1, 8)])
        self.clean(tagged)  # every temporary Drive download is gone
        self.assertEqual(video.claimed_disk_bytes(), 0)

        status, body, headers = self.call(f"/{tagged['id']}/sources/video-003/render.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(body, render.output_path(tagged["id"], "video-003").read_bytes())
        self.assertIn(b"cam03_tagged.mp4", headers[b"content-disposition"])
        self.assertEqual(headers[b"content-type"], b"video/mp4")
        self.assertEqual(self.call(f"/{tagged['id']}/sources/video-009/render.mp4")[0], 404)
        self.assertEqual(self.call(f"/{plain['id']}/sources/video-001/render.mp4")[0], 404)
        public = json.loads(self.call(f"/{tagged['id']}")[1])
        self.assertEqual([v["render"]["status"] for v in public["videos"]], ["completed"] * 7)
        self.assertNotIn("source_file", public["videos"][0])

    def test_delete_one_output_leaves_the_others_and_the_results(self):
        state = wait_idle(self, self.submit(RENDER, keys=list(self.items)[:2])["id"])[0]
        before = self.counts(state)
        self.assertEqual(self.call(f"/{state['id']}/sources/video-001/render", "DELETE")[0], 200)
        after = video.get(state["id"])
        self.assertEqual((after["videos"][0]["render"]["status"], after["videos"][1]["render"]["status"]), ("deleted", "completed"))
        self.assertFalse(render.output_path(state["id"], "video-001").exists())
        self.assertTrue(render.output_path(state["id"], "video-002").is_file())
        self.assertEqual(self.call(f"/{state['id']}/sources/video-001/render.mp4")[0], 409)
        self.assertEqual(self.counts(after), before)
        # A Drive source cannot be rendered later: its download no longer exists.
        status, body, _ = self.call(f"/{state['id']}/render", "POST", {"detector": "plain"})
        self.assertEqual(status, 409)
        self.assertIn(b"before starting a Drive batch", body)
        self.assertEqual(self.call(f"/{state['id']}", "DELETE")[0], 200)
        self.assertFalse(video.directory(state["id"]).exists(), "experiment Delete removes the tagged videos too")

    def test_detector_mode_is_frozen_per_batch_and_only_the_render_pass_uses_it(self):
        calls = {"plain": 0, "tiled": 0}

        def plain(image, device, timings):
            calls["plain"] += 1
            return video.detect(image)

        def tiled(image, device, timings, before_call=None):
            calls["tiled"] += 1
            self.assertIsNotNone(before_call, "tiled yields to live requests between its detector calls")
            return video.detect(image)

        self.patch(devices, "detect", plain)
        self.patch(devices, "detect_tiled", tiled)
        key = list(self.items)[:1]
        state = wait_idle(self, self.submit({**RENDER, "detector": "tiled"}, keys=key)["id"])[0]
        self.assertEqual(state["videos"][0]["render"]["detector"], "tiled")
        self.assertEqual(calls["tiled"], 6, "every frame of the render pass")
        recognition_calls = calls["plain"]
        self.assertEqual(recognition_calls, state["videos"][0]["sampled_frames"], "recognition still uses the experiment's own detector")
        calls.update(plain=0, tiled=0)
        state = wait_idle(self, self.submit(RENDER, keys=key)["id"])[0]
        self.assertEqual((calls["tiled"], calls["plain"]), (0, recognition_calls + 6))
        self.assertEqual(state["render_settings"], RENDER)
        with self.assertRaises(Exception):
            render.RenderSettings(detector="huge")

    def test_render_failure_keeps_completed_recognition(self):
        def broken(**kwargs):
            kwargs["report"]({"status": "failed", "error": "ffmpeg failed while encoding", "finished_at": video.now()})
            return {"status": "failed"}

        with patch.object(render, "render_source", broken):
            state = wait_idle(self, self.submit(RENDER, keys=list(self.items)[:2])["id"])[0]
        self.assertEqual(state["status"], "completed")
        self.assertEqual([s["status"] for s in state["videos"]], ["completed", "completed"])
        self.assertEqual([s["render"]["status"] for s in state["videos"]], ["failed", "failed"])
        self.assertEqual(state["sampled_frames"], 6)
        self.assertEqual(self.call(f"/{state['id']}/sources/video-001/render.mp4")[0], 409)
        self.clean(state)

        def crash(**kwargs):
            raise RuntimeError("unexpected")

        with patch.object(render, "render_source", crash):
            state = wait_idle(self, self.submit(RENDER, keys=list(self.items)[:1])["id"])[0]
        self.assertEqual((state["status"], state["videos"][0]["status"], state["videos"][0]["render"]["status"]),
                         ("completed", "completed", "failed"))
        self.assertIn("recognition results are unaffected", state["videos"][0]["render"]["error"])
        self.clean(state)

    def slow_render_detect(self, entered, delay=0.08):
        def detect(image, device, timings):
            if "rendering" in scheduler._stages.values():
                entered.set()
                time.sleep(delay)
            return video.detect(image)
        self.patch(devices, "detect", detect)

    def test_cancelling_one_render_keeps_recognition_and_removes_the_download(self):
        entered = threading.Event()
        self.slow_render_detect(entered)
        submitted = self.submit(RENDER, keys=list(self.items)[:2])
        self.assertTrue(entered.wait(20))
        live = video.get(submitted["id"])
        self.assertEqual(live["status"], "running", "the batch stays active while a tagged video is being written")
        self.assertEqual(self.call(f"/{submitted['id']}", "DELETE")[0], 409)
        self.assertEqual(self.call(f"/{submitted['id']}/sources/video-001/render/cancel", "POST")[0], 200)
        state = wait_idle(self, submitted["id"])[0]
        first, second = state["videos"]
        self.assertEqual((first["status"], first["render"]["status"]), ("completed", "cancelled"))
        self.assertEqual((second["status"], second["render"]["status"]), ("completed", "completed"), "other sources are not affected")
        self.assertEqual(state["status"], "completed")
        self.assertFalse(render.output_path(state["id"], "video-001").exists())
        self.assertEqual(list(video.directory(state["id"]).rglob("*.partial.mp4")), [])
        self.clean(state)

    def test_cancelling_the_batch_during_render_stops_it_and_later_sources_are_not_rendered(self):
        entered = threading.Event()
        self.slow_render_detect(entered)
        submitted = self.submit(RENDER, keys=list(self.items)[:3])
        self.assertTrue(entered.wait(20))
        video.cancel(submitted["id"])
        state = wait_idle(self, submitted["id"])[0]
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual((state["videos"][0]["status"], state["videos"][0]["render"]["status"]), ("completed", "cancelled"))
        for source in state["videos"][1:]:
            self.assertEqual((source["status"], source["render"]["status"]), ("not_processed", "not_rendered"))
        self.assertEqual(state["sampled_frames"], state["videos"][0]["sampled_frames"])
        self.assertEqual(list(video.directory(state["id"]).rglob("*.mp4")), [])
        self.clean(state)

    def test_restart_removes_partials_marks_running_render_interrupted_and_keeps_pending_ones_queued(self):
        with patch.object(scheduler, "launch", lambda: None):
            state = self.submit(RENDER, keys=list(self.items)[:3])
        state["status"] = "running"
        state["videos"][0].update(status="completed", sampled_frames=3)
        state["videos"][0]["render"].update(status="running")
        state["videos"][1].update(status="running", sampled_frames=1)
        video._write(state)
        renders = video.directory(state["id"]) / "renders"
        renders.mkdir()
        (renders / "video-001.partial.mp4").write_bytes(b"partial")
        scheduler._indices.clear()
        with patch.object(scheduler, "launch", lambda: None):
            video._initialized = False
            video.initialize()
        recovered = video.get(state["id"])
        self.assertEqual(recovered["status"], "queued", "untouched pending sources continue")
        first, second, third = recovered["videos"]
        self.assertEqual((first["status"], first["render"]["status"]), ("completed", "interrupted"))
        self.assertIn("Recognition results are unaffected", first["render"]["error"])
        self.assertEqual((second["status"], second["render"]["status"]), ("interrupted", "not_rendered"))
        self.assertEqual((third["status"], third["render"]["status"]), ("pending", "queued"))
        self.assertFalse((renders / "video-001.partial.mp4").exists())
        self.assertEqual(first["sampled_frames"], 3, "completed recognition is never replayed")
        with patch.object(scheduler, "load_index", lambda job_id: self.matcher()):
            scheduler.launch()
            final = wait_idle(self, state["id"])[0]
        self.assertEqual((final["videos"][2]["status"], final["videos"][2]["render"]["status"]), ("completed", "completed"))
        self.assertTrue(render.output_path(state["id"], "video-003").is_file())

    def test_batch_request_accepts_render_and_capabilities_describe_it(self):
        status, body, _ = self.call("/render/capabilities")
        self.assertEqual(status, 200)
        caps = json.loads(body)
        self.assertEqual([d["value"] for d in caps["detectors"]], ["plain", "tiled"])
        self.assertEqual((caps["default_detector"], caps["available"], caps["panel_width"]), ("plain", True, 440))
        self.assertIn("GPU", caps["detectors"][1]["label"])
        self.patch(scheduler, "names_snapshot", lambda job_id: dict(NAMES))
        request = dict(name="API batch", folder_link=f"https://drive.google.com/drive/folders/{PARENT}", file_ids=list(self.items)[:1],
                       account_id=ACCOUNT["account_id"], render={"enabled": True, "detector": "tiled", "keep_audio": False})
        status, body, _ = self.call("/drive/batches", "POST", request)
        self.assertEqual(status, 201, body)
        state = wait_idle(self, json.loads(body)["id"])[0]
        self.assertEqual((state["videos"][0]["render"]["status"], state["videos"][0]["render"]["detector"],
                          state["videos"][0]["render"]["audio"]), ("completed", "tiled", "not_requested"))
        request["render"]["detector"] = "enormous"
        self.assertEqual(self.call("/drive/batches", "POST", request)[0], 422)

    def test_capabilities_list_the_three_modes_for_decoder_and_encoder(self):
        gpu = {"decoder": (True, ""), "encoder": (False, "No capable devices found")}
        with patch.object(media, "gpu_modes", lambda: gpu):
            caps = json.loads(self.call("/render/capabilities")[1])
        for key in ("decoder_modes", "encoder_modes"):
            self.assertEqual([m["value"] for m in caps[key]], ["auto", "gpu", "cpu"])
            self.assertTrue(caps[key][0]["available"] and caps[key][2]["available"])
        self.assertEqual((caps["decoder_modes"][1]["available"], caps["decoder_modes"][1]["detail"]), (True, None))
        self.assertEqual((caps["encoder_modes"][1]["available"], caps["encoder_modes"][1]["detail"]), (False, "No capable devices found"))
        self.assertIn("never falls back to the CPU", caps["mode_note"])
        self.assertIn(media.ENCODER_LABELS[media.ENCODER_ORDER[0]] + " first", caps["encoder_modes"][0]["detail"])

    def test_batch_with_an_explicit_gpu_mode_this_computer_lacks_is_refused_before_anything_is_created(self):
        request = dict(name="GPU batch", folder_link=f"https://drive.google.com/drive/folders/{PARENT}", file_ids=list(self.items)[:1],
                       account_id=ACCOUNT["account_id"], render={"enabled": True, "decoder": "gpu", "encoder": "auto"})
        before = len(video.list_jobs())
        with patch.object(media, "gpu_modes", lambda: {"decoder": (False, "No NVDEC capable devices found"), "encoder": (True, "")}):
            status, body, _ = self.call("/drive/batches", "POST", request)
        self.assertEqual(status, 400, body)
        self.assertIn("GPU decoding (NVDEC) is not available on this computer", json.loads(body)["detail"])
        self.assertEqual([s["status"] for s in video.list_jobs() if s["status"] in video.ACTIVE], [])
        self.assertEqual(len([s for s in video.list_jobs() if s.get("render_settings")]), 0)
        self.assertLessEqual(len(video.list_jobs()) - before, 1)   # at most the refused, never-started shell
        request["render"]["decoder"] = "hardware"
        self.assertEqual(self.call("/drive/batches", "POST", request)[0], 422)

    def test_modes_are_frozen_per_batch_and_reported_per_source(self):
        self.patch(media, "gpu_modes", lambda: {"decoder": (True, ""), "encoder": (True, "")})
        state = wait_idle(self, self.submit({**RENDER, "decoder": "cpu", "encoder": "cpu"}, keys=list(self.items)[:2])["id"])[0]
        self.assertEqual((state["render_settings"]["decoder"], state["render_settings"]["encoder"]), ("cpu", "cpu"))
        for source in state["videos"]:
            record = source["render"]
            self.assertEqual((record["status"], record["decoder_mode"], record["encoder_mode"], record["decoder"], record["encoder"]),
                             ("completed", "cpu", "cpu", "OpenCV FFmpeg (CPU)", "libx264"), record)

    def test_a_gpu_mode_one_video_cannot_use_fails_that_tagged_video_only(self):
        self.patch(media, "gpu_modes", lambda: {"decoder": (True, ""), "encoder": (True, "")})
        real = media.encoder_works
        self.patch(media, "encoder_works", lambda enc, w, h: (False, "Width 4280 exceeds 4096") if enc == "h264_nvenc" else real(enc, w, h))
        before = self.counts(wait_idle(self, self.submit(None, keys=list(self.items)[:1], name="plain")["id"])[0])
        state = wait_idle(self, self.submit({**RENDER, "encoder": "gpu"}, keys=list(self.items)[:1], name="gpu")["id"])[0]
        record = state["videos"][0]["render"]
        self.assertEqual((state["status"], state["videos"][0]["status"], record["status"], record["encoder_mode"]),
                         ("completed", "completed", "failed", "gpu"), record)
        self.assertIn("GPU encoding (h264_nvenc) cannot write", record["error"])
        self.assertIn("Width 4280 exceeds 4096", record["error"])
        self.assertIsNone(record.get("encoder"))                     # nothing was encoded on the CPU instead
        self.assertFalse(render.output_path(state["id"], "video-001").exists())
        self.assertEqual(self.counts(state)[0], before[0])           # recognition is what it would have been anyway
        self.assertEqual(list(batch.downloads_dir(state["id"]).rglob("*.*")), [])   # the temporary download is still removed

    def test_only_admins_reach_render_routes(self):
        state = wait_idle(self, self.submit(RENDER, keys=list(self.items)[:1])["id"])[0]
        self.app.dependency_overrides[get_current_user] = lambda: User(username="viewer", password_hash="x", role="station")
        for path, method in ((f"/{state['id']}/sources/video-001/render.mp4", "GET"), (f"/{state['id']}/sources/video-001/render", "DELETE"),
                             (f"/{state['id']}/sources/video-001/render/cancel", "POST"), (f"/{state['id']}/render", "POST"),
                             ("/render/capabilities", "GET")):
            self.assertEqual(self.call(path, method, {} if method == "POST" else None)[0], 403, path)
        self.assertTrue(render.output_path(state["id"], "video-001").is_file())


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class LocalUploadRenderTests(unittest.TestCase):
    for _name, _method in local_fixture.VideoCase.__dict__.items():
        if callable(_method) and not _name.startswith("test_"):
            locals()[_name] = _method

    def start(self, state, body):
        status, raw, _ = self.call(f"/{state['id']}/start", "POST", json.dumps(body).encode(), {"Content-Type": "application/json"})
        self.assertEqual(status, 200, raw)
        return wait_idle(self, state["id"])[0]

    def post(self, path, body=None):
        return self.call(path, "POST", json.dumps(body or {}).encode(), {"Content-Type": "application/json"})

    def test_render_chosen_at_start_runs_after_recognition_without_changing_it(self):
        plain = self.start(self.upload(name="a.avi"), {})
        tagged = self.start(self.upload(name="b.avi"), {"render": {"enabled": True, "keep_audio": True, "detector": "plain"}})
        self.assertEqual((plain["status"], tagged["status"]), ("completed", "completed"))
        for field in COUNT_FIELDS + ("people",):
            self.assertEqual(tagged[field], plain[field], field)
        record = tagged["render"]
        self.assertEqual((record["status"], record["frames_written"], record["width"], record["height"]), ("completed", 16, 504, 64))
        self.assertLessEqual(tagged["finished_at"], record["started_at"] if record["started_at"] > tagged["finished_at"] else record["finished_at"])
        self.assertTrue(render.output_path(tagged["id"], "local").is_file())
        rows = lambda s: [r for r in csv.reader(io.StringIO(video.to_csv(s))) if r[1] not in  # noqa: E731
                          ("experiment_id", "filename", "video_sha256", "started_at", "finished_at", "processing_seconds",
                           "processing_throughput_fps", "stage_seconds")]
        self.assertEqual(rows(tagged), rows(plain), "the CSV has no render fields and the same values")
        status, body, headers = self.call(f"/{tagged['id']}/sources/local/render.mp4")
        self.assertEqual((status, body), (200, render.output_path(tagged["id"], "local").read_bytes()))
        self.assertIn(b"b_tagged.mp4", headers[b"content-disposition"])
        self.assertTrue(video.video_path(tagged).is_file(), "the retained upload is untouched")
        self.assertEqual(self.call(f"/{plain['id']}/sources/local/render.mp4")[0], 404)

    def test_retained_upload_can_be_rendered_later_as_a_scheduled_job(self):
        ready = self.upload()
        self.assertEqual(self.post(f"/{ready['id']}/render", {"detector": "plain"})[0], 409)   # never started
        done = self.start(ready, {})
        self.assertNotIn("render", done)
        counts = {f: done[f] for f in COUNT_FIELDS}
        status, raw, _ = self.post(f"/{done['id']}/render", {"detector": "tiled", "keep_audio": False})
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["render"]["status"], "queued")
        state = wait_idle(self, done["id"])[0]
        deadline = time.monotonic() + 30
        while state["render"]["status"] in render.ACTIVE and time.monotonic() < deadline:
            time.sleep(0.02)
            state = video.get(done["id"])
        self.assertEqual((state["render"]["status"], state["render"]["detector"], state["render"]["audio"]),
                         ("completed", "tiled", "not_requested"), state["render"])
        self.assertEqual({f: state[f] for f in COUNT_FIELDS}, counts)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["finished_at"], done["finished_at"], "the run's own timestamps are untouched")
        self.assertEqual(state["processing_seconds"], done["processing_seconds"])
        self.assertEqual(self.post(f"/{done['id']}/render", {})[0], 409)                      # an output already exists
        self.assertEqual(self.call(f"/{done['id']}/sources/local/render", "DELETE")[0], 200)
        self.assertFalse(render.output_path(done["id"], "local").exists())
        self.assertEqual(video.get(done["id"])["render"]["status"], "deleted")
        self.assertEqual(self.post(f"/{done['id']}/render", {})[0], 200)                      # and can be made again
        while scheduler._workers or video.get(done["id"])["render"]["status"] in render.ACTIVE:
            time.sleep(0.02)
        self.assertEqual(video.get(done["id"])["render"]["status"], "completed")

    def test_later_render_can_be_cancelled_before_and_while_it_runs(self):
        done = self.start(self.upload(), {})
        with patch.object(scheduler, "launch", lambda: None), patch.object(scheduler._wake, "set", lambda: None):
            self.assertEqual(self.post(f"/{done['id']}/render", {})[0], 200)
            self.assertEqual(self.post(f"/{done['id']}/sources/local/render/cancel")[0], 200)
        state = video.get(done["id"])
        self.assertEqual(state["render"]["status"], "cancelled")
        entered = threading.Event()

        def slow(image, device, timings):
            entered.set()
            time.sleep(0.05)
            return []

        self.patch(devices, "detect", slow)
        self.assertEqual(self.post(f"/{done['id']}/render", {})[0], 200)
        self.assertTrue(entered.wait(20))
        self.assertEqual(self.call(f"/{done['id']}", "DELETE")[0], 409, "cannot delete while a render is running")
        self.assertEqual(self.post(f"/{done['id']}/sources/local/render/cancel")[0], 200)
        while scheduler._workers:
            time.sleep(0.02)
        state = video.get(done["id"])
        self.assertEqual((state["status"], state["render"]["status"]), ("completed", "cancelled"))
        self.assertEqual(list(video.directory(done["id"]).rglob("*.mp4")), [])
        self.assertEqual(self.call(f"/{done['id']}", "DELETE")[0], 200)

    def test_cancelling_a_queued_run_settles_its_render_and_nothing_is_rendered_later(self):
        state = self.upload()
        with patch.object(scheduler, "launch", lambda: None), patch.object(scheduler._wake, "set", lambda: None):
            status, raw, _ = self.post(f"/{state['id']}/start", {"render": {"enabled": True}})
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(raw)["render"]["status"], "queued")
            self.assertEqual(self.post(f"/{state['id']}/cancel")[0], 200)
        after = video.get(state["id"])
        self.assertEqual((after["status"], after["render"]["status"]), ("cancelled", "not_rendered"))
        scheduler.launch()
        time.sleep(0.6)
        self.assertEqual(scheduler._workers, {})
        self.assertEqual(self.post(f"/{state['id']}/render", {})[0], 409)   # only completed runs render later

    def test_start_rejects_an_unknown_render_option(self):
        state = self.upload()
        self.assertEqual(self.post(f"/{state['id']}/start", {"render": {"enabled": True, "detector": "x"}})[0], 422)
        self.assertEqual(self.post(f"/{state['id']}/start", {"render": {"enabled": True, "speed": 2}})[0], 422)
        self.assertEqual(self.post(f"/{state['id']}/start", {"render": {"enabled": True, "encoder": "nvenc"}})[0], 422)
        self.assertEqual(video.get(state["id"])["status"], "ready")

    def test_explicit_gpu_mode_this_computer_lacks_is_refused_at_start_and_for_a_later_render(self):
        no_gpu = {"decoder": (False, "No NVDEC capable devices found"), "encoder": (False, "No capable devices found")}
        state = self.upload()
        with patch.object(media, "gpu_modes", lambda: no_gpu):
            status, raw, _ = self.post(f"/{state['id']}/start", {"render": {"enabled": True, "encoder": "gpu"}})
            self.assertEqual(status, 400, raw)
            self.assertIn("GPU encoding (h264_nvenc) is not available on this computer: No capable devices found", json.loads(raw)["detail"])
            self.assertEqual(video.get(state["id"])["status"], "ready")          # nothing started, it can still be started
            self.assertNotIn("render", video.get(state["id"]))
            # A render that is switched off is never checked, whatever modes it carries.
            done = self.start(state, {"render": {"enabled": False, "decoder": "gpu", "encoder": "gpu"}})
            self.assertEqual(done["status"], "completed")
            status, raw, _ = self.post(f"/{done['id']}/render", {"decoder": "gpu"})
            self.assertEqual(status, 400, raw)
            self.assertIn("GPU decoding (NVDEC) is not available on this computer", json.loads(raw)["detail"])
            self.assertNotIn("render", video.get(done["id"]))
            self.assertEqual(self.post(f"/{done['id']}/render", {"decoder": "cpu", "encoder": "cpu"})[0], 200)
        state = video.get(done["id"])
        deadline = time.monotonic() + 30
        while (scheduler._workers or state["render"]["status"] in render.ACTIVE) and time.monotonic() < deadline:
            time.sleep(0.02)
            state = video.get(done["id"])
        self.assertEqual((state["render"]["status"], state["render"]["decoder_mode"], state["render"]["encoder_mode"], state["render"]["encoder"]),
                         ("completed", "cpu", "cpu", "libx264"), state["render"])

    def test_modes_chosen_at_start_are_stored_with_the_run(self):
        with patch.object(media, "gpu_modes", side_effect=AssertionError("probed")):   # auto and cpu never probe the GPU
            done = self.start(self.upload(), {"render": {"enabled": True, "decoder": "auto", "encoder": "cpu"}})
        self.assertEqual((done["render"]["status"], done["render"]["decoder_mode"], done["render"]["encoder_mode"]), ("completed", "auto", "cpu"))
        self.assertEqual((done["render"]["decoder"], done["render"]["encoder"]), ("OpenCV FFmpeg (CPU)", "libx264"))
        stored = video.get(done["id"])["render_settings"]
        self.assertEqual((stored["decoder"], stored["encoder"]), ("auto", "cpu"))


if __name__ == "__main__":
    unittest.main()
