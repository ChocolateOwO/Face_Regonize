# Hybrid face embedding: shipped as opt-in, off by default (2026-11-26)

This evaluates the Mice Hub request (P2 #6, `friend_requests_to_send_2026-11-26`) on this project's own PC. The rejected variable-size F1 batching (`2026-11-25-look-f1-batch-embed.md`) stays reverted.

**Decision.** The hybrid ships behind `RECONIZE_HYBRID_EMBED=1`. The default stays the per-face path. It changed no recognition decision and halves crowd-frame latency. However, it makes the robot-like stream's tail slightly worse on this GPU, and the request's acceptance rule keeps the current path in that case.

## What the hybrid does (`backend/app/face_recognition/engine.py`)

**Path by number of faces in the frame:**

| Faces in the frame | Path |
|---|---|
| 0 | no embedding call |
| 1-2 | the existing per-face call on the live session (batch 1), unchanged |
| 3 or more | a second recognition session on the same model file; every call is exactly 8 aligned faces |

For 3 or more faces:

- Faces are aligned exactly as `ArcFaceONNX.get()` does, padded with copies of the last face, and processed in chunks of 8.
- Padded rows are discarded.

**Where it applies:**

- Only the LIVE lane: `/api/look`, the kiosk and the stations.
- With the flag on, `/api/look/batch` follows the same rule, so neither session ever sees a third batch size.
- With the flag off, `/api/look/batch` is unchanged: it still sends a variable batch.
- Background work keeps the per-face loop.

**When it is built:**

- At startup, CUDA only.
- Only when `RECONIZE_HYBRID_VRAM_NEED_MB` (512) plus the live margin (1024) of GPU memory is free.
- It is warmed at batch 8, and batch 1 is warmed again.
- Any failure keeps the per-face path. The reason shows in `/api/health` → `inference.hybrid_embed`.

## Conditions

**Hardware and software:** RTX 4050 Laptop GPU (6 GB), driver 610.78, onnxruntime 1.29.0, buffalo_l, det_size 320, dual mode. These are this PC's own measurements; the RTX 5070 Ti figures in the request are reference only.

**Harness.** `tools/hybrid_embed_bench.py` runs in-process: detect plus embed on the LIVE lane, the way `/api/look` runs it, with no HTTP.

- Old and hybrid are compared in the same process, in interleaved A/B blocks: 4 blocks × 150 frames per variant per scenario, with 10 unmeasured frames after each switch.

**Images (approved QA set only).** 213 images and 282 faces: the F1 set of public InsightFace samples and QA synthetic composites (198 images), 7 grids of 3-16 degraded near-threshold faces, and 6 QA crowd photos.

- Gallery: 6 QA people.
- Faces per frame: 0×22, 1×166, 2×7, 3×4, 4×3, 5×2, 6×5, 7×2, 8×1, 16×1.

**Contention is simulated.** A separate process runs buffalo_l at 640 on 1600 px crowd frames non-stop. The Voice/LLM stack is not on this PC.

**Memory.** GPU memory is the whole-GPU value from nvidia-smi on an otherwise idle GPU, because Windows (WDDM) does not report per-process memory.

## Recognition equivalence (all 213 images, both paths)

| Check | Result |
|---|---|
| Images whose count, order, boxes or detection scores changed | 0 |
| Images whose identity or threshold decision changed | 0 |
| Faces that took the hybrid path | 102 (on 18 images) |
| Faces within ±0.05 of the 0.45 threshold | 38 |
| Lowest embedding cosine, old vs hybrid | 0.9999995 |
| Largest best-score difference | 0.0001028 |

## Latency, ms per frame (detect + embed, LIVE lane), old → hybrid

| Scenario (n = 600 per variant) | p50 | p95 | p99 | embed p95 | frames/s |
|---|---|---|---|---|---|
| 0 faces | 6.2 → 6.3 | 8.2 → 7.3 | 10.3 → 9.2 | 0 → 0 | 155 → 157 |
| 1 face | 12.4 → 12.4 | 14.1 → 14.5 | 17.5 → 17.0 | 6.4 → 6.5 | 79 → 79 |
| 2 faces | 18.6 → 18.6 | 20.6 → 20.4 | 24.7 → 22.7 | 12.8 → 12.8 | 53 → 53 |
| 3 faces | 24.5 → **28.3** | 26.9 → **32.0** | 31.1 → **33.4** | 19.2 → **22.4** | 40 → **35** |
| 8 faces | 55.9 → **29.9** | 60.8 → **32.1** | 68.8 → **35.1** | 52.7 → **23.9** | 18 → **33** |
| 16 faces | 105.9 → **52.7** | 114.1 → **55.5** | 121.2 → **57.6** | 106.1 → **46.9** | 9 → **19** |
| Robot-like 1,1,1,2,1,0,1,1,3,1 | 12.4 → 12.6 | 24.3 → **27.4** | 25.8 → **28.8** | 18.0 → **20.9** | 73 → 71 |
| Crowd, random 1-8 faces | 24.9 → 27.6 | 50.2 → **30.5** | 56.1 → **32.9** | 43.2 → **23.0** | 35 → **43** |

With the simulated GPU contention:

| Scenario | p50 | p95 | p99 | frames/s |
|---|---|---|---|---|
| Robot-like | 26.1 → 26.4 | 48.5 → **56.1** | 53.2 → **60.5** | 36 → 35 |
| Crowd 1-8 | 49.6 → 50.2 | 102.3 → **61.5** | 110.2 → **64.3** | 18 → **22** |
| 8 faces | 108.2 → **57.5** | 117.5 → **62.4** | 122.4 → **64.3** | 9 → **18** |

## Startup, first look and memory

Fresh processes, off/on interleaved, 2 runs each:

| | Off | On |
|---|---|---|
| Model + live lane load | 3.5-3.7 s | 3.7 s |
| Building and warming the batch-8 session | n/a | +0.57-0.63 s |
| Whole-GPU memory after startup | ~1.31 GB | ~1.81 GB (+~0.53-0.57 GB) |
| First 6-face crowd frame | 475-625 ms | 447-486 ms |
| First 1-face frame | 15.6-15.8 ms | 15.3-16.7 ms |

- The first crowd frame is the existing cold first look on a new image size, and it occurs in both variants.
- Peak whole-GPU memory during the runs: 1.85 GB alone, 2.47 GB with the contention process.

## Why it is not the default

- **It costs the robot.** On this GPU one batch-8 call (about 22 ms) costs more than three single-face calls (about 19 ms). A robot frame with 3 faces therefore gets slower, and the robot-like stream's p95 rises by 3 ms alone and by 7.6 ms under contention.
- **The benefit is elsewhere.** It appears from about 4-5 faces per frame: crowd p95 is 50 → 31 ms alone and 102 → 62 ms under contention.
- **It costs memory.** About 0.55 GB more GPU memory, on a 6 GB card that Voice also uses.

**Who should turn it on.** It is worth enabling, with `RECONIZE_HYBRID_EMBED=1` and a restart, on a station whose live frames usually hold several faces, or on a GPU where batch 8 is cheaper relative to batch 1 (the request measured a gain on an RTX 5070 Ti). Measure again after any change of onnxruntime, driver or GPU.

## Reproduce

```
python tools/hybrid_embed_bench.py --images <F1 images> --images <F1 images_var> --crowd <QA event-load-photos> --gallery <COPY of the QA app.db> --out results.json --blocks 4 --frames 150 --load-script gpu_load.py
python tools/hybrid_embed_bench.py --first-look off|on --first-look-images <dir with a 6-face and a 1-face image>
```

- The images and the QA database are local test assets (synthetic people and public samples), not in Git.
