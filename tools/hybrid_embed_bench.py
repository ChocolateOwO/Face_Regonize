"""Hybrid face embedding: equivalence + latency, old (per-face) vs hybrid, in
ONE process on the real GPU, interleaved A/B blocks. In-process engine calls
(detect + embed on the LIVE lane, as /api/look runs them); no HTTP, no DB
writes. Results: docs/experiments/2026-11-26-hybrid-embedding.md.

    python tools/hybrid_embed_bench.py --images DIR [--images DIR ...]
        --gallery QA_DB_COPY --out results.json [--blocks 4] [--frames 150]
        [--crowd DIR] [--load-script gpu_load.py]
    python tools/hybrid_embed_bench.py --first-look on|off     (fresh process)

--gallery is a COPY of a QA database (Person.embedding is read, nothing is
written). --load-script runs a separate GPU load process for the contention
blocks (the friend's Voice/LLM stack is not on this PC: simulated contention).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLER_DIR = Path.cwd()  # --out is relative to where the command was run
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT / "backend")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.face_recognition import engine  # noqa: E402

THRESHOLD = 0.45


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)


def summary(ms, embed, seconds):
    return {"n": len(ms), "p50": pct(ms, 50), "p95": pct(ms, 95), "p99": pct(ms, 99),
            "mean": round(statistics.fmean(ms), 2) if ms else None,
            "embed_p50": pct(embed, 50), "embed_p95": pct(embed, 95), "embed_p99": pct(embed, 99),
            "fps": round(len(ms) / seconds, 1) if seconds else None}


def vram_used_mb():
    """Whole-GPU used memory (MiB) from nvidia-smi. Windows (WDDM) does not
    report per-process GPU memory, so deltas are taken on an otherwise idle
    GPU and labelled as whole-GPU numbers."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        return int(out[0]) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class VramPeak:
    def __init__(self):
        self.peak, self._stop = 0, threading.Event()

    def __enter__(self):
        def run():
            while not self._stop.wait(0.5):
                v = vram_used_mb()
                if v:
                    self.peak = max(self.peak, v)
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self.t.join(2)


@contextmanager
def variant(name):
    saved = engine._hybrid["enabled"]
    engine._hybrid["enabled"] = name == "hybrid" and engine._hybrid["session"] is not None
    try:
        yield
    finally:
        engine._hybrid["enabled"] = saved


def look(img):
    t = {}
    t0 = time.perf_counter()
    with engine.use(engine.LIVE, "look"):
        faces = engine.detect_faces(img, t)
    return faces, (time.perf_counter() - t0) * 1000, t.get("embedding_ms", 0.0)


def load_images(dirs):
    out = []
    for d in dirs:
        for p in sorted(Path(d).glob("*.jpg")) + sorted(Path(d).glob("*.png")):
            img = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                out.append((p.name, img))
    return out


