"""Live-vs-background inference load test on the REAL model (measurement tool).

Runs in one process against a backend code tree (no HTTP server, no database,
no storage writes): loads the real InsightFace model, enrolls synthetic face
crops into an in-memory index, then for --seconds runs at the same time:

  live      --cams Mice cameras calling look_service.look() at 4 frames/s
            each, one request in flight per camera (dropped on 429/503);
            one photobooth camera at 2/s through the same /api/look path;
            one kiosk and one camera node at 2/s through the station path
            (new code: live_scheduler.live_turn + detect_faces; legacy code:
            plain detect_faces, as the kiosk did before);
  busy      (--scenario busy) Event Photo tiled scans of 3600x2400 crowd
            photos in a loop, and a tagged-video style tiled 1080p detector
            on its own CUDA video session, as the video scheduler runs it.

--backend points at the code tree to measure, so the SAME harness measures
the old code (a copy without the lanes: --mode legacy) and the new code
(--mode dual or --mode shared). Results go to --out as JSON.

Not a guarantee: numbers depend on this PC's state at that moment. Run the
variants interleaved and compare only runs taken together.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    k = min(len(v) - 1, max(0, int(round(p / 100 * len(v) + 0.5)) - 1))
    return round(v[k], 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", type=Path, required=True)
    ap.add_argument("--mode", choices=["dual", "shared", "legacy"], required=True)
    ap.add_argument("--scenario", choices=["live", "busy"], default="busy")
    ap.add_argument("--seconds", type=float, default=45)
    ap.add_argument("--cams", type=int, default=3)
    ap.add_argument("--faces", type=Path, required=True)
    ap.add_argument("--crowd", type=Path, required=True)
    ap.add_argument("--no-video", action="store_true", help="busy scenario without the video loop")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    backend = args.backend.resolve()
    sys.path.insert(0, str(backend))
    os.chdir(backend)
    if args.mode != "legacy":
        os.environ["RECONIZE_INFERENCE_MODE"] = args.mode

    import cv2
    import numpy as np
    import psutil

    from app.face_recognition import engine
    from app.face_recognition.index import RecognitionIndex
    from app.services import look_service, video_devices
    from app.services import inference_priority
    from app.services.event_photo_detection_service import detect_event_faces

    has_lanes = hasattr(engine, "init_live_lane")
    if (args.mode == "legacy") == has_lanes:
        raise SystemExit(f"--mode {args.mode} does not match the code at {backend} (lanes={has_lanes})")
    live_scheduler = None
    if has_lanes:
        from app.services import live_scheduler

    t_load = time.perf_counter()
    engine.get_face_app()
    mode_info = engine.init_live_lane(args.mode) if has_lanes else {"mode": "legacy (one session, one lock)"}
    load_s = time.perf_counter() - t_load

    # Enroll the synthetic faces (background lane), patch them into /api/look.
    crops = [cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR) for p in sorted(args.faces.glob("face*.png"))]
    people = []
    for i, crop in enumerate(crops):
        faces = engine.detect_faces(crop)
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        people.append(type("P", (), dict(id=f"p{i}", first_name=f"Face{i}", last_name="", participant_id=f"90{i:02d}",
                                         embedding=best.embedding.tobytes())))
    index = RecognitionIndex()
    index.rebuild(people)
    look_service.recognition_index = index
    if hasattr(look_service, "address_for"):
        look_service.address_for = lambda session, ids: {}
    else:
        look_service.call_as_for = lambda session, ids: {}

    rng = random.Random(5)

    def scene(n):
        frame = np.full((480, 640, 3), 90, np.uint8)
        for k in range(n):
            c = cv2.resize(rng.choice(crops), (150, 190))
            x = 40 + k * 300
            frame[120:310, x:x + 150] = c
        return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()

    frames = [scene(1 + i % 2) for i in range(16)]
    decoded = [cv2.imdecode(np.frombuffer(f, np.uint8), cv2.IMREAD_COLOR) for f in frames]

    stop_at = time.perf_counter() + args.seconds
    lock = threading.Lock()
    records: list[dict] = []

    def record(**kw):
        with lock:
            records.append(kw)

    def look_camera(name: str, period: float):
        i = 0
        while time.perf_counter() < stop_at:
            begin = time.perf_counter()
            data = frames[i % len(frames)]
            i += 1
            try:
                if has_lanes:
                    out = look_service.look(data, source=name, user_id="loadtest")
                else:
                    out = look_service.look(data)
                status = 200
                matched = sum(1 for f in out["faces"] if f["status"] == "matched")
            except look_service.LookError as exc:
                status, matched = exc.status, 0
            ms = (time.perf_counter() - begin) * 1000
            record(source=name, kind="look", t=begin, ms=ms, status=status, matched=matched)
            time.sleep(max(period - (time.perf_counter() - begin), 0.0))

    def station(name: str, workload: str, period: float):
        i = 0
        while time.perf_counter() < stop_at:
            begin = time.perf_counter()
            img, _ = engine.resize_for_detection(decoded[i % len(decoded)])
            i += 1
            status = 200
            try:
                if has_lanes:
                    with live_scheduler.live_turn(source=name, workload=workload, timeout=30.0, drop=False):
                        engine.detect_faces(img)
                else:
                    engine.detect_faces(img)
            except Exception:  # noqa: BLE001
                status = 503
            ms = (time.perf_counter() - begin) * 1000
            record(source=name, kind=workload, t=begin, ms=ms, status=status, matched=0)
            time.sleep(max(period - (time.perf_counter() - begin), 0.0))

    background = {"event_photos": 0, "event_faces": 0, "event_s": [], "video_frames": 0, "video_s": [], "errors": []}

    def event_photo_loop():
        photos = sorted(args.crowd.glob("crowd_*.jpg"))[:6]
        imgs = [cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR) for p in photos]
        i = 0
        while time.perf_counter() < stop_at:
            begin = time.perf_counter()
            try:
                faces = detect_event_faces(imgs[i % len(imgs)])
            except Exception as exc:  # noqa: BLE001
                background["errors"].append(repr(exc))
                return
            i += 1
            background["event_photos"] += 1
            background["event_faces"] += len(faces)
            background["event_s"].append(time.perf_counter() - begin)

    def video_loop():
        frame = cv2.resize(cv2.imdecode(np.frombuffer(sorted(args.crowd.glob("crowd_*.jpg"))[0].read_bytes(), np.uint8),
                                        cv2.IMREAD_COLOR), (1920, 1080))
        try:
            video_devices.session("cuda")
        except Exception as exc:  # noqa: BLE001
            background["errors"].append(f"video session: {exc!r}")
            return
        while time.perf_counter() < stop_at:
            begin = time.perf_counter()
            inference_priority.yield_to_interactive()  # what the render loop does per frame
            video_devices.detect_tiled(frame, "cuda", {}, inference_priority.yield_to_interactive)
            background["video_frames"] += 1
            background["video_s"].append(time.perf_counter() - begin)

    samples = {"cpu": [], "rss_mb": [], "gpu_util": [], "gpu_mem_mb": []}
    proc = psutil.Process()
    proc.cpu_percent(None)

    def monitor():
        while time.perf_counter() < stop_at:
            samples["cpu"].append(proc.cpu_percent(None) / psutil.cpu_count())
            samples["rss_mb"].append(proc.memory_info().rss / 2**20)
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
                util, mem = (float(x) for x in out.split(","))
                samples["gpu_util"].append(util)
                samples["gpu_mem_mb"].append(mem)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)

    threads = [threading.Thread(target=look_camera, args=(f"mice-{i}", 0.25)) for i in range(args.cams)]
    threads.append(threading.Thread(target=look_camera, args=("photobooth", 0.5)))
    threads.append(threading.Thread(target=station, args=("kiosk:loadtest", "kiosk", 0.5)))
    threads.append(threading.Thread(target=station, args=("node:CAM-LOAD", "camera_node", 0.5)))
    if args.scenario == "busy":
        threads.append(threading.Thread(target=event_photo_loop))
        if not args.no_video:
            threads.append(threading.Thread(target=video_loop))
    threads.append(threading.Thread(target=monitor))
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    def summarize(rows):
        ok = [r["ms"] for r in rows if r["status"] == 200]
        busy = [r for r in rows if r["status"] != 200]
        # longest stretch with no successful answer, measured from a refused request
        longest, streak_start = 0.0, None
        for r in sorted(rows, key=lambda r: r["t"]):
            if r["status"] != 200:
                streak_start = streak_start if streak_start is not None else r["t"]
            elif streak_start is not None:
                longest = max(longest, r["t"] - streak_start)
                streak_start = None
        if streak_start is not None:
            longest = max(longest, rows[-1]["t"] - streak_start)
        return {"requests": len(rows), "ok": len(ok), "refused": len(busy),
                "refused_pct": round(100 * len(busy) / len(rows), 2) if rows else None,
                "ok_per_s": round(len(ok) / wall, 2), "p50_ms": pct(ok, 50), "p95_ms": pct(ok, 95), "p99_ms": pct(ok, 99),
                "max_ms": round(max(ok), 1) if ok else None, "under_300ms_pct": round(100 * sum(1 for x in ok if x <= 300) / len(ok), 1) if ok else None,
                "longest_refused_stretch_s": round(longest, 2)}

    by_source = {}
    for r in records:
        by_source.setdefault(r["source"], []).append(r)
    mice = [r for r in records if r["source"].startswith("mice-")]
    mice_ok = [sum(1 for r in by_source[s] if r["status"] == 200) for s in by_source if s.startswith("mice-")]
    result = {
        "mode": args.mode, "scenario": args.scenario + ("-no-video" if args.no_video else ""), "seconds": round(wall, 1), "cams": args.cams,
        "inference_mode": mode_info, "model_load_s": round(load_s, 1),
        "mice_all": summarize(mice),
        "fairness_mice_min_over_max_ok": round(min(mice_ok) / max(mice_ok), 3) if mice_ok and max(mice_ok) else None,
        "per_source": {s: summarize(rows) for s, rows in sorted(by_source.items())},
        "background": {
            "event_photos": background["event_photos"], "event_faces": background["event_faces"],
            "event_photos_per_min": round(background["event_photos"] / wall * 60, 1),
            "event_photo_median_s": round(statistics.median(background["event_s"]), 2) if background["event_s"] else None,
            "video_frames": background["video_frames"], "video_fps": round(background["video_frames"] / wall, 2),
            "errors": background["errors"],
        },
        "resources": {k: {"avg": round(statistics.mean(v), 1), "max": round(max(v), 1)} for k, v in samples.items() if v},
    }
    if has_lanes:
        from app.services import health_status
        result["health_at_end"] = health_status.snapshot()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("mode", "scenario", "mice_all", "fairness_mice_min_over_max_ok", "background")},
                     ensure_ascii=False))
    os._exit(0)  # do not wait for ORT/CUDA teardown


if __name__ == "__main__":
    main()
