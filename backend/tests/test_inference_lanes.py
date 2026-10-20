"""Live/background inference lanes (2026-10-20, Mice robot request).

Fake models only (no InsightFace load): what is under test is the
scheduling, the locks, the mode decision and the API contract around them.
Real-GPU latency is measured separately (tools/inference_load_test.py).
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException, UploadFile

from app.api import look as look_api
from app.face_recognition import engine
from app.face_recognition.engine import DetectedFace
from app.face_recognition.index import RecognitionIndex
from app.models.models import User
from app.services import (health_status, inference_priority, live_scheduler, local_time, look_service,
                          person_address, settings_cache)
from app.services.live_scheduler import LiveBusy, LiveScheduler

USER = User(id="u1", username="robot-hub", password_hash="x", role="machine")


def unit(i: int) -> np.ndarray:
    v = np.zeros(512, np.float32)
    v[i] = 1.0
    return v


def jpeg(w: int = 320, h: int = 240) -> bytes:
    import cv2
    return cv2.imencode(".jpg", np.full((h, w, 3), 90, np.uint8))[1].tobytes()


class FakeModel:
    """det/rec stand-in. Records which session ran and checks no two calls
    are ever inside the same session at once."""

    def __init__(self, name: str, sleep_s: float = 0.0, faces: int = 0):
        self.name, self.sleep_s, self.faces = name, sleep_s, faces
        self.calls = 0
        self.inside = 0
        self.max_inside = 0
        self._g = threading.Lock()
        model = self

        class Det:
            session = SimpleNamespace(get_providers=lambda: ["CUDAExecutionProvider"])

            def detect(self, img, max_num=0, metric="default"):
                with model._g:
                    model.inside += 1
                    model.max_inside = max(model.max_inside, model.inside)
                    model.calls += 1
                try:
                    time.sleep(model.sleep_s)
                    boxes = np.array([[10, 10, 50, 50, 0.9]] * model.faces, np.float32).reshape(-1, 5)
                    return boxes, None
                finally:
                    with model._g:
                        model.inside -= 1

        class Rec:
            session = SimpleNamespace(get_providers=lambda: ["CUDAExecutionProvider"])

            def get(self, img, face):
                face.embedding = unit(0)

            def get_feat(self, img):
                return np.zeros((1, 512), np.float32)

        self.det_model = Det()
        self.models = {"recognition": Rec()}


class EngineState(unittest.TestCase):
    """Isolates engine module state (live session, mode) per test."""

    def setUp(self):
        self.background = FakeModel("background")
        for target, name, value in [
            (engine, "_face_app", self.background),
            (engine, "_live_app", None),
            (engine, "_mode", dict(engine._mode, mode="starting")),
            (live_scheduler, "scheduler", LiveScheduler()),
            (live_scheduler, "look_stats", live_scheduler.LookStats()),
        ]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.img = np.zeros((64, 64, 3), np.uint8)


# ---------------------------------------------------------------------------
# Fair live scheduling
# ---------------------------------------------------------------------------

class SchedulerTests(unittest.TestCase):
    def hold_first(self, s: LiveScheduler):
        return s.acquire("holder", timeout=1, drop=False)

    def queue(self, s: LiveScheduler, source: str, order: list, timeout=5, drop=False, hold_s=0.0):
        def run():
            try:
                t = s.acquire(source, timeout=timeout, drop=drop)
            except LiveBusy as exc:
                order.append((source, exc.reason))
                return
            order.append(source)
            time.sleep(hold_s)
            s.release(t)
        th = threading.Thread(target=run)
        th.start()
        return th

    def wait_pending(self, s: LiveScheduler, n: int):
        deadline = time.time() + 2
        while s.snapshot()["queue"] < n and time.time() < deadline:
            time.sleep(0.002)
        self.assertEqual(s.snapshot()["queue"], n)

    def test_round_robin_between_sources(self):
        s = LiveScheduler()
        holder = self.hold_first(s)
        order: list = []
        threads = []
        # Busy camera A queues three frames before quiet cameras B and C queue one each.
        for source in ("A", "A", "A", "B", "C"):
            threads.append(self.queue(s, source, order))
            self.wait_pending(s, len(threads))
        s.release(holder)
        for t in threads:
            t.join(5)
        self.assertEqual(order, ["A", "B", "C", "A", "A"], "one turn per source per round")

    def test_quiet_source_is_not_starved_by_a_flood(self):
        s = LiveScheduler(max_pending=64)
        holder = self.hold_first(s)
        order: list = []
        threads = [self.queue(s, "flood", order) for _ in range(20)]
        self.wait_pending(s, 20)
        threads.append(self.queue(s, "quiet", order))
        self.wait_pending(s, 21)
        s.release(holder)
        for t in threads:
            t.join(5)
        self.assertLessEqual(order.index("quiet"), 1, order)

    def test_per_source_bound_for_drop_callers(self):
        s = LiveScheduler(per_source_drop=2)
        holder = self.hold_first(s)
        order: list = []
        threads = [self.queue(s, "cam", order, drop=True) for _ in range(2)]
        self.wait_pending(s, 2)
        with self.assertRaises(LiveBusy) as ctx:
            s.acquire("cam", timeout=1, drop=True)
        self.assertEqual(ctx.exception.reason, "source_busy")
        self.assertGreater(ctx.exception.retry_after, 0)
        # other sources are still admitted
        threads.append(self.queue(s, "other", order, drop=True))
        self.wait_pending(s, 3)
        s.release(holder)
        for t in threads:
            t.join(5)
        self.assertEqual(sorted(order), ["cam", "cam", "other"])

    def test_total_queue_is_bounded(self):
        s = LiveScheduler(max_pending=3)
        holder = self.hold_first(s)
        order: list = []
        threads = [self.queue(s, f"s{i}", order) for i in range(3)]
        self.wait_pending(s, 3)
        with self.assertRaises(LiveBusy) as ctx:
            s.acquire("late", timeout=1, drop=False)
        self.assertEqual(ctx.exception.reason, "queue_full")
        s.release(holder)
        for t in threads:
            t.join(5)
        self.assertEqual(s.snapshot()["queue"], 0)

    def test_timeout_leaves_no_ghost_turn(self):
        s = LiveScheduler()
        holder = self.hold_first(s)
        began = time.perf_counter()
        with self.assertRaises(LiveBusy) as ctx:
            s.acquire("cam", timeout=0.05, drop=True)
        self.assertLess(time.perf_counter() - began, 0.5)
        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertEqual(s.snapshot()["queue"], 0)
        s.release(holder)
        # The timed-out turn is never granted later: the next caller gets the turn at once.
        t = s.acquire("next", timeout=0.05, drop=True)
        s.release(t)
        self.assertEqual(s.snapshot()["expired"], 1)

    def test_holder_release_is_idempotent_and_turns_flow(self):
        s = LiveScheduler()
        t = s.acquire("a", timeout=None, drop=False)
        s.release(t)
        s.release(t)
        t2 = s.acquire("b", timeout=0.01, drop=True)
        s.release(t2)
        self.assertEqual(s.snapshot()["granted"], 2)

    def test_four_cameras_at_four_fps_all_get_served(self):
        """Mice pattern: one request in flight per camera, ~4/s each, 5 ms
        model time -> nothing refused, every camera served."""
        s = LiveScheduler()
        served = {f"cam{i}": 0 for i in range(4)}
        refused = []
        stop = time.perf_counter() + 1.0

        def camera(name):
            while time.perf_counter() < stop:
                try:
                    t = s.acquire(name, timeout=0.5, drop=True)
                except LiveBusy:
                    refused.append(name)
                    continue
                time.sleep(0.005)
                s.release(t)
                served[name] += 1
                time.sleep(0.25)

        threads = [threading.Thread(target=camera, args=(n,)) for n in served]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(refused, [])
        self.assertTrue(all(v >= 3 for v in served.values()), served)

    def test_retry_headers_are_whole_seconds_and_never_zero(self):
        self.assertEqual(look_service.retry_headers(0.0)["Retry-After"], "1")
        self.assertEqual(look_service.retry_headers(0.2), {"Retry-After": "1", "X-Retry-After-Ms": "200"})
        self.assertEqual(look_service.retry_headers(2.4)["Retry-After"], "3")
        self.assertEqual(look_service.retry_headers(1e9)["Retry-After"], str(look_service.RETRY_AFTER_MAX_S))
        self.assertEqual(look_service.retry_headers(None)["Retry-After"], "1")

    def test_estimate_grows_with_the_queue(self):
        s = LiveScheduler()
        idle = s.estimate_wait()
        holder = self.hold_first(s)
        order: list = []
        threads = [self.queue(s, f"s{i}", order) for i in range(5)]
        self.wait_pending(s, 5)
        self.assertGreater(s.estimate_wait(), idle)
        s.release(holder)
        for t in threads:
            t.join(5)


# ---------------------------------------------------------------------------
# Lanes, shared fallback, background yielding
# ---------------------------------------------------------------------------

class LaneTests(EngineState):
    def test_shared_mode_live_uses_the_background_session_and_lock(self):
        self.assertIs(engine.lock_for(engine.LIVE), engine.inference_lock)
        with live_scheduler.live_turn(source="cam", workload="look", timeout=1, drop=True):
            engine.detect_faces(self.img)
        self.assertEqual(self.background.calls, 1)

    def test_dual_mode_live_never_waits_for_a_background_scan(self):
        live = FakeModel("live")
        with patch.object(engine, "_live_app", live):
            self.assertIsNot(engine.lock_for(engine.LIVE), engine.inference_lock)
            with engine.inference_lock:  # a long background scan is running
                began = time.perf_counter()
                with live_scheduler.live_turn(source="cam", workload="look", timeout=0.5, drop=True) as turn:
                    engine.detect_faces(self.img, lock_timeout=turn.remaining())
                self.assertLess(time.perf_counter() - began, 0.2)
        self.assertEqual((live.calls, self.background.calls), (1, 0))

    def test_without_a_live_turn_calls_stay_on_the_background_lane(self):
        live = FakeModel("live")
        with patch.object(engine, "_live_app", live):
            engine.detect_faces(self.img)
        self.assertEqual((live.calls, self.background.calls), (0, 1))

    def test_shared_mode_live_call_times_out_with_an_estimate(self):
        engine.inference_lock.acquire()
        try:
            with self.assertRaises(engine.InferenceBusy) as ctx:
                with live_scheduler.live_turn(source="cam", workload="look", timeout=0.05, drop=True) as turn:
                    engine.detect_faces(self.img, lock_timeout=turn.remaining())
        finally:
            engine.inference_lock.release()
        self.assertIsNotNone(ctx.exception.retry_after)
        self.assertEqual(live_scheduler.scheduler.snapshot()["queue"], 0)

    def test_background_steps_aside_for_a_waiting_live_caller_in_shared_mode(self):
        waited = []
        with inference_priority.interactive():
            began = time.perf_counter()
            with engine.hold(engine.BACKGROUND, "event_photo"):
                waited.append(time.perf_counter() - began)
        self.assertGreaterEqual(waited[0], 0.25, "bounded yield (0.3 s) before taking the shared lock")
        self.assertLess(waited[0], 1.0, "but never indefinitely: background keeps progressing")

    def test_background_does_not_yield_in_dual_mode(self):
        with patch.object(engine, "_live_app", FakeModel("live")):
            with inference_priority.interactive():
                began = time.perf_counter()
                with engine.hold(engine.BACKGROUND, "event_photo"):
                    pass
                self.assertLess(time.perf_counter() - began, 0.1)

    def test_background_progresses_under_continuous_live_load(self):
        stop = threading.Event()

        def live_loop():
            while not stop.is_set():
                with live_scheduler.live_turn(source="cam", workload="look", timeout=1, drop=False):
                    engine.detect_faces(self.img)
                time.sleep(0.001)

        threads = [threading.Thread(target=live_loop) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            done = 0
            began = time.perf_counter()
            while done < 5:
                with engine.hold(engine.BACKGROUND, "event_photo"):
                    done += 1
            self.assertLess(time.perf_counter() - began, 5.0)
        finally:
            stop.set()
            for t in threads:
                t.join(5)

    def test_tiled_event_scan_releases_the_lock_between_calls(self):
        from app.services import event_photo_detection_service as eps
        seen = []
        model = FakeModel("background", faces=1)
        real_detect = model.det_model.detect

        def probe(crop, max_num=0, metric="default"):
            seen.append(engine.inference_lock.locked())
            return real_detect(crop, max_num=max_num, metric=metric)

        model.det_model.detect = probe
        free_between = []
        with patch.object(engine, "_face_app", model), patch.object(engine, "get_face_app", lambda: model):
            stop = threading.Event()

            def watcher():
                while not stop.is_set():
                    if engine.inference_lock.acquire(blocking=False):
                        free_between.append(True)
                        engine.inference_lock.release()
                    time.sleep(0.0005)

            w = threading.Thread(target=watcher)
            w.start()
            model.sleep_s = 0.005
            eps.detect_event_faces(np.zeros((1080, 1920, 3), np.uint8))
            stop.set()
            w.join(5)
        self.assertEqual(len(seen), 9, "global pass + 8 tiles")
        self.assertTrue(all(seen), "every detector call holds the shared lock")
        self.assertTrue(free_between, "the lock was free between tiles")
        self.assertEqual(engine.status()["typical_call_ms"].keys() >= {"event_photo"}, True)

    def test_status_names_the_workload_not_the_camera(self):
        started, release = threading.Event(), threading.Event()

        def bg():
            with engine.hold(engine.BACKGROUND, "event_photo"):
                started.set()
                release.wait(5)

        t = threading.Thread(target=bg)
        t.start()
        started.wait(5)
        try:
            st = engine.status()
            self.assertEqual(st["background_busy_with"], "event_photo")
            self.assertEqual(st["live_busy_with"], "event_photo", "shared mode: live waits behind it")
        finally:
            release.set()
            t.join(5)
        self.assertIsNone(engine.status()["background_busy_with"])


# ---------------------------------------------------------------------------
# Model lifecycle: dual / shared decision
# ---------------------------------------------------------------------------

class ModeTests(EngineState):
    def builder(self, provider):
        self.built.append(provider)
        return FakeModel("live")

    def setUp(self):
        super().setUp()
        self.built = []

    def test_cuda_with_room_goes_dual(self):
        info = engine.init_live_lane("auto", builder=self.builder, free_vram=lambda: 4000)
        self.assertEqual((info["mode"], info["device"]), ("dual", "cuda"))
        self.assertEqual(self.built, ["CUDAExecutionProvider"])
        self.assertTrue(engine.is_dual())

    def test_second_call_does_not_build_again(self):
        engine.init_live_lane("auto", builder=self.builder, free_vram=lambda: 4000)
        engine.init_live_lane("auto", builder=self.builder, free_vram=lambda: 4000)
        self.assertEqual(len(self.built), 1)

    def test_cpu_only_stays_shared_without_building(self):
        cpu = FakeModel("cpu")
        cpu.models["recognition"].session = SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])
        with patch.object(engine, "_face_app", cpu):
            info = engine.init_live_lane("auto", builder=self.builder, free_vram=lambda: None)
        self.assertEqual((info["mode"], info["device"]), ("shared", "cpu"))
        self.assertIn("CPU", info["reason"])
        self.assertEqual(self.built, [])
        self.assertFalse(engine.is_dual())

    def test_not_enough_vram_stays_shared_without_building(self):
        info = engine.init_live_lane("auto", builder=self.builder, free_vram=lambda: 500)
        self.assertEqual(info["mode"], "shared")
        self.assertIn("not enough free GPU memory", info["reason"])
        self.assertEqual(self.built, [])

    def test_cuda_failure_falls_back_and_drops_the_session(self):
        def broken(provider):
            raise RuntimeError("CUDA failure 2: out of memory")
        info = engine.init_live_lane("auto", builder=broken, free_vram=lambda: 4000)
        self.assertEqual(info["mode"], "shared")
        self.assertIn("out of memory", info["reason"])
        self.assertFalse(engine.is_dual())

    def test_partial_init_failure_in_warmup_falls_back(self):
        def half(provider):
            m = FakeModel("live")
            m.models["recognition"].get_feat = lambda img: (_ for _ in ()).throw(RuntimeError("rec model failed"))
            return m
        info = engine.init_live_lane("auto", builder=half, free_vram=lambda: 4000)
        self.assertEqual(info["mode"], "shared")
        self.assertFalse(engine.is_dual())
        self.assertFalse(engine._live_lock.locked())

    def test_too_little_memory_left_after_loading_falls_back(self):
        readings = iter([4000, 300])
        info = engine.init_live_lane("auto", builder=self.builder, free_vram=lambda: next(readings))
        self.assertEqual(info["mode"], "shared")
        self.assertFalse(engine.is_dual())

    def test_forced_shared_and_unknown_values(self):
        self.assertEqual(engine.init_live_lane("shared", builder=self.builder, free_vram=lambda: 9999)["mode"], "shared")
        engine._mode["mode"] = "starting"
        self.assertEqual(engine.init_live_lane("turbo", builder=self.builder, free_vram=lambda: 9999)["mode"], "shared")
        self.assertEqual(self.built, [])

    def test_vram_unreadable_still_tries_and_records_unknown_usage(self):
        info = engine.init_live_lane("auto", builder=self.builder, free_vram=lambda: None)
        self.assertEqual(info["mode"], "dual")
        self.assertIsNone(info["live_vram_mb"])

    def test_background_model_loads_once_under_concurrency(self):
        loads = []

        def slow_load():
            loads.append(1)
            time.sleep(0.05)
            engine._face_app = FakeModel("bg")

        with patch.object(engine, "_face_app", None), patch.object(engine, "_load_background_app", slow_load):
            threads = [threading.Thread(target=engine.get_face_app) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(len(loads), 1)


# ---------------------------------------------------------------------------
# /api/look and /api/health
# ---------------------------------------------------------------------------

class LookApiTests(EngineState):
    def setUp(self):
        super().setUp()
        people = [SimpleNamespace(id="p0", first_name="Ann", last_name="Alpha", participant_id="9001", embedding=unit(0).tobytes())]
        self.index = RecognitionIndex()
        self.index.rebuild(people)
        self.model_sleep = 0.0
        self.faces = [DetectedFace(unit(0), (10.0, 10.0, 60.0, 60.0), 0.9)]
        self.concurrent = 0
        self.max_concurrent = 0
        self.g = threading.Lock()

        def fake_detect(img, timings=None, lock_timeout=None):
            with engine.hold(engine.LIVE if engine._context.get()[0] == engine.LIVE else engine.BACKGROUND,
                             "look", timeout=lock_timeout):
                with self.g:
                    self.concurrent += 1
                    self.max_concurrent = max(self.max_concurrent, self.concurrent)
                time.sleep(self.model_sleep)
                with self.g:
                    self.concurrent -= 1
            return list(self.faces)

        for target, name, value in [
            (look_service, "recognition_index", self.index),
            (look_service.engine, "is_ready", lambda: True),
            (look_service.engine, "detect_faces", fake_detect),
            (look_service, "address_for", lambda session, ids: {"p0": {"call_as": "พี่แอน", "title": "ดร.", "language": "th"}}),
            (look_service.logger, "disabled", True),
        ]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        settings_cache.set_threshold(0.45)

    def call(self, source="cam-1"):
        return look_api.look(photo=UploadFile(io.BytesIO(jpeg()), filename="f.jpg"), source=source, want="faces", user=USER)

    def test_matched_face_carries_title_and_language(self):
        out = self.call()
        face = out["faces"][0]
        self.assertEqual((face["call_as"], face["title"], face["language"]), ("พี่แอน", "ดร.", "th"))
        self.assertIs(out["kept"], False)

    def test_many_cameras_at_once_all_answered_one_inference_at_a_time(self):
        self.model_sleep = 0.01
        results, errors = [], []

        def cam(name):
            for _ in range(5):
                try:
                    results.append(self.call(source=name))
                except HTTPException as exc:
                    errors.append(exc.status_code)

        threads = [threading.Thread(target=cam, args=(f"cam-{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 20)
        self.assertEqual(self.max_concurrent, 1, "the live model runs one call at a time")

    def test_busy_429_has_a_real_retry_after(self):
        self.model_sleep = 0.0
        engine.inference_lock.acquire()  # shared mode: a background call holds the model
        try:
            with self.assertRaises(HTTPException) as ctx:
                self.call()
        finally:
            engine.inference_lock.release()
        exc = ctx.exception
        self.assertEqual(exc.status_code, 429)
        self.assertGreaterEqual(int(exc.headers["Retry-After"]), 1)
        self.assertIn("X-Retry-After-Ms", exc.headers)
        stats = live_scheduler.look_stats.snapshot()
        self.assertEqual(stats["requests_busy"], 1)

    def test_second_frame_from_a_busy_camera_is_dropped_others_admitted(self):
        self.model_sleep = 0.3
        first = threading.Thread(target=self.call, kwargs={"source": "cam-x"})
        first.start()
        time.sleep(0.05)
        second = threading.Thread(target=self.call, kwargs={"source": "cam-x"})
        second.start()
        time.sleep(0.05)
        with self.assertRaises(HTTPException) as ctx:
            self.call(source="cam-x")  # a third frame from the same camera
        self.assertEqual(ctx.exception.status_code, 429)
        first.join(5)
        second.join(5)

    def test_health_schema_and_no_camera_names(self):
        self.call(source="secret-camera-name")
        body = health_status.snapshot()
        for key in ("status", "model_ready", "inference_mode", "device", "busy_with", "live_queue",
                    "background_queue", "look_avg_wait_ms", "inference"):
            self.assertIn(key, body)
        self.assertEqual(body["status"], "ok")
        self.assertIsInstance(body["look_avg_wait_ms"], float)
        for key in ("p50_ms", "p95_ms", "p99_ms", "requests_ok", "requests_busy", "longest_busy_streak_s"):
            self.assertIn(key, body["inference"]["look"])
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("secret-camera-name", text)
        self.assertNotIn("Ann", text)

    def test_health_does_not_wait_for_the_model(self):
        engine.inference_lock.acquire()
        try:
            began = time.perf_counter()
            health_status.snapshot()
            self.assertLess(time.perf_counter() - began, 0.1)
        finally:
            engine.inference_lock.release()

    def test_source_key_is_sanitized_and_falls_back_to_the_account(self):
        self.assertEqual(look_service.source_key("nong cam/1", "u1"), "look:nong_cam_1")
        self.assertEqual(look_service.source_key(None, "u1"), "look:account:u1")
        self.assertEqual(look_service.source_key("", None), "look:account:unknown")


# ---------------------------------------------------------------------------
# One recognition index for both lanes
# ---------------------------------------------------------------------------

class SharedIndexTests(LookApiTests):
    def test_enrollment_import_and_delete_are_seen_by_live_at_once(self):
        self.faces = [DetectedFace(unit(7), (10.0, 10.0, 60.0, 60.0), 0.9)]
        self.assertEqual(self.call()["faces"][0]["status"], "unknown")
        newcomer = SimpleNamespace(id="p7", first_name="Nok", last_name="", participant_id="9007", embedding=unit(7).tobytes())
        self.index.upsert(newcomer)  # what Add Person / import do after commit
        with patch.object(look_service, "address_for", lambda s, ids: {}):
            self.assertEqual(self.call()["faces"][0]["participant_id"], "9007")
            self.index.remove("p7")
            self.assertEqual(self.call()["faces"][0]["status"], "unknown")

    def test_concurrent_reads_during_writes_are_consistent(self):
        index = RecognitionIndex()
        index.rebuild([SimpleNamespace(id=f"p{i}", first_name=f"P{i}", last_name="", participant_id=f"{i:04d}",
                                       embedding=unit(i).tobytes()) for i in range(50)])
        errors, stop = [], threading.Event()

        def reader():
            probe = np.stack([unit(3), unit(40)])
            while not stop.is_set():
                try:
                    out = index.match_batch(probe, 0.45)
                    if out[0].person_id != "p3":
                        errors.append(("p3 lost", out[0]))
                    if out[1].person_id not in ("p40", None):
                        errors.append(("p40 wrong", out[1]))
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def writer():
            for n in range(300):
                index.upsert(SimpleNamespace(id="p40", first_name="P40", last_name="", participant_id="0040",
                                             embedding=unit(40).tobytes()))
                index.remove("p40")
                index.upsert(SimpleNamespace(id=f"x{n}", first_name="X", last_name="", participant_id=f"x{n}",
                                             embedding=unit(100 + n % 300).tobytes()))

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        writer()
        stop.set()
        for t in readers:
            t.join(5)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# title / language / timezone
# ---------------------------------------------------------------------------

class VoiceProfileTests(unittest.TestCase):
    def test_language_tags(self):
        n = person_address.normalize_language
        self.assertEqual(n("TH"), "th")
        self.assertEqual(n("en_us"), "en-US")
        self.assertEqual(n("zh-hant-tw"), "zh-Hant-TW")
        self.assertIsNone(n("  "))
        self.assertIsNone(n(None))
        self.assertEqual(n(" th\n"), "th", "surrounding whitespace is trimmed")
        for bad in ("thai language", "e", "en-", "x" * 40, "t\nh", "ไทย"):
            with self.assertRaises(person_address.CallAsError):
                n(bad)

    def test_title_rules_match_call_as(self):
        n = person_address.normalize_title
        self.assertEqual(n("  ดร.   สมชาย "), "ดร. สมชาย")
        self.assertIsNone(n(""))
        with self.assertRaises(person_address.CallAsError):
            n("a​b")
        with self.assertRaises(person_address.CallAsError):
            n("x" * 41)


class LocalTimeTests(unittest.TestCase):
    def test_naive_local_gets_the_server_offset_without_changing_the_instant(self):
        stored = datetime(2026, 10, 19, 10, 0, 0)
        out = local_time.aware(stored)
        self.assertIsNotNone(out.tzinfo)
        self.assertEqual(out.replace(tzinfo=None), stored, "wall-clock value unchanged")
        self.assertEqual(out.utcoffset(), stored.astimezone().utcoffset())

    def test_aware_and_none_pass_through(self):
        self.assertIsNone(local_time.aware(None))
        aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertIs(local_time.aware(aware), aware)

    def test_filter_bounds_round_trip(self):
        stored = datetime(2026, 10, 19, 10, 0, 0)
        self.assertEqual(local_time.to_stored(local_time.aware(stored)), stored)
        self.assertEqual(local_time.to_stored(stored), stored)
        utc = local_time.aware(stored).astimezone(timezone.utc)
        self.assertEqual(local_time.to_stored(utc), stored)


if __name__ == "__main__":
    unittest.main()
