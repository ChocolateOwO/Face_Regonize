"""Synthetic sequential videos + fake detector/identities. No Drive/real data.

ASGI requests exercised directly to avoid adding httpx to the existing Python
environment. Each test has its own experiment root and temporary identity DB.
"""
import asyncio
import csv
import hashlib
import inspect
import io
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import shutil
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from fastapi import FastAPI
from sqlmodel import Session, SQLModel, create_engine, select
from app.api import local_video_experiment as api
from app.auth.deps import get_current_user
from app.face_recognition.index import RecognitionIndex
from app.models.models import Person, User
from app.services import local_video_experiment as video
from app.services.scan_settings import ScanSettings, next_scan_start

REAL_SNAPSHOT = video.identity_snapshot

ADMIN = User(username="test-admin", password_hash="x", role="admin")

async def request(app, path, method="GET", body=b"", headers=None, chunks=None):
    path, _, query = path.partition("?")
    items = list(chunks) if chunks is not None else [body]
    sent = []
    async def receive():
        if items:
            data = items.pop(0)
            if data is None:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": data, "more_body": bool(items)}
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(event):
        sent.append(event)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": query.encode(),
        "headers": [(k.lower().encode(), str(v).encode()) for k,v in (headers or {}).items()],
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": ""}
    await app(scope, receive, send)
    start = next(e for e in sent if e["type"] == "http.response.start")
    result = b"".join(e.get("body", b"") for e in sent if e["type"] == "http.response.body")
    return start["status"], result, dict(start["headers"])

def embedding(n):
    arr = np.zeros(512, np.float32)
    arr[n] = 1
    return arr

class VideoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-video-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch(video, "ROOT", self.root / "experiments")
        self.patch(video, "_initialized", False)
        self.patch(video, "_active", None)
        self.patch(video, "_cancel", threading.Event())
        self.app = FastAPI()
        self.app.include_router(api.router)
        self.app.dependency_overrides[get_current_user] = lambda: ADMIN
        self.index = RecognitionIndex()
        self.index.rebuild([SimpleNamespace(id="a", first_name="Same", last_name="Name", participant_id="1", embedding=embedding(0).tobytes()),
                            SimpleNamespace(id="b", first_name="Same", last_name="Name", participant_id="2", embedding=embedding(1).tobytes())])
        self.model = {"name":"buffalo_l", "provider":"FakeProvider", "detector_provider":"FakeProvider",
            "threshold":0.45, "detection_threshold":0.5, "detection_size":[320,320], "enrolled_identities":2}
        self.patch(video, "identity_snapshot", lambda: (self.index, self.model.copy()))
        self.patch(video, "detect", lambda image: [])
        self.patch(video, "scan_clock", lambda: 0.0)
        # Google construction is a hard failure even if an accidental coupling
        # gets introduced in a later change.
        from app.services import google_drive_folder_service, google_drive_oauth_service
        self.patch(google_drive_folder_service, "_get_service", self.no_google)
        self.patch(google_drive_oauth_service, "get_write_service", self.no_google)
        self.addCleanup(self.drain)

    def no_google(self, *_a, **_kw):
        raise AssertionError("Real Google Drive forbidden in synthetic tests")

    def patch(self, obj, name, value):
        p = patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def drain(self):
        video._cancel.set()
        deadline = time.monotonic()+10
        while video._active is not None and time.monotonic()<deadline:
            time.sleep(0.01)
        self.assertIsNone(video._active)

    def call(self, path="", method="GET", body=b"", headers=None, chunks=None):
        return asyncio.run(request(self.app, "/api/local-video-experiment"+path, method, body, headers, chunks))

    def synthetic(self, fps=5, frames=16):
        path = self.root / "synthetic.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (64,64))
        self.assertTrue(writer.isOpened())
        try:
            for i in range(frames):
                writer.write(np.full((64,64,3), (i*10)%256, np.uint8))
        finally:
            writer.release()
        return path.read_bytes()

    def upload(self, data=None, name="synthetic.avi"):
        data = self.synthetic() if data is None else data
        status, raw, _ = self.call("?"+urlencode({"filename":name}), "POST", headers={
            "Content-Type":"application/octet-stream", "Content-Length":str(len(data))},
            chunks=[data[:23], data[23:100], data[100:]])
        self.assertEqual(status,201,raw)
        return json.loads(raw)

    def wait(self, job_id):
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            state = video.get(job_id)
            if state["status"] not in video.ACTIVE and video._active is None:
                return state
            time.sleep(0.01)
        self.fail("Worker did not reach terminal state")

    def test_identity_snapshot_reads_only_dummy_identities_and_freezes_matcher(self):
        from app.face_recognition import engine
        temp_engine=create_engine("sqlite:///"+(self.root/"identities.db").as_posix())
        self.addCleanup(temp_engine.dispose)
        SQLModel.metadata.create_all(temp_engine)
        with Session(temp_engine) as session:
            person=Person(participant_id="FAKE-1",first_name="Mock",last_name="Identity",
                image_path="",embedding=embedding(0).tobytes())
            session.add(person);session.commit()
            person_id=person.id
        before=(self.root/"identities.db").read_bytes()
        fake_app=SimpleNamespace(det_model=SimpleNamespace(det_thresh=0.5,input_size=(320,320),
            session=SimpleNamespace(get_providers=lambda:["FakeProvider"])))
        threshold=video.settings_cache.get_threshold()
        with patch.object(video,"db_engine",temp_engine), patch.object(engine,"get_face_app",lambda:fake_app), \
                patch.object(engine,"get_active_provider",lambda:"FakeProvider"):
            index,metadata=REAL_SNAPSHOT()
        self.assertEqual((self.root/"identities.db").read_bytes(),before,"No DB writes")
        self.assertEqual(metadata["threshold"],threshold)
        self.assertEqual(metadata["enrolled_identities"],1)
        self.assertEqual(index.match_batch(np.stack([embedding(0)]),threshold)[0].person_id,person_id)
        with Session(temp_engine) as session:
            session.delete(session.get(Person,person_id));session.commit()
        self.assertEqual(index.size(),1,"Frozen snapshot survives later enrollment changes")
        self.assertEqual(video.settings_cache.get_threshold(),threshold)

    def test_stream_upload_is_idle_validated_and_hashed(self):
        data = self.synthetic()
        with patch.object(video, "detect", side_effect=AssertionError("Selection/upload must not infer")):
            state = self.upload(data, "clip.avi")
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["sampled_frames"],0)
        self.assertEqual(state["media"]["sha256"],hashlib.sha256(data).hexdigest())
        self.assertEqual(video.video_path(state).read_bytes(),data)
        self.assertEqual((state["media"]["fps"],state["media"]["max_samples"]), (5,8))
        self.assertIsNone(video._active)

    def test_filename_rejection(self):
        for name in ("../clip.avi", r"C:\clip.avi", "x\n.avi", "x.exe", "x.avi ", "", "x"*181+".avi"):
            with self.subTest(name=name):
                status,_,_=self.call("?"+urlencode({"filename":name}),"POST",b"test",
                    {"Content-Type":"application/octet-stream"})
                self.assertEqual(status,400)
        self.assertFalse(video.ROOT.exists())

    def test_empty_size_content_type_and_stream_limits(self):
        status,_,_=self.call("?filename=x.avi","POST",b"",{"Content-Type":"application/octet-stream"})
        self.assertEqual(status,400)
        self.assertEqual(self.call("?filename=x.avi","POST",b"x",{"Content-Type":"video/avi"})[0],415)
        self.assertEqual(self.call("?filename=x.avi","POST",b"",{
            "Content-Type":"application/octet-stream","Content-Length":str(video.MAX_BYTES+1)})[0],413)
        with patch.object(video,"MAX_BYTES",10):
            self.assertEqual(self.call("?filename=x.avi","POST",headers={"Content-Type":"application/octet-stream"},
                chunks=[b"123456",b"789012"])[0],413)
        self.assertEqual(video.list_jobs(),[])
        self.assertEqual(list(video.ROOT.iterdir()),[])

    def test_disconnect_incomplete_invalid_and_playlist_uploads_cleaned(self):
        headers={"Content-Type":"application/octet-stream"}
        for data in (b"garbage", b"#EXTM3U\nhttp://example.test/clip.ts", b"RIFFxxxxAVI "+"bad".encode()):
            self.assertEqual(self.call("?filename=x.avi","POST",data,headers)[0],400)
        self.assertEqual(self.call("?filename=x.avi","POST",headers=headers,chunks=[b"RIFF",None])[0],400)
        self.assertEqual(self.call("?filename=x.avi","POST",b"small",
            {**headers,"Content-Length":"99"})[0],400)
        self.assertEqual(list(video.ROOT.iterdir()),[])

    def test_duration_fps_pixel_limits(self):
        data=self.synthetic()
        class FakeCapture:
            def __init__(self, fps=5, count=10000, width=64, height=64):
                self.values={cv2.CAP_PROP_FPS:fps,cv2.CAP_PROP_FRAME_COUNT:count,
                    cv2.CAP_PROP_FRAME_WIDTH:width,cv2.CAP_PROP_FRAME_HEIGHT:height}
            def isOpened(self): return True
            def get(self,p): return self.values[p]
            def read(self): return True,np.zeros((64,64,3),np.uint8)
            def release(self): pass
        for cap in (FakeCapture(),FakeCapture(fps=121,count=10),FakeCapture(width=10000),
                    FakeCapture(fps=float("nan")),FakeCapture(count=0),FakeCapture(fps=0.5)):
            with patch.object(video,"open_capture",lambda _p:cap):
                self.assertEqual(self.call("?filename=x.avi","POST",data,
                    {"Content-Type":"application/octet-stream"})[0],400)
        self.assertEqual(video.list_jobs(),[])

    def test_fractional_sampling_is_deterministic_without_seeking(self):
        self.assertEqual(video.next_capture(0,0,0,29.97),(11,0.4))
        self.assertEqual(video.next_capture(0,0,0,2.5),(1,0.4))
        self.assertEqual(video.maximum_sample_count(1,30),1)
        state=self.upload()
        seen=[]
        def detector(image):
            seen.append(round(float(image.mean())))
            return []
        with patch.object(video,"detect",detector):
            video.start(state["id"]); done=self.wait(state["id"])
        self.assertEqual(seen,[0,20,40,60,80,100,120,140])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,2,4,6,8,10,12,14])
        self.assertEqual([s["timestamp_seconds"] for s in done["samples"]],[0,0.4,0.8,1.2,1.6,2,2.4,2.8])
        self.assertEqual(done["decoded_frames"],16)
        self.assertEqual(done["status"],"completed")
        self.assertEqual(video.public(done)["actual_video_detection_hz"],2.5)

    def test_slow_inference_skips_frames_no_queue_and_records_work(self):
        state=self.upload()
        clock_values=iter([0.0,0.2]*20)
        with patch.object(video,"scan_clock",lambda:next(clock_values)):
            video.start(state["id"]); done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,3,6,9,12,15])
        self.assertEqual([s["capture_time_seconds"] for s in done["samples"]],[0,0.6,1.2,1.8,2.4,3])
        self.assertEqual({s["scan_work_seconds"] for s in done["samples"]},{0.2})
        self.assertEqual(done["decoded_frames"],16)
        self.assertEqual(done["sampled_frames"],6)
        visible=video.public(done)
        self.assertEqual(visible["actual_video_detection_hz"],round(1/0.6,6))
        self.assertEqual(visible["decoded_frames_not_inferred"],10)

    def test_fps_quantization_uses_latest_not_future_frame(self):
        data=self.synthetic(fps=29.97,frames=61)
        state=self.upload(data)
        video.start(state["id"]); done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,11,23,35,47,59])
        for sample in done["samples"]:
            self.assertLessEqual(sample["timestamp_seconds"],sample["capture_time_seconds"]+1e-6)
            self.assertLess(sample["capture_time_seconds"]-sample["timestamp_seconds"],1/29.97+1e-6)

    def test_low_fps_never_reprocesses_same_saved_frame(self):
        state=self.upload(self.synthetic(fps=1,frames=4))
        video.start(state["id"]); done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,1,2,3])
        self.assertEqual([s["capture_time_seconds"] for s in done["samples"]],[0,1,2,3])
        self.assertEqual(video.public(done)["actual_video_detection_hz"],1)
        self.assertEqual(video.public(done)["decoded_frames_not_inferred"],0)

    def test_varying_work_controls_next_capture_and_is_not_fixed_2_5_hz(self):
        state=self.upload()
        work=[0.1,0.3,0.0,0.6,0.2,0.1]
        values=iter([value for duration in work for value in (0.0,duration)])
        with patch.object(video,"scan_clock",lambda:next(values)):
            video.start(state["id"]);done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,2,6,8,13])
        self.assertEqual([s["capture_time_seconds"] for s in done["samples"]],[0,0.5,1.2,1.6,2.6])
        self.assertLess(video.public(done)["actual_video_detection_hz"],2.5)

    def test_old_completed_results_keep_original_rule_counts_and_csv(self):
        state=self.upload()
        state.update(status="completed",sampling_hz=1.0,
            sampling_method="nominal-fps-v1: sample ceil(k*fps); timestamp=frame_index/fps; sequential decode",
            sampled_frames=4,decoded_frames=16,processing_seconds=2.0,
            samples=[dict(frame_index=i*5,timestamp_seconds=float(i),faces=0,matched_people=0,unknown_detections=0) for i in range(4)])
        for field in ("post_scan_delay_seconds","historical_reference","timing_differences","scan_work_seconds"):
            state.pop(field,None)
        with video._lock:video._write(state)
        before=(video.directory(state["id"])/"state.json").read_bytes()
        visible=video.public(video.get(state["id"]))
        rows=list(csv.DictReader(io.StringIO(video.to_csv(video.get(state["id"])))))
        metadata={r["key"]:r["value"] for r in rows if r["record_type"]=="metadata"}
        self.assertEqual(metadata["sampling_hz"],"1.0")
        self.assertTrue(metadata["sampling_method"].startswith("nominal-fps-v1"))
        self.assertEqual(visible["actual_video_detection_hz"],1.0)
        self.assertEqual(metadata["sampled_frame_count"],"4")
        self.assertEqual((video.directory(state["id"])/"state.json").read_bytes(),before)

    def test_rates_and_reference_csv_ui_are_explicit(self):
        state=self.upload()
        self.assertIsNone(video.public(state)["actual_video_detection_hz"])
        video.start(state["id"]); done=self.wait(state["id"])
        visible=video.public(done)
        self.assertAlmostEqual(visible["processing_throughput_fps"],done["sampled_frames"]/done["processing_seconds"],places=5)
        rows=list(csv.DictReader(io.StringIO(video.to_csv(done))))
        metadata={r["key"]:r["value"] for r in rows if r["record_type"]=="metadata"}
        self.assertEqual(metadata["sampling_hz"],"variable")
        self.assertEqual(metadata["post_scan_delay_seconds"],"0.4")
        self.assertEqual(metadata["actual_video_detection_hz"],"2.5")
        self.assertEqual(metadata["historical_reference"],video.HISTORICAL_COMMIT)
        self.assertTrue(metadata["sampling_method"].startswith("configured-camera-v3"))
        page=(Path(__file__).resolve().parents[2]/"frontend/src/pages/LocalVideoExperiment.tsx").read_text(encoding="utf-8")
        self.assertIn("Actual processed cadence (video time)",page)
        self.assertIn("Measured processing throughput (wall time)",page)

    def test_defaults_snapshot_and_combined_rule_have_no_added_default_delay(self):
        state=self.upload()
        video.start(state["id"]);done=self.wait(state["id"])
        self.assertEqual(done["scan_settings"],ScanSettings().model_dump())
        for sample in done["samples"]:
            self.assertEqual(sample["scan_work_seconds"],0)
        self.assertAlmostEqual(next_scan_start(10,10.2,ScanSettings()),10.6)
        self.assertAlmostEqual(next_scan_start(10,10,ScanSettings()),10.4)

    def test_custom_target_dominates_short_post_scan_wait(self):
        state=self.upload()
        settings=ScanSettings(camera_fps=30,post_scan_delay_seconds=0.1,target_detections_per_second=1)
        values=iter([0,0.2]*20)
        with patch.object(video,"scan_clock",lambda:next(values)):
            video.start(state["id"],settings);done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,5,10,15])
        self.assertEqual([s["capture_time_seconds"] for s in done["samples"]],[0,1,2,3])

    def test_custom_post_scan_wait_dominates_target_and_slow_inference(self):
        state=self.upload()
        settings=ScanSettings(post_scan_delay_seconds=1,target_detections_per_second=10)
        values=iter([0,0.2]*20)
        with patch.object(video,"scan_clock",lambda:next(values)):
            video.start(state["id"],settings);done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,6,12])
        self.assertEqual([s["capture_time_seconds"] for s in done["samples"]],[0,1.2,2.4])

    def test_virtual_camera_limit_skips_source_frames_and_preserves_media(self):
        data=self.synthetic(fps=30,frames=60)
        state=self.upload(data)
        settings=ScanSettings(camera_fps=2,post_scan_delay_seconds=0,target_detections_per_second=30)
        calls=[]
        with patch.object(video,"detect",lambda image:calls.append(image.mean()) or []):
            video.start(state["id"],settings);done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,15,30,45])
        self.assertEqual([s["capture_time_seconds"] for s in done["samples"]],[0,0.5,1,1.5])
        self.assertEqual(len(calls),4)
        self.assertEqual(done["decoded_frames"],60)
        self.assertEqual(done["media"]["fps"],30)
        self.assertEqual(video.public(done)["actual_video_detection_hz"],2)
        self.assertEqual(video.video_path(done).read_bytes(),data)

    def test_low_source_fps_high_requested_rates_no_duplicate_or_backlog(self):
        state=self.upload(self.synthetic(fps=1,frames=4))
        settings=ScanSettings(camera_fps=120,post_scan_delay_seconds=0,target_detections_per_second=30)
        video.start(state["id"],settings);done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,1,2,3])
        self.assertEqual([s["capture_time_seconds"] for s in done["samples"]],[0,1,2,3])
        self.assertEqual(video.public(done)["actual_video_detection_hz"],1)

    def test_slow_work_never_queues_catchup_scans(self):
        state=self.upload()
        values=iter([0,1.2]*20)
        with patch.object(video,"scan_clock",lambda:next(values)):
            video.start(state["id"],ScanSettings(post_scan_delay_seconds=0,target_detections_per_second=30))
            done=self.wait(state["id"])
        self.assertEqual([s["frame_index"] for s in done["samples"]],[0,6,12])
        self.assertEqual([s["capture_time_seconds"] for s in done["samples"]],[0,1.2,2.4])

    def test_video_start_validates_and_freezes_settings_and_csv(self):
        state=self.upload()
        before=video.directory(state["id"])/"state.json"
        original=before.read_bytes()
        for bad in ({"camera_fps":0},{"camera_fps":121},{"post_scan_delay_seconds":-0.1},
                    {"post_scan_delay_seconds":31},{"target_detections_per_second":0},
                    {"target_detections_per_second":31},{"camera_fps":"30"},{"camera_fps":True},{"camera_fps":float("nan")},{"target_detections_per_second":float("inf")},{"extra":1}):
            self.assertEqual(self.call("/"+state["id"]+"/start","POST",json.dumps(bad).encode(),
                {"Content-Type":"application/json"})[0],422)
            self.assertEqual(before.read_bytes(),original)
        chosen={"camera_fps":2.0,"post_scan_delay_seconds":0.25,"target_detections_per_second":10.0}
        status,raw,_=self.call("/"+state["id"]+"/start","POST",json.dumps(chosen).encode(),{"Content-Type":"application/json"})
        self.assertEqual(status,200,raw)
        done=self.wait(state["id"])
        self.assertEqual(done["scan_settings"],chosen)
        frozen=before.read_bytes()
        self.assertEqual(self.call("/"+state["id"]+"/start","POST",b"{}",{"Content-Type":"application/json"})[0],409)
        self.assertEqual(before.read_bytes(),frozen)
        metadata={r["key"]:r["value"] for r in csv.DictReader(io.StringIO(video.to_csv(done))) if r["record_type"]=="metadata"}
        self.assertEqual(metadata["camera_fps"],"2.0")
        self.assertEqual(metadata["post_scan_delay_seconds"],"0.25")
        self.assertEqual(metadata["target_detections_per_second"],"10.0")
        self.assertEqual(metadata["video_fps"],"5.0")
        self.assertEqual(metadata["sampled_frame_count"],str(done["sampled_frames"]))
        self.assertEqual(float(metadata["actual_video_detection_hz"]),video.public(done)["actual_video_detection_hz"])

    def test_historical_v2_results_are_not_given_new_settings_or_rewritten(self):
        state=self.upload()
        state.update(status="completed",sampled_frames=2,decoded_frames=16,processing_seconds=1.0,
            sampling_method="historical-camera-v2: latest distinct frame after measured scan work + 0.400s; virtual source clock",
            samples=[dict(frame_index=i*2,timestamp_seconds=i*0.4,capture_time_seconds=i*0.4,
                          scan_work_seconds=0,faces=0,matched_people=0,unknown_detections=0) for i in range(2)])
        state.pop("scan_settings",None)
        video._write(state)
        original=(video.directory(state["id"])/"state.json").read_bytes()
        metadata={r["key"]:r["value"] for r in csv.DictReader(io.StringIO(video.to_csv(video.get(state["id"])))) if r["record_type"]=="metadata"}
        self.assertTrue(metadata["sampling_method"].startswith("historical-camera-v2"))
        self.assertEqual(metadata["camera_fps"],"")
        self.assertEqual(metadata["target_detections_per_second"],"")
        self.assertEqual(metadata["actual_video_detection_hz"],"2.5")
        self.assertEqual((video.directory(state["id"])/"state.json").read_bytes(),original)

    def test_per_person_once_per_sample_same_names_unknown_and_csv(self):
        state=self.upload()
        def detector(image):
            seed=round(float(image.mean()))
            codes={0:[0,0,2,2],20:[0,1],40:[],60:[1,2]}.get(seed,[])
            return [SimpleNamespace(embedding=embedding(c)) for c in codes]
        with patch.object(video,"detect",detector):
            video.start(state["id"]); done=self.wait(state["id"])
        self.assertEqual([(p["person_id"],p["detection_count"]) for p in done["people"]],[("a",2),("b",2)])
        self.assertEqual((done["unknown_detections"],done["frames_with_unknown"],done["matched_face_detections"]),(3,2,5))
        status,raw,headers=self.call("/"+state["id"]+"/report.csv")
        self.assertEqual(status,200)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows=list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        people=[r for r in rows if r["record_type"]=="person"]
        metadata={r["key"]:r["value"] for r in rows if r["record_type"]=="metadata"}
        self.assertEqual(len(people),2)
        self.assertEqual({r["detection_count"] for r in people},{"2"})
        self.assertNotEqual(people[0]["identity_key"],people[1]["identity_key"])
        self.assertEqual((people[0]["first_timestamp_seconds"],people[0]["last_timestamp_seconds"]),("0.0","0.4"))
        for key,value in {"sampled_frame_count":"8","sampling_hz":"variable","match_threshold":"0.45",
            "video_duration_seconds_nominal":"3.2","provider":"FakeProvider","partial_results":"False"}.items():
            self.assertEqual(metadata[key],value)
        self.assertNotIn("accuracy",metadata)
        self.assertEqual(headers[b"cache-control"],b"no-store")

    def test_zero_results_and_csv_formula_unicode(self):
        state=self.upload()
        video.start(state["id"]); done=self.wait(state["id"])
        self.assertEqual(done["people"],[])
        self.assertEqual(done["sampled_frames"],8)
        self.assertEqual(len([r for r in csv.DictReader(io.StringIO(video.to_csv(done))) if r["record_type"]=="person"]),0)
        done["people"]=[{"person_id":"a","name":"=SUM(1,2)","detection_count":1,"first_timestamp_seconds":0,"last_timestamp_seconds":0},
            {"person_id":"b","name":"\u0e01\u0e23\u0e23\u0e13","detection_count":2,"first_timestamp_seconds":0,"last_timestamp_seconds":1}]
        rows=list(csv.DictReader(io.StringIO(video.to_csv(done))))
        self.assertEqual([r["name"] for r in rows if r["record_type"]=="person"],["'=SUM(1,2)","\u0e01\u0e23\u0e23\u0e13"])

    def test_cancel_finishes_inflight_frame_preserves_partial_and_blocks_delete(self):
        state=self.upload()
        entered,release=threading.Event(),threading.Event()
        def detector(image):
            entered.set()
            self.assertTrue(release.wait(5))
            return [SimpleNamespace(embedding=embedding(0))]
        with patch.object(video,"detect",detector):
            video.start(state["id"])
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.call("/"+state["id"],"DELETE")[0],409)
            self.assertEqual(self.call("/"+state["id"]+"/start","POST")[0],409)
            self.assertEqual(video.cancel(state["id"])["status"],"cancelling")
            self.assertEqual(self.call("/"+state["id"],"DELETE")[0],409)
            release.set(); done=self.wait(state["id"])
        self.assertEqual(done["status"],"cancelled")
        self.assertEqual(done["sampled_frames"],1)
        self.assertEqual(done["people"][0]["detection_count"],1)
        self.assertTrue(video.public(done)["partial"])
        self.assertEqual(self.call("/"+state["id"]+"/start","POST")[0],409)

    def test_restart_marks_active_and_uploading_interrupted_without_running(self):
        state=self.upload()
        state.update(status="running",sampled_frames=1,processing_seconds=1.25)
        with video._lock: video._write(state)
        uploading=video.allocate("other.avi")
        video._initialized=False
        with patch.object(video,"identity_snapshot",side_effect=AssertionError("Never resume")):
            video.initialize()
            done=video.get(state["id"])
        self.assertEqual(done["status"],"interrupted")
        self.assertEqual(done["sampled_frames"],1)
        self.assertEqual(done["processing_seconds"],1.25)
        self.assertEqual(video.get(uploading["id"])["status"],"interrupted")
        self.assertFalse(video.public(done)["can_start"])
        self.assertTrue(video.public(done)["can_delete"])
        self.assertIsNone(video._active)

    def test_decode_failure_keeps_partial(self):
        state=self.upload()
        original=video.open_capture
        class Truncated:
            def __init__(self,p): self.cap=original(p); self.n=0
            def isOpened(self): return self.cap.isOpened()
            def read(self):
                self.n+=1
                return (False,None) if self.n>6 else self.cap.read()
            def release(self): self.cap.release()
        with patch.object(video,"open_capture",Truncated):
            video.start(state["id"]);done=self.wait(state["id"])
        self.assertEqual(done["status"],"failed")
        self.assertEqual(done["sampled_frames"],3)
        self.assertEqual(done["decode_audit"]["outcome"],"decoder_disagreement")
        self.assertEqual(done["decode_audit"]["verified_frame_count"],16)

    def synthetic_mkv(self, gap=True):
        executable = shutil.which("ffmpeg")
        self.assertIsNotNone(executable, "FFmpeg required for EOF regression tests")
        path = self.root / "synthetic-vfr.mkv"
        args = [executable, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=96x96:rate=15:duration=3"]
        if gap:
            args += ["-vf", "setpts=PTS+if(gte(N\\,20)\\,3/TB\\,0)"]
        args += ["-fps_mode", "passthrough", "-c:v", "ffv1", "-y", str(path)]
        result = subprocess.run(args, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return path.read_bytes()

    def test_real_mkv_vfr_gap_metadata_overestimate_is_clean_eof(self):
        state = self.upload(self.synthetic_mkv(), "gap.mkv")
        self.assertGreater(state["media"]["frame_count"], 45)
        video.start(state["id"]); done = self.wait(state["id"])
        self.assertEqual(done["status"], "completed", done["error"])
        self.assertEqual(done["decoded_frames"], 45)
        self.assertEqual(done["decode_audit"]["outcome"], "normal_eof")
        self.assertGreater(done["decoded_timestamp_max_gap_seconds"], 3)
        self.assertGreater(done["decoded_timestamp_last_seconds"], 5.8)
        visible = video.public(done)
        self.assertEqual(visible["verified_total_frames"], 45)
        self.assertFalse(visible["progress_is_estimate"])
        self.assertEqual(visible["progress_percent"], 100)
        samples = [x["frame_index"] for x in done["samples"]]
        self.assertEqual(len(samples), len(set(samples)))
        self.assertEqual(done["scan_settings"], ScanSettings().model_dump())
        metadata = {r["key"]:r["value"] for r in csv.DictReader(io.StringIO(video.to_csv(done))) if r["record_type"]=="metadata"}
        self.assertEqual(metadata["verified_total_frames"], "45")
        self.assertEqual(metadata["reported_frame_count_estimate"], str(state["media"]["frame_count"]))
        self.assertEqual(metadata["decode_outcome"], "normal_eof")
        self.assertEqual(metadata["decoded_frame_count"], "45")
        self.assertEqual(metadata["sampled_frame_count"], str(done["sampled_frames"]))
        self.assertEqual(metadata["camera_fps"], "30.0")

    def test_actual_frames_can_exceed_reported_count_without_failure(self):
        state=self.upload()
        state["media"]["frame_count"]=4
        video._write(state)
        video.start(state["id"]);done=self.wait(state["id"])
        self.assertEqual(done["status"],"completed",done["error"])
        self.assertEqual(done["decoded_frames"],16)
        self.assertEqual(video.public(done)["verified_total_frames"],16)
        self.assertEqual(done["media"]["frame_count"],4,"Keep original estimate")

    def test_real_truncated_mkv_preserves_partial_matches_and_csv(self):
        data=self.synthetic_mkv(gap=False)
        state=self.upload(data[:int(len(data)*0.65)],"truncated.mkv")
        with patch.object(video,"detect",lambda image:[SimpleNamespace(embedding=embedding(0))]):
            video.start(state["id"]);done=self.wait(state["id"])
        self.assertEqual(done["status"],"failed")
        self.assertEqual(done["decode_audit"]["outcome"],"decode_error")
        self.assertGreater(done["sampled_frames"],0)
        self.assertLess(done["decoded_frames"],45)
        self.assertTrue(video.public(done)["partial"])
        self.assertIsNone(video.public(done)["verified_total_frames"])
        self.assertEqual(done["people"][0]["detection_count"],done["sampled_frames"])
        self.assertEqual(done["people"][0]["name"],"Same Name")
        rows=list(csv.DictReader(io.StringIO(video.to_csv(done))))
        people=[r for r in rows if r["record_type"]=="person"]
        self.assertEqual(people[0]["name"],"Same Name")
        self.assertEqual(people[0]["detection_count"],str(done["sampled_frames"]))
        metadata={r["key"]:r["value"] for r in rows if r["record_type"]=="metadata"}
        self.assertEqual(metadata["status"],"failed")
        self.assertEqual(metadata["decode_outcome"],"decode_error")
        self.assertEqual(metadata["verified_total_frames"],"")

    def test_missing_eof_verifier_is_honest_partial_not_corruption_claim(self):
        state=self.upload()
        with patch.object(video.shutil,"which",return_value=None):
            video.start(state["id"]);done=self.wait(state["id"])
        self.assertEqual(done["status"],"failed")
        self.assertEqual(done["decode_audit"]["outcome"],"unverified")
        self.assertIn("unavailable",done["error"])
        self.assertEqual(done["sampled_frames"],8)

    def test_cancel_during_eof_verification_preserves_complete_samples(self):
        state=self.upload()
        entered,release=threading.Event(),threading.Event()
        def verify(*args):
            entered.set(); self.assertTrue(release.wait(5))
            return dict(outcome="cancelled",verified_frame_count=None,error=None)
        with patch.object(video,"verify_eof",verify):
            video.start(state["id"]);self.assertTrue(entered.wait(5))
            current=video.get(state["id"])
            self.assertEqual(current["decode_phase"],"verifying_eof")
            self.assertEqual(current["sampled_frames"],8)
            video.cancel(state["id"]);release.set();done=self.wait(state["id"])
        self.assertEqual(done["status"],"cancelled")
        self.assertEqual(done["sampled_frames"],8)
        self.assertEqual(done["decoded_frames"],16)

    def test_old_failed_results_and_csv_remain_read_only(self):
        state=self.upload()
        state.update(status="failed",sampled_frames=1,decoded_frames=1,unknown_detections=1,
            frames_with_unknown=1,samples=[dict(frame_index=0,timestamp_seconds=0,faces=1,matched_people=0,unknown_detections=1)])
        video._write(state);path=video.directory(state["id"])/"state.json";before=path.read_bytes()
        visible=video.public(video.get(state["id"]))
        self.assertEqual(visible["detected_face_detections"],1)
        self.assertEqual(visible["people"],[])
        self.assertIsNone(visible["verified_total_frames"])
        metadata={r["key"]:r["value"] for r in csv.DictReader(io.StringIO(video.to_csv(state))) if r["record_type"]=="metadata"}
        self.assertEqual(metadata["decode_outcome"],"not_verified_in_original_run")
        self.assertEqual(path.read_bytes(),before)

    def preview_run(self):
        state=self.upload()
        self.index.rebuild([SimpleNamespace(id="a",first_name="Synthetic",last_name="Alpha",participant_id="1",embedding=embedding(0).tobytes()),
            SimpleNamespace(id="b",first_name="Synthetic",last_name="Beta",participant_id="2",embedding=embedding(1).tobytes())])
        def detector(image):
            seed=round(float(image.mean()))
            choices={0:[(0,0.6,(8,12,24,30)),(0,0.8,(10,10,26,28)),(1,0.95,(40,12,56,30))],
                20:[(0,0.9,(12,8,32,40)),(2,1,(42,40,60,62))],40:[(0,0.9,(14,9,34,41))]}.get(seed,[(0,0.7,(8,12,24,30))])
            return [SimpleNamespace(embedding=embedding(code)*score+embedding(2)*((1-score**2)**0.5),bbox=bbox) for code,score,bbox in choices]
        with patch.object(video,"detect",detector):
            video.start(state["id"]);done=self.wait(state["id"])
        self.assertEqual(done["status"],"completed",done["error"])
        return done

    def test_one_strongest_whole_frame_per_identity_with_bbox_timestamp_and_earliest_tie(self):
        done=self.preview_run()
        self.assertEqual([(p["name"],p["detection_count"]) for p in done["people"]],[("Synthetic Alpha",8),("Synthetic Beta",1)])
        self.assertEqual(done["matched_face_detections"],10,"Duplicates count as face detections, once per identity/frame")
        self.assertEqual(done["unknown_detections"],1)
        alpha,beta=done["people"]
        self.assertEqual(alpha["preview"]["frame_index"],2,"Equal later score retains earliest example")
        self.assertEqual(alpha["preview"]["frame_number"],3)
        self.assertEqual(alpha["preview"]["timestamp_seconds"],0.4)
        self.assertEqual(alpha["preview"]["source_timestamp_seconds"],0.4)
        self.assertAlmostEqual(alpha["preview"]["match_score"],0.9,places=6)
        self.assertEqual(alpha["preview"]["bbox"],[12,8,32,40])
        self.assertEqual(beta["preview"]["frame_index"],0)
        images=list((video.directory(done["id"])/"previews").iterdir())
        self.assertEqual(len(images),2)
        self.assertTrue(all(p.suffix==".jpg" for p in images))
        image=cv2.imread(str(video.preview_path(done["id"],video.identity_key(done["id"],"a"))))
        self.assertEqual(image.shape,(64,64,3),"Whole frame, no crop")
        self.assertAlmostEqual(float(image.mean()),20,delta=1)
        people=video.public(done)["people"]
        self.assertEqual([p["name"] for p in people],["Synthetic Alpha","Synthetic Beta"])
        rows=list(csv.DictReader(io.StringIO(video.to_csv(done))))
        self.assertEqual([r["name"] for r in rows if r["record_type"]=="person"],["Synthetic Alpha","Synthetic Beta"])
        self.assertTrue(all(p["preview_available"] for p in people))

    def test_preview_scaling_uses_actual_xy_dimensions_and_unscored_earliest(self):
        state=self.upload();row=dict(person_id="a",name="Synthetic",detection_count=1)
        image=np.full((75,201,3),70,np.uint8);face=SimpleNamespace(bbox=(-10,11,100,100))
        with patch.object(video,"PREVIEW_MAX_SIDE",80):
            video.save_representative(state,row,face,SimpleNamespace(score=None),image,3,0.6)
            video.save_representative(state,row,SimpleNamespace(bbox=(20,20,100,70)),SimpleNamespace(score=None),image,5,1)
        example=row["preview"]
        self.assertEqual((example["width"],example["height"]),(80,30))
        self.assertEqual(example["frame_index"],3)
        self.assertEqual(example["source_bbox"],[0,11,100,75])
        self.assertEqual(example["bbox"],[0,11*30/75,100*80/201,30])
        self.assertIsNone(example["match_score"])
        self.assertIn("earliest",example["selection_rule"])
        self.assertEqual(cv2.imread(str(video.preview_path(state["id"],video.identity_key(state["id"],"a")))).shape,(30,80,3))
        previous=row["preview"].copy()
        video.save_representative(state,row,SimpleNamespace(bbox=(float("nan"),0,10,10)),SimpleNamespace(score=1),image,6,1.2)
        self.assertEqual(row["preview"],previous)

    def test_manual_verdict_separate_from_counts_and_in_person_csv_persists_restart(self):
        done=self.preview_run();key=video.identity_key(done["id"],"a");example=done["people"][0]["preview"]["example_key"]
        path=video.directory(done["id"])/"state.json";original=path.read_bytes()
        for verdict in ("Correct","Incorrect","Unsure","Not reviewed"):
            status,raw,_=self.call("/"+done["id"]+"/people/"+key+"/review","POST",
                json.dumps(dict(verdict=verdict,example_key=example)).encode(),{"Content-Type":"application/json"})
            self.assertEqual(status,200,raw)
            self.assertEqual(json.loads(raw)["people"][0]["manual_verdict"],verdict)
            self.assertEqual(path.read_bytes(),original,"Automatic recognition state is immutable")
            row=next(r for r in csv.DictReader(io.StringIO(video.to_csv(video.get(done["id"])))) if r["record_type"]=="person" and r["name"]=="Synthetic Alpha")
            self.assertEqual(row["manual_verdict"],verdict)
            self.assertEqual(row["detection_count"],"8")
            self.assertEqual(row["preview_frame_number"],"3")
            self.assertIn("shown example only",row["review_meaning"])
        video.review_example(done["id"],key,"Correct",example)
        video._initialized=False
        with patch.object(video,"detect",side_effect=AssertionError("Never reprocess")):
            video.initialize();visible=video.public(video.get(done["id"]))
        self.assertEqual(visible["people"][0]["manual_verdict"],"Correct")
        self.assertEqual(path.read_bytes(),original)
        for data,status in [(dict(verdict="correct",example_key=example),422),
                            (dict(verdict="Correct",example_key="0"*32),409),
                            (dict(verdict="Correct",example_key=example,accuracy=1),422)]:
            self.assertEqual(self.call("/"+done["id"]+"/people/"+key+"/review","POST",json.dumps(data).encode(),{"Content-Type":"application/json"})[0],status)

    def test_preview_and_review_admin_only_and_no_public_path_or_credentials(self):
        done=self.preview_run();visible=video.public(done);person=visible["people"][0];key=person["identity_key"]
        endpoint="/"+done["id"]+"/people/"+key
        image=endpoint+"/preview?example_key="+person["preview"]["example_key"]
        body=json.dumps(dict(verdict="Correct",example_key=person["preview"]["example_key"])).encode()
        self.app.dependency_overrides.clear()
        for path,method in [(image,"GET"),(endpoint+"/review","POST")]:
            self.assertEqual(self.call(path,method,body,{"Content-Type":"application/json"})[0],401)
        self.app.dependency_overrides[get_current_user]=lambda:User(username="viewer",password_hash="x",role="viewer")
        self.assertEqual(self.call(image)[0],403)
        self.assertEqual(self.call(endpoint+"/review","POST",body,{"Content-Type":"application/json"})[0],403)
        self.app.dependency_overrides[get_current_user]=lambda:ADMIN
        status,raw,headers=self.call(image)
        self.assertEqual(status,200);self.assertTrue(raw.startswith(b"\xff\xd8"))
        self.assertEqual(headers[b"cache-control"],b"no-store")
        self.assertEqual(self.call(endpoint+"/preview?example_key="+"0"*32)[0],409)
        self.assertNotIn(str(self.root),json.dumps(visible))
        self.assertNotIn(".jpg",video.to_csv(done))
        video.preview_path(done["id"],key).write_bytes(b"interrupted replacement")
        self.assertEqual(self.call(image)[0],409,"Image fingerprint guards metadata/image consistency")

    def test_review_blocked_while_running_and_delete_removes_only_its_previews_reviews(self):
        done=self.preview_run();keep=self.upload();visible=video.public(done);key=visible["people"][0]["identity_key"];example=done["people"][0]["preview"]["example_key"]
        done["status"]="running";video._write(done)
        with self.assertRaises(video.Busy):video.review_example(done["id"],key,"Correct",example)
        self.assertEqual(self.call("/"+done["id"]+"/people/"+key+"/preview?example_key="+example)[0],409)
        done["status"]="completed";video._write(done);video.review_example(done["id"],key,"Correct",example)
        self.assertTrue((video.directory(done["id"])/"reviews.json").exists())
        video.remove(done["id"])
        self.assertFalse(video.directory(done["id"]).exists())
        self.assertTrue(video.video_path(keep).is_file())

    def test_older_matched_run_preview_unavailable_does_not_reprocess(self):
        state=self.upload();state.update(status="completed",sampled_frames=2,people=[dict(person_id="a",name="Older Name",detection_count=2,first_timestamp_seconds=0,last_timestamp_seconds=1)])
        video._write(state);original=(video.directory(state["id"])/"state.json").read_bytes()
        with patch.object(video,"detect",side_effect=AssertionError("Never reprocess old video")):
            visible=video.public(video.get(state["id"]));person=visible["people"][0]
            self.assertFalse(person["preview_available"])
            self.assertEqual(person["preview_message"],"Preview unavailable for this older run")
            self.assertEqual(person["manual_verdict"],"Not reviewed")
            self.assertEqual(self.call("/"+state["id"]+"/people/"+person["identity_key"]+"/preview?example_key="+"0"*32)[0],404)
            self.assertEqual((video.directory(state["id"])/"state.json").read_bytes(),original)

    def test_delete_only_selected_experiment_and_traversal_rejected(self):
        drop=self.upload()
        keep=self.upload()
        sentinel=self.root/"other-storage"/"sentinel"
        sentinel.parent.mkdir();sentinel.write_text("keep")
        self.assertEqual(self.call("/"+drop["id"],"DELETE")[0],200)
        self.assertFalse(video.directory(drop["id"]).exists())
        self.assertTrue(video.directory(keep["id"]).exists())
        self.assertEqual(sentinel.read_text(),"keep")
        for job_id in ("../other-storage","not-an-id","0"*32):
            with self.assertRaises(video.NotFound): video.get(job_id)
        self.assertEqual(self.call("/"+drop["id"]+"/video")[0],404)

    def test_all_routes_admin_only_and_media_not_public(self):
        state=self.upload()
        paths=[("", "GET"),("?filename=test.avi","POST"),("/"+state["id"],"GET"),("/"+state["id"],"DELETE"),
            ("/"+state["id"]+"/start","POST"),("/"+state["id"]+"/cancel","POST"),
            ("/"+state["id"]+"/video","GET"),("/"+state["id"]+"/report.csv","GET")]
        self.app.dependency_overrides.clear()
        for path,method in paths:
            self.assertEqual(self.call(path,method)[0],401)
        self.app.dependency_overrides[get_current_user]=lambda: User(username="viewer",password_hash="x",role="viewer")
        for path,method in paths:
            self.assertEqual(self.call(path,method)[0],403)
        self.app.dependency_overrides[get_current_user]=lambda:ADMIN
        self.assertEqual(self.call("/"+state["id"]+"/video")[0],200)
        public=json.loads(self.call("/"+state["id"])[1])
        self.assertNotIn(str(self.root),json.dumps(public))
        self.assertNotIn("person_id",json.dumps(public))

    def test_video_does_not_import_legacy_drive_or_normal_pipeline(self):
        source=inspect.getsource(video)+inspect.getsource(api)
        for forbidden in ("google_drive","legacy_experiment","photo_processing_service","event_pipeline_service",
                          "session.add(","session.commit(","session.delete("):
            self.assertNotIn(forbidden,source)
        root=Path(__file__).resolve().parents[2]
        page=(root/"frontend/src/pages/LocalVideoExperiment.tsx").read_text(encoding="utf-8")
        self.assertIn('onChange={e => { setFile(',page)
        self.assertIn('Start experiment',page)
        self.assertIn('xhr.send(file)',page)

if __name__=="__main__":
    unittest.main()
