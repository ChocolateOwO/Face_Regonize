"""/api/health body: liveness plus face-model occupancy.

Top-level fields (all additive except `status`, which is unchanged):

  status             "ok" — the backend answers (unchanged)
  model_ready        the face model is loaded and warmed
  inference_mode     "dual" (separate live session) | "shared" (one session,
                     background yields to live) | "starting"
  device             "cuda" | "cpu" | "tensorrt" | "unknown"
  busy_with          workload holding the model a live frame needs right now
                     ("look", "kiosk", "camera_node", and in shared mode also
                     "event_photo", "video", "import", "enrollment", ...) or null
  live_queue         live frames waiting for their turn
  background_queue   background calls waiting for the background lock
  look_avg_wait_ms   mean wait (turn + lock) of recent successful /api/look
                     calls, null before the first one
  look_capacity      throttling hints: max_live_fps (frames/s the live model
                     sustains at the measured time per frame), active_sources
                     (in the last 10 s), recommended_fps_per_source (80% of
                     max_live_fps shared by them), queue/inflight and limits

`inference` holds the detail (reason for the mode, per-lane holders, live
scheduler counters, /api/look latency percentiles over the last 512 calls,
background yield totals, Mice Hub GPU lease counters). Only counters and workload labels: no camera
names, people, images or embeddings.
"""
from __future__ import annotations

from app.face_recognition import engine
from app.services import hub_scheduler, inference_priority, live_scheduler, look_service


def snapshot() -> dict:
    eng = engine.status()
    live = live_scheduler.scheduler.snapshot()
    look = live_scheduler.look_stats.snapshot()
    busy = eng["live_busy_with"] or live["busy_with"]
    return {
        "status": "ok",
        "model_ready": engine.is_ready(),
        "inference_mode": eng["mode"],
        "device": eng["device"],
        "busy_with": busy,
        "live_queue": live["queue"],
        "background_queue": eng["background_waiting"],
        "look_avg_wait_ms": look["avg_wait_ms"],
        # Throttling hints for /api/look clients (robot hub F5). In memory only.
        "look_capacity": {
            **live_scheduler.scheduler.capacity(),
            **look_service.inflight(),
            "frame_deadline_ms": int(look_service.LOCK_TIMEOUT_S * 1000),
            "max_batch": look_service.MAX_BATCH,
        },
        "inference": {
            "mode_reason": eng["reason"],
            "live_provider": eng["live_provider"],
            "background_provider": eng["background_provider"],
            "live_session_vram_mb": eng["live_vram_mb"],
            "live_busy_with": busy,
            "background_busy_with": eng["background_busy_with"],
            "typical_call_ms": eng["typical_call_ms"],
            "live": live,
            "look": look,
            "priority": inference_priority.stats(),
            "hub_gpu_lease": hub_scheduler.stats(),
            "hybrid_embed": eng.get("hybrid_embed"),
        },
    }