def grids(singles, sizes=(3, 4, 6, 8, 9, 12, 16)):
    """Composite frames of N single-face images (the near-threshold set) so the
    hybrid path sees degraded, near-threshold faces too."""
    tiles = [cv2.resize(img, (240, 240)) for _, img in singles]
    out, k = [], 0
    for n in sizes:
        cols = 4 if n > 9 else 3 if n > 4 else n if n <= 4 else 3
        rows = -(-n // cols)
        chosen = [tiles[(k + i) % len(tiles)] for i in range(n)]
        k += n
        chosen += [np.full_like(tiles[0], 255)] * (rows * cols - n)
        grid = np.vstack([np.hstack(chosen[r * cols:(r + 1) * cols]) for r in range(rows)])
        out.append((f"grid_{n}.jpg", grid))
    return out


def gallery(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT participant_id, embedding FROM person WHERE embedding IS NOT NULL").fetchall()
    con.close()
    ids = [r[0] for r in rows]
    mat = np.stack([np.frombuffer(r[1], np.float32) for r in rows]) if rows else np.zeros((0, 512), np.float32)
    return ids, mat


def decide(faces, ids, mat):
    out = []
    for f in faces:
        if len(ids):
            scores = mat @ f.embedding
            i = int(np.argmax(scores))
            s = float(scores[i])
            out.append((ids[i] if s >= THRESHOLD else None, s))
        else:
            out.append((None, 0.0))
    return out


def equivalence(images, ids, mat):
    rows, changed, min_cos, max_dscore, near = [], 0, 1.0, 0.0, 0
    for name, img in images:
        with variant("old"):
            a, _, _ = look(img)
        with variant("hybrid"):
            b, _, _ = look(img)
        same_boxes = len(a) == len(b) and all(x.bbox == y.bbox and x.det_score == y.det_score for x, y in zip(a, b))
        cos = [float(np.dot(x.embedding, y.embedding)) for x, y in zip(a, b)]
        da, db_ = decide(a, ids, mat), decide(b, ids, mat)
        decisions_same = [x[0] for x in da] == [y[0] for y in db_]
        dscore = max((abs(x[1] - y[1]) for x, y in zip(da, db_)), default=0.0)
        near += sum(1 for x in da if abs(x[1] - THRESHOLD) < 0.05)
        changed += 0 if (same_boxes and decisions_same) else 1
        min_cos = min([min_cos] + cos)
        max_dscore = max(max_dscore, dscore)
        rows.append({"image": name, "faces": len(a), "hybrid_path": len(a) >= engine.HYBRID_MIN_FACES,
                     "same_count_order_boxes": same_boxes, "same_decisions": decisions_same,
                     "min_cos": round(min(cos), 7) if cos else None, "max_score_diff": round(dscore, 7),
                     "matched": [x[0] for x in da]})
    return {"images": len(images), "faces": sum(r["faces"] for r in rows),
            "images_on_hybrid_path": sum(1 for r in rows if r["hybrid_path"]),
            "faces_on_hybrid_path": sum(r["faces"] for r in rows if r["hybrid_path"]),
            "near_threshold_faces": near, "changed_images": changed, "min_cos": round(min_cos, 7),
            "max_score_diff": round(max_dscore, 7), "rows": rows}


def scenarios(images):
    by = {}
    for name, img in images:
        with variant("old"):
            n = len(look(img)[0])
        by.setdefault(n, []).append(img)
    def pick(n):
        return by.get(n) or []
    big = [img for n, imgs in by.items() if n > 8 for img in imgs]
    robot_counts = [1, 1, 1, 2, 1, 0, 1, 1, 3, 1]
    rng = random.Random(7)
    crowd_counts = [rng.randint(1, 8) for _ in range(40)]
    def seq(counts):
        out = []
        for i, c in enumerate(counts):
            pool = pick(c) or pick(min(by, key=lambda k: abs(k - c)))
            out.append(pool[i % len(pool)])
        return out
    sc = {f"fixed_{n}": [pick(n)[0]] for n in (0, 1, 2, 3, 8) if pick(n)}
    if big:
        sc["fixed_gt8"] = [big[0]]
    sc["robot_1112101131"] = seq(robot_counts)
    sc["crowd_random_1to8"] = seq(crowd_counts)
    return sc, {k: len(v) for k, v in sorted(by.items())}


def run_block(frames, variant_name, n):
    ms, embed = [], []
    with variant(variant_name):
        t0 = time.perf_counter()
        for i in range(n):
            _, total, e = look(frames[i % len(frames)])
            ms.append(total)
            embed.append(e)
        secs = time.perf_counter() - t0
    return ms, embed, secs


def latency(sc, blocks, frames, contention=False):
    res = {}
    for name, seq in sc.items():
        acc = {"old": ([], [], 0.0), "hybrid": ([], [], 0.0)}
        for b in range(blocks):
            order = ("old", "hybrid") if b % 2 == 0 else ("hybrid", "old")
            for v in order:
                run_block(seq, v, 10)  # settle after the switch, not counted
                ms, e, s = run_block(seq, v, frames)
                a = acc[v]
                acc[v] = (a[0] + ms, a[1] + e, a[2] + s)
        res[name] = {v: summary(*acc[v]) for v in acc}
    return res


def first_look(flag):
    t0 = time.perf_counter()
    engine.get_face_app()
    engine.init_live_lane()
    t_live = time.perf_counter()
    info = engine.init_hybrid(flag)
    t_h = time.perf_counter()
    img = np.zeros((480, 640, 3), np.uint8)
    rows = {}
    for name, frame in (("first_call_blank", img),):
        rows[name] = round(look(frame)[1], 1)
    return {"flag": flag, "model_and_live_lane_ms": round((t_live - t0) * 1000), "init_hybrid_ms": round((t_h - t_live) * 1000),
            "hybrid": info, "gpu_used_mb": vram_used_mb(), **rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", action="append", default=[])
    ap.add_argument("--crowd")
    ap.add_argument("--gallery")
    ap.add_argument("--out")
    ap.add_argument("--blocks", type=int, default=4)
    ap.add_argument("--frames", type=int, default=150)
    ap.add_argument("--load-script")
    ap.add_argument("--first-look", choices=["on", "off"])
    ap.add_argument("--first-look-images", action="append", default=[])
    a = ap.parse_args()

    if a.first_look:
        r = first_look("1" if a.first_look == "on" else "0")
        imgs = load_images(a.first_look_images)
        for name, img in imgs[:2]:
            r[f"first_call_{name}"] = round(look(img)[1], 1)
        print(json.dumps(r))
        return

    t0 = time.perf_counter()
    engine.get_face_app()
    engine.init_live_lane()
    base_vram = vram_used_mb()
    startup = round((time.perf_counter() - t0) * 1000)
    info = engine.init_hybrid("1")
    after_vram = vram_used_mb()
    images = load_images(a.images)
    singles = [(n, i) for n, i in images if n.startswith(("near_", "one_"))]
    images += grids(singles)
    if a.crowd:
        images += [(n, cv2.resize(i, (1280, int(i.shape[0] * 1280 / i.shape[1])))) for n, i in load_images([a.crowd])[:6]]
    ids, mat = gallery(a.gallery) if a.gallery else ([], np.zeros((0, 512), np.float32))

    out = {"gpu": subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                                 capture_output=True, text=True).stdout.strip(),
           "mode": engine.mode_info(), "hybrid": info, "startup_model_live_ms": startup,
           "gpu_used_mb_before_hybrid": base_vram, "gpu_used_mb_after_hybrid": after_vram,
           "gallery_people": len(ids)}
    out["equivalence"] = equivalence(images, ids, mat)
    sc, counts = scenarios(images)
    out["face_count_histogram"] = counts
    with VramPeak() as peak:
        out["latency_alone"] = latency(sc, a.blocks, a.frames)
    out["gpu_used_peak_mb_alone"] = peak.peak
    if a.load_script:
        proc = subprocess.Popen([sys.executable, a.load_script, "100000"], stdout=subprocess.PIPE, text=True)
        try:
            proc.stdout.readline()  # "ready ..."
            time.sleep(3)
            keep = {k: v for k, v in sc.items() if k in ("robot_1112101131", "crowd_random_1to8", "fixed_8")}
            with VramPeak() as peak:
                out["latency_gpu_contention_simulated"] = latency(keep, a.blocks, a.frames)
            out["gpu_used_peak_mb_contention"] = peak.peak
        finally:
            proc.kill()
    (CALLER_DIR / a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "equivalence"}, indent=1))
    eq = {k: v for k, v in out["equivalence"].items() if k != "rows"}
    print(json.dumps(eq, indent=1))


if __name__ == "__main__":
    main()
