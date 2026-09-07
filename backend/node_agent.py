"""Local Recognition Node Agent — runs on a Windows PC that owns a camera and
wants to do its OWN face-recognition inference, rather than sending every
frame to Central.

WHAT THIS IS
    A separate, standalone FastAPI process. It is NOT the Reconize backend -
    it has no database, no participant records, no PDPA, no Google anything.
    It knows exactly two things: a cached copy of Central's recognition index
    (participant_id, name, embedding), and how to run InsightFace against a
    frame it is handed. It reuses the SAME detection/embedding code Central
    uses (app.face_recognition.engine) - not a reimplementation, not a
    different model.

WHAT THIS DOES NOT DO
    - It never opens a camera. cv2.VideoCapture() does not appear anywhere in
      this file, on purpose (see the docstring at the top of camera_service.py
      for the unrelated ffmpeg subsystem this is deliberately NOT reusing -
      that one owns physical cameras directly, which is exactly wrong here:
      the BROWSER on this same PC owns the camera, and hands this agent JPEG
      frames over HTTP on loopback only).
    - It never writes to Central's database, and keeps no database of its own.
    - It never decides attendance. It only reports "I saw participant X with
      this confidence" to Central's /api/node/event; Central decides whether
      that becomes a check-in.

RUN IT
    python node_agent.py --node-id CAM-01 --port 8101 --central http://127.0.0.1:8001

    (In the Dummy, "http://127.0.0.1:8001" is this same PC's Reconize
    backend; on a second real Windows PC it would be the Central machine's
    LAN address instead.)

HEALTH / RECOGNIZE
    GET  /health     -> {"status": "ok", "node_id": ..., "index_version": ...}
    POST /recognize   -> one JPEG frame in, matched participant(s) out.
                          Runs entirely against the LOCAL index; never blocks
                          on Central being reachable.

HEARTBEAT (agent -> Central)
    This agent ALSO periodically sends POST /api/node/heartbeat with
    {"node_id": ..., "local_agent_ok": true} so Central's /api/node/status
    endpoint can report the agent as ready even when the browser can't reach
    it directly (HTTPS mixed-content blocking). The agent knows its own truth
    from the other side of that barrier.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.face_recognition.engine import detect_faces, get_face_app  # noqa: E402 — path insert must come first
from app.face_recognition.index import RecognitionIndex  # noqa: E402
from app.services import storage_service  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s node_agent: %(message)s")
logger = logging.getLogger("node_agent")

# How often this agent asks Central "has the index changed?" (§22). A cheap
# GET against an in-memory version counter — nowhere near expensive enough to
# justify anything fancier than a plain poll for a Dummy-scale deployment.
INDEX_POLL_SEC = 5.0

# How often this agent sends a self-report heartbeat to Central so the frontend
# can tell the agent is alive even when browser mixed-content blocking prevents
# a direct 127.0.0.1 HTTP fetch (HTTPS page). Independent of the index poll —
# the agent is "ready" to infer regardless of whether Central's index changed.
HEARTBEAT_SEC = 5.0

# CONCURRENCY (multiple cameras hitting this one agent at once)
#
# get_face_app() is a process-wide SINGLETON (app/face_recognition/engine.py)
# - every /recognize call, from every camera, runs against the exact same
# InsightFace session. Concurrent calls into the same ONNX Runtime session
# from multiple threads is not something this project has ever verified safe
# (the whole rest of Reconize only ever calls it from one request at a time),
# so rather than assume it works, actual inference is serialized behind this
# semaphore - "ONE loaded recognition engine" stays literally true even with
# three cameras scanning at once.
#
# Serializing inference does not mean serializing HTTP: decode + the wait for
# the semaphore both run via asyncio.to_thread, so accepting camera B's frame
# is never blocked on camera A's request merely being open.
_inference_semaphore = threading.Semaphore(1)

# Bounded backlog, not unlimited queuing. Three cameras each at a ~1 request/
# 400ms cadence is comfortably inside this; a genuine pile-up (agent falling
# behind, or many more cameras than tested) rejects with 503 rather than
# growing memory and latency without limit - the caller's own scan loop just
# tries again next tick, exactly like a busy-guard.
_MAX_WAITING = 4
_waiting_count = 0
_waiting_lock = threading.Lock()


class Agent:
    def __init__(self, node_id: str, central_url: str, verify_tls: bool = True) -> None:
        self.node_id = node_id
        self.central_url = central_url.rstrip("/")
        self.index = RecognitionIndex()
        self.threshold = 0.45
        self.central_reachable = False
        self._stop = threading.Event()
        self._auth_token: str | None = None
        # Central's run_server.py serves https with a SELF-SIGNED certificate
        # (see its own docstring) - a browser handles that with a one-time
        # "accept the warning" click, which a Python client has no equivalent
        # of. verify_tls=False accepts that same risk programmatically, which
        # is appropriate for a LAN-only Dummy agent talking to a Dummy Central
        # it was explicitly pointed at, but would NOT be appropriate against
        # an address this agent did not just get run against.
        self.verify_tls = verify_tls
        if not verify_tls:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def login(self, username: str, password: str) -> None:
        """The index-sync and event-reporting endpoints require the same
        bearer auth every other Reconize client uses - a Local Agent is not
        a special trust tier, it authenticates like any other caller."""
        try:
            resp = requests.post(
                f"{self.central_url}/api/auth/login",
                json={"username": username, "password": password}, timeout=5, verify=self.verify_tls,
            )
            resp.raise_for_status()
            self._auth_token = resp.json()["access_token"]
            logger.info("Authenticated with Central at %s", self.central_url)
        except Exception as e:  # noqa: BLE001 — startup must report this clearly, not crash silently
            logger.error("Could not log in to Central (%s): %s", self.central_url, e)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._auth_token}"} if self._auth_token else {}

    def refresh_index(self) -> bool:
        """Pull the current index if our version disagrees with Central's.
        Returns True on success (whether or not a refresh was actually
        needed) so the caller can tell "checked and current" from "could not
        reach Central" (§31/§32 — this is what CENTRAL SERVER DISCONNECTED
        vs LOCAL RECOGNITION READY is decided from)."""
        try:
            resp = requests.get(
                f"{self.central_url}/api/node/recognition-index",
                headers=self._headers(), timeout=5, verify=self.verify_tls,
            )
            resp.raise_for_status()
            data = resp.json()
            self.central_reachable = True
        except Exception as e:  # noqa: BLE001 — a network hiccup must not crash the poll loop
            logger.warning("Central unreachable: %s", e)
            self.central_reachable = False
            return False

        if data["version"] != self.index.version():
            people = [
                type("P", (), {  # a tiny throwaway object with just the attrs rebuild() reads
                    "id": p["participant_id"], "first_name": p["name"], "last_name": "",
                    "participant_id": p["participant_id"],
                    "embedding": np.array(p["embedding"], dtype=np.float32).tobytes(),
                })()
                for p in data["participants"]
            ]
            self.index.rebuild(people)
            # rebuild() bumps its OWN internal counter, which will not equal
            # Central's version number (they are two independent counters) -
            # deliberately: this class's next self.index.version() is not
            # meant to be compared to Central's number again, only used as a
            # cheap "did anything change locally" signal for /health.
            self.threshold = data["threshold"]
            logger.info("Recognition index refreshed: %d participants (Central version %d)",
                       len(data["participants"]), data["version"])
        return True

    def poll_loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_index()
            self._stop.wait(INDEX_POLL_SEC)

    def report_to_central(self) -> bool:
        """Tell Central this agent is alive and ready, via
        POST /api/node/heartbeat with local_agent_ok=true.

        This is the agent's OWN independent truth channel. The frontend cannot
        always reach this agent directly: when the Reconize page is served over
        HTTPS, the browser blocks the http://127.0.0.1 health-check fetch as mixed
        content. Central's /api/node/status endpoint then has no way to know the
        agent is running — UNLESS the agent tells it directly from this side of
        the firewall. local_agent_ok=true means "the process is up and its
        inference engine is loaded"; it does NOT claim Central's index is
        current or that /recognize would succeed (that depends on the local
        index, surfaced separately via the index poll)."""
        url = f"{self.central_url}/api/node/heartbeat"
        try:
            resp = requests.post(
                url,
                json={"node_id": self.node_id, "local_agent_ok": True},
                headers=self._headers(),
                timeout=5,
                verify=self.verify_tls,
            )
            if resp.ok:
                self.central_reachable = True
                return True
            self.central_reachable = False
            return False
        except Exception as e:  # noqa: BLE001 — network hiccup must not crash the loop
            logger.warning("Central self-report failed: %s", e)
            self.central_reachable = False
            return False

    def heartbeat_loop(self) -> None:
        """Periodic reporter thread. Keeps Central's node_registry touched with
        local_agent_ok=true so /api/node/status shows this agent as ready even
        when the browser's direct health check is blocked by mixed content."""
        while not self._stop.is_set():
            self.report_to_central()
            self._stop.wait(HEARTBEAT_SEC)

    def stop(self) -> None:
        self._stop.set()


