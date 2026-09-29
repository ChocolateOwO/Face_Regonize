"""Mocked Drive + synthetic AVI. No real OAuth, Drive, identities or user video."""
import asyncio
import copy
import csv
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from fastapi import FastAPI
from app.api import local_video_drive, local_video_experiment as api
from app.auth.deps import get_current_user
from app.models.models import User
from app.services import local_video_experiment as video, drive_video_batch as batch, drive_video_source as drive
from app.services.scan_settings import ScanSettings
from test_local_video_experiment import request

PARENT = "folder_12345"
ACCOUNT = {"display_name": "Synthetic Drive", "email": "synthetic@example.test", "account_id": "fake-account"}
MODEL = dict(name="Fake", provider="FakeProvider", detector_provider="FakeProvider", threshold=.45,
    detection_threshold=.5, detection_size=[320, 320], enrolled_identities=2)
REAL_VERIFY_EOF = video.verify_eof

class FakeRequest:
    def __init__(self, value): self.value = value
    def execute(self, **_): return copy.deepcopy(self.value)

class DriveBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-drive-video-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch(video, "ROOT", self.root / "experiments")
        self.patch(video, "_initialized", True)
        self.patch(video, "_active", None)
        self.patch(video, "_cancel", threading.Event())
        self.patch(video, "scan_clock", lambda: 0.0)
        self.patch(video, "verify_eof", lambda path, count: dict(outcome="normal_eof", verified_frame_count=count, error=None))
        self.patch(video, "identity_snapshot", lambda: (self.matcher(), copy.deepcopy(MODEL)))
        self.patch(video, "detect", self.faces)
        self.items, self.payloads, self.downloads = {}, {}, []
        self.visible = None
        self.broken = set()
        self.service = self.fake_service()
        self.patch(drive.oauth, "get_video_read_service", lambda: self.service)
        self.patch(drive, "MediaIoBaseDownload", self.downloader)
        # Hard-fail any accidental service-account path.
        from app.services import google_drive_folder_service
        self.patch(google_drive_folder_service, "_get_service", lambda: (_ for _ in ()).throw(AssertionError("Real Drive forbidden")))
        self.app = FastAPI()
        self.app.include_router(local_video_drive.router)
        self.app.include_router(api.router)
        self.app.dependency_overrides[get_current_user] = lambda: User(username="synthetic-admin", password_hash="x", role="admin")
        for i in range(1, 8): self.add_video(i)
        self.addCleanup(self.drain)

    def patch(self, obj, name, value):
        item = patch.object(obj, name, value); item.start(); self.addCleanup(item.stop); return item

    def drain(self):
        video._cancel.set()
        deadline = time.monotonic() + 10
        while video._active is not None and time.monotonic() < deadline: time.sleep(.01)
        self.assertIsNone(video._active)

    def add_video(self, i, fps=5, frames=6):
        key = f"video_{i:05d}"
        path = self.root / f"synthetic-{i}.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (160, 80))
        self.assertTrue(writer.isOpened())
        for frame in range(frames):
            image = np.full((80, 160, 3), 20 + i * 15 + frame, np.uint8)
            writer.write(image)
        writer.release()
        data = path.read_bytes()
        self.payloads[key] = data
        self.items[key] = dict(id=key, name=f"cam{i:02d}.avi", mimeType="video/x-msvideo", size=str(len(data)),
            parents=[PARENT], trashed=False, version="1", md5Checksum=hashlib.md5(data).hexdigest(), modifiedTime="2026-01-01T00:00:00Z",
            videoMediaMetadata=dict(durationMillis=str(int(frames / fps * 1000)), width=160, height=80))
        return key

    def fake_service(self):
        owner = self
        class Files:
            def get(self, fileId, **_):
                if fileId == PARENT: return FakeRequest(dict(id=PARENT, name="Synthetic camera videos", mimeType=drive.FOLDER_MIME))
                if fileId not in owner.items: raise SimpleHttpError(404)
                return FakeRequest(owner.items[fileId])
            def list(self, q, **_):
                assert q == f"'{PARENT}' in parents and trashed = false"
                return FakeRequest(dict(files=[item for key, item in owner.items.items() if owner.visible is None or key in owner.visible]))
            def get_media(self, fileId, **_):
                owner.downloads.append(fileId)
                return SimpleNamespace(key=fileId)
            def create(self, **_): raise AssertionError("No Drive writes")
            def update(self, **_): raise AssertionError("No Drive writes")
            def delete(self, **_): raise AssertionError("Never delete originals")
        return SimpleNamespace(_http=SimpleNamespace(timeout=None), files=lambda: Files(),
            about=lambda: SimpleNamespace(get=lambda **_: FakeRequest(dict(user=dict(displayName=ACCOUNT["display_name"], emailAddress=ACCOUNT["email"], permissionId=ACCOUNT["account_id"])))))

    def downloader(self, output, request, chunksize):
        owner = self
        class Reader:
            def next_chunk(self, num_retries):
                assert num_retries == 0
                folders = list(video.ROOT.glob("*/downloads"))
                assert len(folders) == 1
                assert len(list(folders[0].iterdir())) == 1, "Never accumulate several sources"
                data = owner.payloads[request.key]
                output.write(data[:len(data) // 2])
                if request.key in owner.broken: raise ConnectionError("Synthetic interrupted download")
                output.write(data[len(data) // 2:])
                return None, True
        return Reader()

    def faces(self, image):
        score = .5 + float(image.mean()) / 1000
        return [SimpleNamespace(embedding=np.array([1, score]), bbox=np.array([5, 10, 25, 40])),
            SimpleNamespace(embedding=np.array([1, .2]), bbox=np.array([30, 10, 50, 40])),
            SimpleNamespace(embedding=np.array([2, .8]), bbox=np.array([90, 10, 110, 40])),
            SimpleNamespace(embedding=np.array([0, 0]), bbox=np.array([120, 10, 140, 40]))]

    def matcher(self):
        class Index:
            def match_batch(self, embeddings, threshold):
                assert threshold == MODEL["threshold"]
                return [SimpleNamespace(person_id="person-a" if row[0] == 1 else "person-b" if row[0] == 2 else None,
                    full_name="Synthetic Alpha" if row[0] == 1 else "Synthetic Beta", first_name="Synthetic", score=float(row[1])) for row in embeddings]
        return Index()

    def call(self, path, method="GET", value=None, headers=None):
        return asyncio.run(request(self.app, "/api/local-video-experiment" + path, method,
            json.dumps(value).encode() if value is not None else b"", {"Content-Type": "application/json", **(headers or {})}))

    def start(self, keys=None, settings=None):
        result = batch.create("Seven synthetic cameras", PARENT, keys or list(self.items), ACCOUNT["account_id"], settings or ScanSettings())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = video.get(result["id"])
            if state["status"] not in video.ACTIVE and video._active is None: return state
            time.sleep(.01)
        self.fail("Batch worker did not stop")

    def clean(self, state):
        self.assertFalse((video.directory(state["id"]) / "downloads").exists())
        self.assertTrue(state["temporary_downloads_cleaned"])

    def test_seven_sources_count_once_and_single_best_preview_per_identity(self):
        with patch.object(video, "identity_snapshot", wraps=video.identity_snapshot) as frozen:
            state = self.start(list(reversed(self.items)))
        self.assertEqual(frozen.call_count, 1)
        self.assertEqual(state["status"], "completed", state["error"])
        self.assertEqual(self.downloads, sorted(self.items))
        self.assertEqual(state["scan_settings"], ScanSettings().model_dump())
        self.assertEqual((state["sampled_frames"], state["matched_face_detections"], state["unknown_detections"]), (21, 63, 21))
        self.assertEqual([p["detection_count"] for p in state["people"]], [21, 21])
        for source in state["videos"]:
            self.assertEqual((source["decoded_frames"], source["sampled_frames"]), (6, 3))
            self.assertEqual(source["media"]["fps"], 5)
            self.assertEqual([s["frame_index"] for s in source["samples"]], [0, 2, 4])
        alpha, beta = state["people"]
        self.assertEqual(alpha["preview"]["source_filename"], "cam07.avi")
        self.assertEqual(alpha["preview"]["frame_number"], 5)
        self.assertEqual(alpha["preview"]["timestamp_seconds"], .8)
        self.assertEqual(alpha["preview"]["bbox"], [5, 10, 25, 40])
        self.assertEqual(beta["preview"]["source_filename"], "cam01.avi")
        self.assertEqual(beta["preview"]["frame_number"], 1, "Equal scores keep earliest source/frame")
        self.assertEqual(len(list((video.directory(state["id"]) / "previews").iterdir())), 2)
        before = copy.deepcopy(state)
        batch.refresh_totals(state); batch.refresh_totals(state)
        self.assertEqual(state, before, "Aggregation never increments old totals")
        self.clean(state)

    def test_csv_verdict_preview_and_per_source_rows_remain_after_download_deletion(self):
        state = self.start()
        visible = video.public(state)
        self.assertEqual(visible["actual_video_detection_hz"], 2.5)
        row = visible["people"][0]
        original = (video.directory(state["id"]) / "state.json").read_bytes()
        reviewed = video.review_example(state["id"], row["identity_key"], "Incorrect", row["preview"]["example_key"])
        self.assertEqual(reviewed["people"][0]["manual_verdict"], "Incorrect")
        self.assertEqual(original, (video.directory(state["id"]) / "state.json").read_bytes())
        records = list(csv.DictReader(io.StringIO(video.to_csv(video.get(state["id"])))))
        metadata = {r["key"]: r["value"] for r in records if r["record_type"] == "metadata"}
        self.assertEqual((metadata["camera_fps"], metadata["post_scan_delay_seconds"], metadata["target_detections_per_second"]), ("30.0", "0.4", "2.5"))
        self.assertEqual(len([r for r in records if r["record_type"] == "video"]), 7)
        self.assertEqual(len([r for r in records if r["record_type"] == "video_person"]), 14)
        people = [r for r in records if r["record_type"] == "person"]
        self.assertEqual(people[0]["manual_verdict"], "Incorrect")
        self.assertEqual(people[0]["source_filename"], "cam07.avi")
        self.assertEqual(people[0]["detection_count"], "21")
        self.assertIn("shown", metadata["review_meaning"])
        self.assertNotIn(str(self.root), video.to_csv(state))
        code, image, _ = self.call(f"/{state['id']}/people/{row['identity_key']}/preview?example_key={row['preview']['example_key']}")
        self.assertEqual(code, 200); self.assertTrue(image.startswith(b"\xff\xd8"))
        self.assertEqual(self.call(f"/{state['id']}/video")[0], 410)
        self.clean(state)

    def test_one_interrupted_download_does_not_lose_other_videos_and_cleanup(self):
        self.broken.add("video_00003")
        state = self.start()
        self.assertEqual(state["status"], "completed_with_errors")
        self.assertEqual((state["sampled_frames"], state["matched_face_detections"]), (18, 54))
        self.assertEqual(state["videos"][2]["status"], "failed")
        self.assertEqual(video.public(state)["completed_videos"], 6)
        self.assertEqual(len(self.downloads), 7)
        self.clean(state)

    def test_decode_failure_keeps_partial_counts_and_continues(self):
        actual = video.process_video
        def fail_source(source, path, index, settings, checkpoint, representative):
            actual(source, path, index, settings, checkpoint, representative)
            if source["filename"] == "cam03.avi": raise video.MediaError("Synthetic damaged tail: partial frame results retained")
        with patch.object(video, "process_video", fail_source): state = self.start()
        self.assertEqual(state["status"], "completed_with_errors")
        self.assertEqual(state["sampled_frames"], 21, "Failed source completed samples remain included exactly once")
        self.assertEqual(state["videos"][2]["status"], "failed")
        self.clean(state)

    def test_cancellation_during_download_removes_temp_and_preserves_prior_sources(self):
        actual = drive.download
        def cancel_third(item, parent, account, path, cancelled, progress):
            if item["file_id"] == "video_00003":
                path.write_bytes(b"partial"); video._cancel.set()
                raise drive.DownloadCancelled("Synthetic cancel")
            return actual(item, parent, account, path, cancelled, progress)
        with patch.object(drive, "download", cancel_third): state = self.start()
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["sampled_frames"], 6)
        self.assertEqual([v["status"] for v in state["videos"]], ["completed", "completed", "cancelled"] + ["not_processed"] * 4)
        self.assertEqual(len(self.downloads), 2)
        self.clean(state)

    def test_cancellation_during_inference_keeps_complete_frame_and_no_backlog(self):
        actual = self.faces
        def faces(image):
            video._cancel.set(); return actual(image)
        with patch.object(video, "detect", faces): state = self.start()
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["sampled_frames"], 1)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(state["people"][0]["detection_count"], 1)
        self.clean(state)

    def test_restart_cleanup_never_downloads_or_duplicates_or_restarts(self):
        state = self.start()
        state["status"] = "running"
        state["videos"][5].update(status="running")
        state["videos"][6].update(status="pending", people=[], sampled_frames=0, decoded_frames=0, matched_face_detections=0, unknown_detections=0)
        batch.refresh_totals(state); video._write(state)
        temp = batch.downloads_dir(state["id"]); temp.mkdir(); (temp / "source.avi").write_bytes(b"interrupted")
        before_downloads = self.downloads[:]
        video._initialized = False; video.initialize()
        recovered = video.get(state["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["videos"][5]["status"], "interrupted")
        self.assertEqual(recovered["videos"][6]["status"], "not_processed")
        self.assertEqual(recovered["sampled_frames"], 18)
        self.assertEqual(self.downloads, before_downloads)
        self.assertEqual(self.call(f"/{state['id']}/start", "POST", {})[0], 409)
        video._initialized = False; video.initialize()
        self.assertEqual(video.get(state["id"])["sampled_frames"], 18)
        self.clean(recovered)

    def test_terminal_restart_cleans_leftover_temporary_file_not_preview(self):
        state = self.start(["video_00001"])
        temp = batch.downloads_dir(state["id"]); temp.mkdir(); (temp / "source.avi").write_bytes(b"leftover")
        previews = list((video.directory(state["id"]) / "previews").iterdir())
        video._initialized = False; video.initialize()
        self.assertFalse(temp.exists())
        self.assertTrue(all(p.exists() for p in previews))
        self.assertEqual(video.get(state["id"])["status"], "completed")

    def test_delete_isolation_and_old_single_experiment_unchanged(self):
        old = video.allocate("old.avi"); old.update(status="completed"); video._write(old)
        old_video = video.video_path(old); old_video.write_bytes(b"older-local-upload")
        before = (video.directory(old["id"]) / "state.json").read_bytes()
        state = self.start(["video_00001"])
        self.assertEqual(self.call(f"/{state['id']}", "DELETE")[0], 200)
        self.assertFalse(video.directory(state["id"]).exists())
        self.assertEqual(old_video.read_bytes(), b"older-local-upload")
        self.assertEqual((video.directory(old["id"]) / "state.json").read_bytes(), before)
        self.assertEqual(len(next(csv.reader(io.StringIO(video.to_csv(old))))), 13)

    def test_zero_matches_and_custom_low_fps_no_duplicate_inference(self):
        key = self.add_video(8, fps=1, frames=3)
        with patch.object(video, "detect", lambda image: []):
            state = self.start([key], ScanSettings(camera_fps=120, post_scan_delay_seconds=0, target_detections_per_second=30))
        self.assertEqual(state["people"], [])
        source = state["videos"][0]
        self.assertEqual([s["frame_index"] for s in source["samples"]], [0, 1, 2])
        self.assertEqual(source["sampled_frames"], 3)
        self.assertEqual(source["media"]["effective_available_fps_limit"], 1)
        self.assertEqual(video.public(state)["actual_video_detection_hz"], 1)
        self.clean(state)

    def test_disk_shortage_no_download_and_cleanup(self):
        with patch.object(drive.shutil, "disk_usage", lambda path: SimpleNamespace(free=0)):
            state = self.start(["video_00001"])
        self.assertEqual(state["status"], "failed")
        self.assertIn("Insufficient local disk", state["videos"][0]["error"])
        self.assertEqual(self.downloads, [])
        self.clean(state)

    def test_unsupported_video_and_all_failed_paths_clean_downloads(self):
        key = "video_00001"
        self.payloads[key] = b"synthetic unsupported container"
        self.items[key].update(size=str(len(self.payloads[key])), md5Checksum=hashlib.md5(self.payloads[key]).hexdigest())
        state = self.start([key])
        self.assertEqual(state["status"], "failed")
        self.assertIn("Invalid video container", state["videos"][0]["error"])
        self.assertEqual(state["sampled_frames"], 0)
        self.clean(state)

    def test_matcher_setup_failure_cleanup_preserves_stopped_batch(self):
        with patch.object(video, "identity_snapshot", side_effect=RuntimeError("Synthetic model setup failure")):
            state = self.start(["video_00001"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.downloads, [])
        self.assertEqual(state["videos"][0]["status"], "not_processed")
        self.clean(state)

    def test_incomplete_checksum_and_oversized_response_never_infer_and_cleanup(self):
        key = "video_00001"
        self.payloads[key] += b"extra beyond frozen metadata"
        state = self.start([key])
        self.assertEqual(state["status"], "failed")
        self.assertIn("exceeded", state["videos"][0]["error"])
        self.assertEqual(state["sampled_frames"], 0)
        self.clean(state)
        video._cancel.clear(); self.payloads[key] = self.payloads[key][:-40]
        state = self.start([key])
        self.assertEqual(state["status"], "failed")
        self.assertIn("incomplete", state["videos"][0]["error"])
        self.assertEqual(state["sampled_frames"], 0)
        self.clean(state)

    def test_slow_work_batch_uses_same_timing_no_duplicate_frames_or_scan_queue(self):
        key = self.add_video(8, fps=30, frames=60)
        times = iter([0, .6, 1, 1.6])
        with patch.object(video, "scan_clock", lambda: next(times)):
            state = self.start([key])
        self.assertEqual(state["status"], "completed", state["videos"][0]["error"])
        self.assertEqual([s["frame_index"] for s in state["videos"][0]["samples"]], [0, 30])
        self.assertEqual(video.public(state)["actual_video_detection_hz"], 1)
        self.clean(state)

    def test_scaled_batch_preview_uses_actual_image_dimensions_and_detection_box(self):
        state = self.start(["video_00001"])
        source = state["videos"][0]
        target = state["people"][0]
        image = np.zeros((1001, 2001, 3), np.uint8)
        face = SimpleNamespace(bbox=np.array([100, 200, 500, 800]))
        match = SimpleNamespace(score=.999)
        video.save_representative(source, target, face, match, image, 4, .8)
        example = target["preview"]
        self.assertEqual(example["source_filename"], "cam01.avi")
        self.assertEqual(example["width"], 1920)
        self.assertEqual(example["bbox"], [100 * 1920 / 2001, 200 * example["height"] / 1001, 500 * 1920 / 2001, 800 * example["height"] / 1001])
        self.assertEqual(len(list((video.directory(state["id"]) / "previews").iterdir())), 2)

    def test_api_selection_validates_defaults_links_ids_parent_size_account_and_settings(self):
        listing = drive.folder_listing("https://drive.google.com/drive/folders/" + PARENT)
        self.assertEqual(len(listing["files"]), 7)
        self.assertEqual(listing["account"], ACCOUNT)
        self.assertFalse(listing["requires_picker_grant"])
        self.visible = {"video_00001"}
        confirmed = drive.folder_listing(PARENT, ["video_00002"])
        self.assertEqual(len(confirmed["files"]), 2)
        body = dict(name="Synthetic API", folder_link=PARENT, file_ids=["video_00001"], account_id=ACCOUNT["account_id"])
        for change in ({"folder_link": "https://example.test/private/video"}, {"file_ids": ["../file.avi"]},
            {"file_ids": ["video_00001"] * 2}, {"account_id": "different-account"}, {"scan_settings": {"camera_fps": 0}}):
            self.assertIn(self.call("/drive/batches", "POST", {**body, **change})[0], {400, 422})
        self.items["video_00001"]["parents"] = ["other_folder"]
        self.assertEqual(self.call("/drive/batches", "POST", body)[0], 400)
        self.items["video_00001"]["parents"] = [PARENT]
        self.items["video_00001"]["size"] = str(drive.MAX_BYTES + 1)
        self.assertEqual(self.call("/drive/batches", "POST", body)[0], 400)
        self.assertEqual(self.downloads, [])

    def test_missing_folder_access_is_actionable_and_expired_grant_is_not_raw_error(self):
        with patch.object(drive, "metadata", side_effect=drive.GrantRequired("No access to this folder. Check sharing with the connected account.")):
            code, raw, _ = self.call("/drive/folder", "POST", dict(folder_link=PARENT))
        self.assertEqual(code, 400)
        self.assertIn(b"No access", raw)
        self.assertNotIn(b"Picker", raw)
        with patch.object(drive.oauth, "get_video_read_service", side_effect=drive.oauth.DriveOAuthError("invalid_grant token=SECRET")):
            code, raw, _ = self.call("/drive/folder", "POST", dict(folder_link=PARENT))
        self.assertEqual(code, 400); self.assertNotIn(b"SECRET", raw); self.assertIn(b"expired", raw)

    def test_seven_link_only_candidates_include_mkv_with_unexpected_mime(self):
        self.items["video_00001"].update(name="cam01.MKV", mimeType="application/x-unknown")
        self.items["video_00002"].update(name="cam02.mkv", mimeType="application/octet-stream")
        result = drive.folder_listing("https://drive.google.com/drive/folders/" + PARENT)
        self.assertEqual(result["outcome"], "videos")
        self.assertEqual(result["supported_count"], 7)
        self.assertEqual(len(result["files"]), 7)
        self.assertTrue(all(f["supported"] for f in result["files"]))
        self.assertNotIn("Picker", result["message"])
        self.assertEqual(self.downloads, [], "Listing cannot download or start inference")

    def test_large_long_drive_input_limits_do_not_change_local_upload_limits(self):
        self.items["video_00001"].update(size=str(2 * 1024**3))
        self.items["video_00001"]["videoMediaMetadata"]["durationMillis"] = str(3600 * 1000)
        result = drive.folder_listing(PARENT)
        self.assertTrue(result["files"][0]["supported"])
        self.assertEqual((video.MAX_BYTES, video.MAX_DURATION), (512 * 1024**2, 1800))
        self.assertEqual((result["limits"]["max_bytes"], result["limits"]["max_duration_seconds"]), (16 * 1024**3, 12 * 3600))
        with self.assertRaisesRegex(drive.DriveVideoError, "16 GiB"):
            drive.validate_video({**self.items["video_00001"], "size": str(drive.MAX_BYTES + 1)}, PARENT)
        too_long = copy.deepcopy(self.items["video_00001"])
        too_long["videoMediaMetadata"]["durationMillis"] = str((drive.MAX_DURATION + 1) * 1000)
        with self.assertRaisesRegex(drive.DriveVideoError, "12 hours"):
            drive.validate_video(too_long, PARENT)

    def test_all_rejected_files_visible_with_specific_reasons_not_fake_empty(self):
        self.items["video_00001"].update(name="notes.pdf", mimeType="application/pdf")
        self.items["video_00002"].update(size=str(drive.MAX_BYTES + 1))
        self.items["video_00003"]["videoMediaMetadata"]["durationMillis"] = str((drive.MAX_DURATION + 1) * 1000)
        self.items["video_00004"]["capabilities"] = {"canDownload": False}
        self.items["video_00005"]["size"] = "0"
        self.items["video_00006"]["videoMediaMetadata"].update(width=5000, height=5000)
        self.items["video_00007"].update(mimeType="application/vnd.google-apps.shortcut")
        result = drive.folder_listing(PARENT)
        self.assertEqual((result["outcome"], len(result["files"]), result["supported_count"]), ("unsupported", 7, 0))
        errors = " ".join(f["error"] for f in result["files"])
        for reason in ("16 GiB", "12 hours", "disabled downloading", "zero", "pixel limit", "shortcuts", "Supported containers"):
            self.assertIn(reason, errors)
        self.assertEqual(self.downloads, [])

    def test_pagination_shared_drive_and_resource_key_requests(self):
        calls = []
        original_get = self.service.files().get
        class Files:
            def get(_, fileId, **kwargs):
                calls.append(("get", kwargs))
                if fileId == PARENT:
                    req = FakeRequest(dict(id=PARENT, name="Shared cameras", mimeType=drive.FOLDER_MIME, driveId="shared_drive_123"))
                else: req = original_get(fileId, **kwargs)
                req.headers = {}
                calls.append(("request", req))
                return req
            def list(_, **kwargs):
                calls.append(("list", kwargs))
                page = kwargs.get("pageToken")
                req = FakeRequest(dict(files=list(self.items.values())[3:] if page else list(self.items.values())[:3], **({} if page else {"nextPageToken": "second"})))
                req.headers = {}; calls.append(("request", req)); return req
        self.service.files = lambda: Files()
        result = drive.folder_listing("https://drive.google.com/drive/folders/" + PARENT + "?resourcekey=synthetic_key")
        self.assertEqual(result["supported_count"], 7)
        requests = [value for action, value in calls if action == "list"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["pageToken"], "second")
        for request in requests:
            self.assertEqual((request["corpora"], request["driveId"]), ("drive", "shared_drive_123"))
            self.assertTrue(request["supportsAllDrives"] and request["includeItemsFromAllDrives"])
            self.assertNotIn("mimeType", request["q"], "No MIME filter may hide MKV")
        for action, req in calls:
            if action == "request": self.assertEqual(req.headers["X-Goog-Drive-Resource-Keys"], PARENT + "/synthetic_key")

    def test_empty_subfolders_only_mixed_files_and_insufficient_permission(self):
        self.visible = set()
        self.assertEqual(drive.folder_listing(PARENT)["outcome"], "empty")
        self.items["subfolder_123"] = dict(id="subfolder_123", name="Nested", mimeType=drive.FOLDER_MIME, parents=[PARENT])
        self.visible = {"subfolder_123"}
        result = drive.folder_listing(PARENT)
        self.assertEqual((result["outcome"], result["subfolder_count"], result["files"]), ("no_files", 1, []))
        self.items["document_123"] = dict(id="document_123", name="notes.txt", mimeType="text/plain", parents=[PARENT], size="10")
        self.visible = None
        result = drive.folder_listing(PARENT)
        self.assertEqual((result["supported_count"], result["unsupported_count"], result["subfolder_count"]), (7, 1, 1))
        for status, text in ((403, "permission"), (404, "No access"), (401, "expired")):
            with patch.object(self.service, "files", return_value=SimpleNamespace(get=lambda **_: (_ for _ in ()).throw(SimpleHttpError(status)))):
                code, raw, _ = self.call("/drive/folder", "POST", dict(folder_link=PARENT))
            self.assertEqual(code, 400); self.assertIn(text.encode(), raw)
            self.assertNotIn(b"Picker", raw)

    def test_read_scope_missing_requires_reconnection_not_picker(self):
        with patch.object(drive.oauth, "get_video_read_service", side_effect=drive.oauth.DriveScopeRequired("Reconnect Google Drive and allow drive.readonly.")):
            code, raw, _ = self.call("/drive/folder", "POST", dict(folder_link=PARENT))
        self.assertEqual(code, 400)
        self.assertIn(b"Reconnect", raw); self.assertIn(b"drive.readonly", raw)
        self.assertNotIn(b"Picker", raw)

    def test_new_batch_frozen_input_limits_and_longer_probe_preserve_csv(self):
        actual_probe = video.probe
        durations = []
        def probe(path, *, max_duration):
            durations.append(max_duration)
            return actual_probe(path, max_duration=max_duration)
        with patch.object(video, "probe", probe): state = self.start(["video_00001"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(durations, [12 * 3600])
        self.assertEqual(state["input_limits"], drive.limits())
        self.assertEqual(state["videos"][0]["input_limits"], state["input_limits"])
        self.assertIn("Synthetic Alpha", video.to_csv(state))
        self.assertEqual(len(list((video.directory(state["id"]) / "previews").iterdir())), 2)
        records = list(csv.DictReader(io.StringIO(video.to_csv(state))))
        metadata = {r["key"]: r["value"] for r in records if r["record_type"] == "metadata"}
        self.assertEqual(metadata["input_max_bytes"], str(drive.MAX_BYTES))
        self.assertEqual(metadata["input_max_duration_seconds"], str(drive.MAX_DURATION))
        self.clean(state)

    def test_synthetic_mkv_vfr_and_truncated_batch_keep_names_previews_and_csv(self):
        executable = shutil.which("ffmpeg")
        self.assertIsNotNone(executable, "FFmpeg required for synthetic MKV regression")
        path = self.root / "synthetic-vfr.mkv"
        command = [executable, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x96:rate=15:duration=3",
            "-vf", "setpts=PTS+if(gte(N\\,20)\\,3/TB\\,0)", "-fps_mode", "passthrough", "-c:v", "ffv1", "-y", str(path)]
        result = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        complete = path.read_bytes()
        for i, data in ((1, complete), (2, complete[:int(len(complete) * .65)])):
            key = f"video_{i:05d}"
            self.payloads[key] = data
            self.items[key].update(name=f"cam{i:02d}.mkv", mimeType="application/x-unexpected",
                size=str(len(data)), md5Checksum=hashlib.md5(data).hexdigest())
        with patch.object(video, "verify_eof", REAL_VERIFY_EOF):
            state = self.start(["video_00001", "video_00002", "video_00003"])
        self.assertEqual(state["status"], "completed_with_errors")
        good, broken, last = state["videos"]
        self.assertEqual((good["status"], good["decoded_frames"]), ("completed", 45))
        self.assertGreater(good["media"]["frame_count"], good["decoded_frames"])
        self.assertEqual(good["decode_audit"]["outcome"], "normal_eof")
        self.assertEqual(broken["status"], "failed")
        self.assertGreater(broken["sampled_frames"], 0)
        self.assertEqual(last["status"], "completed")
        self.assertEqual(state["people"][0]["detection_count"], sum(s["sampled_frames"] for s in state["videos"]))
        visible = video.public(state)
        self.assertEqual(visible["people"][0]["name"], "Synthetic Alpha")
        self.assertTrue(visible["people"][0]["preview_available"])
        self.assertTrue(all("source_file" not in s for s in visible["videos"]))
        self.assertIn("Synthetic Alpha", video.to_csv(state))
        self.clean(state)

    def test_incomplete_listing_repeated_page_token_and_malformed_links_do_not_start(self):
        for value, reason in ((dict(incompleteSearch=True, files=[]), "incomplete"),
            (dict(nextPageToken="repeated", files=[]), "pagination")):
            with patch.object(self.service, "files", return_value=SimpleNamespace(get=lambda **_: FakeRequest(dict(id=PARENT, mimeType=drive.FOLDER_MIME)), list=lambda **_: FakeRequest(value))):
                with self.assertRaisesRegex(drive.DriveVideoError, reason): drive.folder_listing(PARENT)
        for link in ("https://example.test/drive/folders/" + PARENT, "https://drive.google.com:bad/drive/folders/" + PARENT,
            "https://someone@drive.google.com/drive/folders/" + PARENT, "https://drive.google.com/drive/folders/" + PARENT + "?resourcekey=bad/key"):
            code, _, _ = self.call("/drive/folder", "POST", dict(folder_link=link))
            self.assertEqual(code, 400)
        self.assertEqual(self.downloads, [])

    def test_changed_remote_version_not_silently_reprocessed(self):
        actual = drive.download
        def changed(item, parent, account, path, cancelled, progress):
            self.items[item["file_id"]]["version"] = "2"
            return actual(item, parent, account, path, cancelled, progress)
        with patch.object(drive, "download", changed): state = self.start(["video_00001"])
        self.assertEqual(state["status"], "failed")
        self.assertIn("changed since selection", state["videos"][0]["error"])
        self.assertEqual(self.downloads, [])
        self.clean(state)

    def test_all_drive_routes_results_previews_verdicts_admin_only(self):
        state = self.start(["video_00001"])
        person = video.public(state)["people"][0]
        paths = [("/drive/status", "GET"), ("/drive/oauth/start", "GET"), ("/drive/picker/config", "GET"),
            ("/drive/picker/token", "POST"), ("/drive/folder", "POST"), ("/drive/batches", "POST"),
            (f"/{state['id']}", "GET"), (f"/{state['id']}/report.csv", "GET"),
            (f"/{state['id']}/people/{person['identity_key']}/preview?example_key={person['preview']['example_key']}", "GET"),
            (f"/{state['id']}/people/{person['identity_key']}/review", "POST")]
        self.app.dependency_overrides[get_current_user] = lambda: User(username="member", password_hash="x", role="user")
        for path, method in paths: self.assertEqual(self.call(path, method, {})[0], 403, path)
        self.app.dependency_overrides.clear()
        for path, method in paths: self.assertEqual(self.call(path, method, {})[0], 401, path)

class SimpleHttpError(Exception):
    def __init__(self, status): self.resp = SimpleNamespace(status=status)

if __name__ == "__main__": unittest.main()
