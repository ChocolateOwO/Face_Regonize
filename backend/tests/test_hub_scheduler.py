"""Mice Hub GPU leases for background face work (services/hub_scheduler.py and
engine.hold). A local fake of the Hub's scheduler contract (one GPU slot,
token leases, 408 when the wait runs out, expiry) - no real Hub, no model."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.face_recognition import engine  # noqa: E402
from app.services import hub_scheduler  # noqa: E402


class FakeScheduler:
    """One GPU slot like the Hub's LeaseTable: acquire waits up to `wait`
    seconds (408 after), leases expire after ttl, release frees the slot."""

    def __init__(self, ttl=60.0):
        self.cond = threading.Condition()
        self.holder = None
        self.ttl = ttl
        self.n = 0
        self.log = []          # ("acquire"|"release", token, class, owner)
        self.mode = "normal"   # normal | 500 | malformed | slow | disconnect
        self.max_held = 0
        self.held_now = 0

    def acquire(self, klass, owner, wait):
        deadline = time.monotonic() + wait
        with self.cond:
            while self.holder is not None and self.holder[1] > time.monotonic():
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(min(left, 0.05))
            self.n += 1
            token = f"L{self.n}"
            self.holder = (token, time.monotonic() + self.ttl)
            self.log.append(("acquire", token, klass, owner))
            self.held_now = 1
            return token

    def release(self, token):
        with self.cond:
            ok = self.holder is not None and self.holder[0] == token
            if ok:
                self.holder = None
                self.held_now = 0
                self.cond.notify_all()
            self.log.append(("release", token, ok, ""))
            return ok

    def take_slot_for(self, seconds):
        """Something else (speech) holds the GPU for `seconds`."""
        token = self.acquire("speech", "voice", 5)
        threading.Timer(seconds, self.release, args=(token,)).start()
        return token


def handler_for(sched: FakeScheduler):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if sched.mode == "disconnect":
                self.close_connection = True
                return
            if sched.mode == "slow":
                time.sleep(2.0)
            if sched.mode == "500":
                self.send_response(500)
                self.end_headers()
                return
            if self.path.endswith("/release"):
                out, code = {"ok": sched.release(body.get("token"))}, 200
            else:
                token = sched.acquire(body.get("class"), body.get("owner"), float(body.get("wait", 20)))
                out, code = ({"ok": True, "token": token}, 200) if token else ({"ok": False, "error": "busy"}, 408)
                if sched.mode == "malformed":
                    out, code = {"tok": 1}, 200
            raw = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return H


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.sched = FakeScheduler()
        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), handler_for(self.sched))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        env = patch.dict(os.environ, {"MICE_HUB_URL": f"http://127.0.0.1:{self.port}", "MICE_HUB_GPU_LEASE": "1",
                                      "MICE_HUB_LEASE_WAIT": "1"})
        env.start()
        self.addCleanup(env.stop)
        hub_scheduler.reset()
        self.addCleanup(hub_scheduler.reset)

    def background_call(self, workload="event_photo", body=lambda: None):
        with engine.hold(engine.BACKGROUND, workload):
            return body()

    def test_acquire_and_release_around_one_background_call(self):
        seen = []
        self.background_call(body=lambda: seen.append(self.sched.held_now))
        self.assertEqual(seen, [1], "the lease is held during the model call")
        kinds = [(e[0], e[2]) for e in self.sched.log]
        self.assertEqual(kinds, [("acquire", "background"), ("release", True)])
        self.assertEqual(self.sched.held_now, 0)
        self.assertTrue(self.sched.log[0][3].startswith("reconize:event_photo"))

    def test_released_on_error(self):
        with self.assertRaises(RuntimeError):
            self.background_call(body=lambda: (_ for _ in ()).throw(RuntimeError("model failed")))
        self.assertEqual(self.sched.log[-1][:3], ("release", "L1", True))
        self.assertFalse(engine.inference_lock.locked())

    def test_live_calls_never_ask_the_hub(self):
        with engine.hold(engine.LIVE, "look"):
            pass
        with engine.hold(engine.BACKGROUND, "warmup"):
            pass
        self.assertEqual(self.sched.log, [])

    def test_disabled_or_no_hub_never_asks(self):
        for env in ({"MICE_HUB_GPU_LEASE": "0"}, {"MICE_HUB_URL": ""}, {"MICE_HUB_URL": "http://10.0.0.5:8642"}):
            with patch.dict(os.environ, env):
                self.background_call()
        self.assertEqual(self.sched.log, [])

    def test_opted_out_process_never_asks(self):
        hub_scheduler.opt_out("Local Agent")
        self.background_call()
        self.assertEqual(self.sched.log, [])

    def test_busy_hub_408_goes_on_without_a_lease_and_asks_again_next_time(self):
        self.sched.take_slot_for(1.6)
        began = time.monotonic()
        self.background_call()
        self.assertLess(time.monotonic() - began, 1.5, "bounded by MICE_HUB_LEASE_WAIT=1 s")
        self.assertEqual(hub_scheduler.stats()["busy_408"], 1)
        time.sleep(0.7)
        self.background_call()  # asks again (408 is busy, not gone) and now gets the slot
        self.assertEqual(hub_scheduler.stats()["acquired"], 1)

    def test_waits_for_speech_then_runs(self):
        self.sched.take_slot_for(0.3)
        began = time.monotonic()
        self.background_call()
        waited = time.monotonic() - began
        self.assertGreaterEqual(waited, 0.25)
        self.assertEqual(hub_scheduler.stats()["acquired"], 1)

    def test_errors_fail_open_and_back_off(self):
        for mode in ("500", "malformed", "disconnect"):
            with self.subTest(mode=mode):
                hub_scheduler.reset()
                self.sched.mode = mode
                self.background_call()
                st = hub_scheduler.stats()
                self.assertEqual(st["failed_open"], 1)
                self.assertGreater(st["down_for_s"], 0)
                self.background_call()  # not asked again while down
                self.assertEqual(hub_scheduler.stats()["skipped_down"], 1)

    def test_timeout_is_bounded(self):
        self.sched.mode = "slow"
        with patch.dict(os.environ, {"MICE_HUB_LEASE_WAIT": "0"}), patch.object(hub_scheduler, "_post") as post:
            import requests

            post.side_effect = requests.Timeout("read timeout")
            began = time.monotonic()
            self.background_call()
            self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(hub_scheduler.stats()["failed_open"], 1)

    def test_unreachable_hub_fails_open(self):
        with patch.dict(os.environ, {"MICE_HUB_URL": f"http://127.0.0.1:{free_port()}"}):
            began = time.monotonic()
            self.background_call()
            self.assertLess(time.monotonic() - began, 3.0)
        self.assertEqual(hub_scheduler.stats()["failed_open"], 1)

    def test_expired_lease_release_is_harmless(self):
        self.sched.ttl = 0.05
        self.background_call(body=lambda: time.sleep(0.1))
        self.assertEqual(self.sched.log[-1][0], "release")
        self.assertEqual(hub_scheduler.stats()["release_failed"], 0)

    def test_nested_holds_take_one_lease(self):
        with engine.hold(engine.BACKGROUND, "video"):
            with hub_scheduler.lease("inner"):
                pass
        self.assertEqual([e[0] for e in self.sched.log], ["acquire", "release"])

    def test_local_lock_is_free_while_waiting_for_the_hub(self):
        """A live caller (shared mode: same lock) is not blocked by a
        background caller that is still waiting for its lease."""
        self.sched.take_slot_for(0.6)
        t = threading.Thread(target=self.background_call)
        t.start()
        time.sleep(0.15)  # the background call is now waiting for the Hub
        got = engine.inference_lock.acquire(timeout=0.1)
        self.assertTrue(got, "inference lock must not be held while waiting for the Hub")
        engine.inference_lock.release()
        t.join(3)

    def test_concurrent_background_calls_hold_at_most_one_lease(self):
        peak = []

        def body():
            peak.append(sum(1 for e in self.sched.log if e[0] == "acquire") - sum(1 for e in self.sched.log if e[0] == "release" and e[2]))
            time.sleep(0.02)

        threads = [threading.Thread(target=self.background_call, kwargs={"body": body}) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        self.assertEqual(max(peak), 1)
        self.assertEqual(hub_scheduler.stats()["acquired"], 6)
        self.assertEqual(self.sched.held_now, 0)


if __name__ == "__main__":
    unittest.main()
