"""8 cameras x 4 fps: single /api/look calls vs /api/look/batch (measurement tool).

In-process against a backend code tree (real model, real GPU, no HTTP server,
no database writes; person addresses are stubbed). Modes:

  single  one thread per camera, look_service.look() every 250 ms, one
          request in flight per camera (the robot hub's pattern)
  batch   one client that sends all cameras' latest frames as ONE
          look_service.look_batch() every 250 ms
  equiv   checks that batch embeddings equal single embeddings (cosine)

    python look_batch_bench.py --backend <backend dir> --mode single|batch|equiv
        --cams 8 --fps 4 --seconds 40 --faces <dir with face*.png> --out result.json

Label every result with the GPU it ran on; numbers from different machines
or moments are not comparable.
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
    return round(v[min(len(v) - 1, max(0, int(round(p / 100 * len(v) + 0.5)) - 1))], 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", type=Path, required=True)
    ap.add_argument("--mode", choices=["single", "batch", "equiv"], required=True)
    ap.add_argument("--cams", type=int, default=8)
    ap.add_argument("--fps", type=float, default=4)
    ap.add_argument("--seconds", type=float, default=40)
    ap.add_argument("--faces", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    backend = args.backend.resolve()
    sys.path.insert(0, str(backend))
    os.chdir(backend)

    import cv2
    import numpy as np

    from app.face_recognition import engine
    from app.face_recognition.index import RecognitionIndex
    from app.services import look_service

    engine.get_face_app()
    mode_info = engine.init_live_lane("auto")
    crops = [cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR) for p in sorted(args.faces.glob("face*.png"))]
    people = []
    for i, crop in enumerate(crops):
        best = max(engine.detect_faces(crop), key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        people.append(type("P", (), dict(id=f"p{i}", first_name=f"F{i}", last_name="", participant_id=f"90{i:02d}",
                                         embedding=best.embedding.tobytes())))
    index = RecognitionIndex()
    index.rebuild(people)
    look_service.recognition_index = index
    look_service.address_for = lambda session, ids: {}
    rng = random.Random(7)

    def scene(n):
        frame = np.full((480, 640, 3), 90, np.uint8)
        for k in range(n):
            frame[120:310, 40 + k * 300:190 + k * 300] = cv2.resize(rng.choice(crops), (150, 190))
        return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()

    frames = [scene(1 + i % 2) for i in range(16)]

    if args.mode == "equiv":
        has_batch = hasattr(engine, "detect_faces_batch")
        imgs = [engine.resize_for_detection(cv2.imdecode(np.frombuffer(f, np.uint8), cv2.IMREAD_COLOR))[0] for f in frames[:8]]
        with engine.use(engine.LIVE, "bench"):
            single = [engine.detect_faces(img) for img in imgs]
            batch = engine.detect_faces_batch(imgs)
        cos = [float(a.embedding @ b.embedding) for s, b_ in zip(single, batch) for a, b in zip(s, b_)]
        result = {"mode": "equiv", "has_batch": has_batch, "faces": len(cos), "min_cosine": round(min(cos), 6),
                  "same_face_counts": [len(s) for s in single] == [len(b) for b in batch]}
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
        os._exit(0)

    period = 1.0 / args.fps
    stop_at = time.perf_counter() + args.seconds
    lock = threading.Lock()
    records = []  # (source, t, ms, status)

    def record(src, t, ms, status):
        with lock:
            records.append((src, t, ms, status))

    def camera(name):
        i = 0
        while time.perf_counter() < stop_at:
            begin = time.perf_counter()
            try:
                look_service.look(frames[i % len(frames)], source=name, user_id="bench")
                status = 200
            except look_service.LookError as exc:
                status = exc.status
            i += 1
            record(name, begin, (time.perf_counter() - begin) * 1000, status)
            time.sleep(max(period - (time.perf_counter() - begin), 0.0))

    def batcher():
        i = 0
        names = [f"cam-{k}" for k in range(args.cams)]
        while time.perf_counter() < stop_at:
            begin = time.perf_counter()
            items = [(frames[(i + k) % len(frames)], names[k]) for k in range(args.cams)]
            i += 1
            try:
                out = look_service.look_batch(items, user_id="bench")
                statuses = [it["status"] for it in out["items"]]
            except look_service.LookError as exc:
                statuses = [exc.status] * args.cams
            ms = (time.perf_counter() - begin) * 1000
            for name, status in zip(names, statuses):
                record(name, begin, ms, status)
            time.sleep(max(period - (time.perf_counter() - begin), 0.0))

    vram = []

    def monitor():
        while time.perf_counter() < stop_at:
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
                used, util = (float(x) for x in out.split(","))
                vram.append((used, util))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)

    threads = [threading.Thread(target=monitor)]
    if args.mode == "single":
        threads += [threading.Thread(target=camera, args=(f"cam-{k}",)) for k in range(args.cams)]
    else:
        threads.append(threading.Thread(target=batcher))
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    ok = [r[2] for r in records if r[3] == 200]
    refused = [r for r in records if r[3] == 429]
    longest, run_start = 0.0, {}
    for src, t, _ms, status in sorted(records, key=lambda r: r[1]):
        if status == 429:
            run_start.setdefault(src, t)
        elif src in run_start:
            longest = max(longest, t - run_start.pop(src))
    per_cam = {}
    for src, _t, _ms, status in records:
        per_cam.setdefault(src, [0, 0])[0 if status == 200 else 1] += 1
    result = {
        "mode": args.mode, "cams": args.cams, "fps_per_cam": args.fps, "seconds": round(wall, 1),
        "inference_mode": mode_info.get("mode"), "device": mode_info.get("device"),
        "frames_offered": len(records), "frames_answered": len(ok), "answered_fps": round(len(ok) / wall, 2),
        "p50_ms": pct(ok, 50), "p95_ms": pct(ok, 95), "p99_ms": pct(ok, 99), "max_ms": round(max(ok), 1) if ok else None,
        "refused_429": len(refused), "refused_pct": round(100 * len(refused) / len(records), 2) if records else None,
        "longest_429_run_s": round(longest, 2),
        "per_camera_answered": {k: v[0] for k, v in sorted(per_cam.items())},
        "vram_used_mb_max": max((v[0] for v in vram), default=None),
        "gpu_util_avg": round(statistics.mean(v[1] for v in vram), 1) if vram else None,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    os._exit(0)


if __name__ == "__main__":
    main()