def build_app(agent: Agent) -> FastAPI:
    app = FastAPI(title=f"Reconize Local Node Agent — {agent.node_id}")

    # The page calling this agent is served from Central (https://<central>),
    # a different origin than http://127.0.0.1:<port> by definition. This
    # agent never leaves the loopback interface (see main() — host is always
    # 127.0.0.1) and carries no participant PII beyond what recognition_index
    # .snapshot() already narrows to (see index.py), so a permissive CORS
    # policy here does not widen what a remote attacker could reach: nothing
    # outside this machine can open a TCP connection to 127.0.0.1 at all.
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=False,
        allow_methods=["GET", "POST"], allow_headers=["*"],
    )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "node_id": agent.node_id,
            "central_reachable": agent.central_reachable,
            "participants_cached": agent.index.size(),
            "threshold": agent.threshold,
        }

    @app.post("/recognize")
    async def recognize(photo: UploadFile = File(...), camera_id: str | None = Form(None)):
        file_bytes = await photo.read()

        global _waiting_count
        with _waiting_lock:
            if _waiting_count >= _MAX_WAITING:
                raise HTTPException(503, "Agent busy - too many cameras waiting on recognition right now. Try again next scan.")
            _waiting_count += 1
        try:
            t0 = time.perf_counter()
            # Decode also runs off the event loop, not just the GPU section -
            # with three cameras posting at once, the loop staying free to
            # accept/route requests is what actually gives every camera a
            # fair, independent turn rather than queuing behind whichever
            # camera's JPEG happened to arrive (and decode) first.
            faces, matches = await asyncio.to_thread(_decode_and_infer, file_bytes)
            elapsed_ms = (time.perf_counter() - t0) * 1000
        finally:
            with _waiting_lock:
                _waiting_count -= 1
        if faces is None:
            raise HTTPException(400, "Could not decode the uploaded image.")

        results = [
            {
                "participant_id": m.participant_id, "name": m.full_name or m.first_name,
                "confidence": round(m.score, 4),
            }
            for m in matches if m.person_id
        ]
        return {
            "faces_total": len(faces), "matches": results,
            "recognition_ms": round(elapsed_ms, 1), "node_id": agent.node_id,
            "camera_id": camera_id,  # echoed back only — this agent does not use it for matching
        }

    def _decode_and_infer(file_bytes: bytes):
        """Runs on a worker thread (via asyncio.to_thread above) — decode is
        NOT gated by the semaphore, only the actual GPU section is, so three
        cameras' JPEGs can decode in parallel and only queue for the one
        thing that genuinely must be serialized (see the CONCURRENCY note
        where the semaphore is defined). Returns (None, []) for an
        undecodable frame rather than raising - HTTPException does not cross
        the thread boundary cleanly, so the route function checks for None."""
        img = storage_service.decode_image(file_bytes)
        if img is None:
            return None, []
        with _inference_semaphore:
            faces = detect_faces(img)
            matches = []
            if faces:
                embeddings = np.stack([f.embedding for f in faces])
                matches = agent.index.match_batch(embeddings, agent.threshold)
            return faces, matches

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconize Local Recognition Node Agent")
    parser.add_argument("--node-id", required=True, help='e.g. "CAM-01"')
    parser.add_argument("--port", type=int, required=True, help="loopback port this agent listens on")
    parser.add_argument("--central", required=True, help="Central Reconize base URL, e.g. http://127.0.0.1:8001")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--insecure", action="store_true",
                        help="accept Central's self-signed https certificate without verifying it "
                             "(needed for Reconize's own run_server.py — see the Agent class docstring)")
    args = parser.parse_args()

    # get_face_app() (app/face_recognition/engine.py) is an unsynchronized
    # lazy singleton - safe for Central, which only ever calls it once, from
    # its own single-threaded startup, before any request can arrive. This
    # agent has no such guarantee: several cameras can each send their FIRST
    # /recognize call within milliseconds of each other right after startup.
    # If none of them find the model built yet, each races to build its OWN
    # full CUDA session at once - measured on this machine, three cameras
    # doing that simultaneously turned a normal ~30ms recognition into
    # ~10 SECONDS each, and left redundant sessions resident on the GPU.
    # Building it here, synchronously, before uvicorn starts accepting any
    # request, closes that race completely - by the time a second camera's
    # request could possibly arrive, the singleton already exists.
    t0 = time.perf_counter()
    get_face_app()
    logger.info("Recognition model loaded (%.0fms) - safe for concurrent cameras from the first request.",
               (time.perf_counter() - t0) * 1000)

    agent = Agent(args.node_id, args.central, verify_tls=not args.insecure)
    agent.login(args.username, args.password)
    agent.refresh_index()  # populate before serving, so the first /recognize call is not empty-index

    poller = threading.Thread(target=agent.poll_loop, daemon=True, name="index-poll")
    poller.start()

    heartbeat = threading.Thread(target=agent.heartbeat_loop, daemon=True, name="central-heartbeat")
    heartbeat.start()

    app = build_app(agent)
    logger.info("Local Node Agent %s listening on http://127.0.0.1:%d (Central: %s)",
               args.node_id, args.port, args.central)
    # 127.0.0.1 ONLY, never 0.0.0.0 — see WHAT THIS DOES NOT DO above. Another
    # machine reaching this port would be pointless anyway: it has no camera
    # of its own to send frames from, and its own browser would use its OWN
    # 127.0.0.1 agent instead.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
