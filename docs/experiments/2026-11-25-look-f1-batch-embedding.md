# F1: batching face embeddings in `/api/look` (2026-11-25): rejected

**Outcome:** F1 was tried and benchmarked in the Dummy checkout, then reverted there.
It never reached Main, and the production code is unchanged.

It made frames with a fixed number of faces faster. It made a robot-like stream, where the
face count changes from frame to frame, much slower:

- Fixed six-face workload: p95 improved from 202.5 ms to 150.3 ms.
- Robot-like stream: p95 regressed from 158.7 ms to 378.5 ms.

No identity or threshold decision changed on the test set.

This is a response to item F1 of the 2026-11-25 speed-request list.

## What F1 tried

Before F1, `engine.detect_faces()` embeds each detected face with its own recognition-model call:
`rec_model.get(image, face)` once per face.

F1 added an opt-in path used only by `POST /api/look`. It aligns every face of the frame in the
same way `ArcFaceONNX.get()` does, then embeds all of them in **one** `rec_model.get_feat([...])`
call. It stores each raw feature on its face, so normalization, face order and the pairing of
boxes to embeddings stay the same as in the per-face loop.

No other caller was changed: kiosk, enrollment, import, Event Photo and video all kept the loop.
`POST /api/look/batch` was not touched.

## Test setup

| Item | Value |
|---|---|
| GPU | NVIDIA GeForce RTX 4050 Laptop GPU, 6 GB (driver 610.78), on AC power, Windows "Balanced" plan |
| Runtime | Python 3.13.0, onnxruntime 1.29.0 (CUDA EP, `cudnn_conv_algo_search=EXHAUSTIVE`, TF32 on), nvidia-cudnn-cu13 9.25.1.1, insightface 1.0.1 |
| Model | buffalo_l, detection + recognition, det_size 320, threshold 0.45 (unchanged) |
| Inference mode | dual (separate LIVE session for `/api/look`) |
| Server | Dummy backend on 127.0.0.1:8010; isolated copy of the QA database with 6 synthetic people; per-stage timers added by a wrapper script, with no change to app code |
| Client | `requests` with keep-alive on the same PC; latency runs from request sent to full response read |
| Comparison | Original and F1 code run interleaved (original, F1, original, F1), 40 s per scenario per run, after a 15 s warm-up; results pooled over both runs |

Scenarios:

- **Fixed six faces:** 1 client, 10 req/s, one request in flight, the same six-face 640 px frame.
- **0/1/6 mix:** as above, frames cycling between 0, 1 and 6 faces.
- **Robot-like stream:** as above, face counts in the repeating sequence 1,1,1,2,1,0,1,1,3,1.
- **Concurrent GPU work (stand-in):** the fixed six-face workload while a separate process runs buffalo_l
  detection and recognition on 1600 px crowd images nonstop on the same GPU.
- **Saturation:** 4 clients with separate sources, no pause between requests, six-face frame.

## Results (client end-to-end, ms)

| Scenario | Before p50 | Before p95 | F1 p50 | F1 p95 | Answered req/s, before → F1 |
|---|---:|---:|---:|---:|---|
| Fixed six faces | 88.6 | 202.5 | 69.1 | **150.3** | 8.3 → 9.4 |
| 0/1/6 mix | 87.3 | 198.1 | 175.6 | **401.0** | 8.4 → 5.0 |
| **Robot-like stream** | 81.1 | 158.7 | 118.0 | **378.5** | 8.9 → 6.1 |
| Concurrent GPU work (stand-in) | 98.2 | 116.4 | 63.3 | 84.2 | 9.7 → 10.0 |
| Saturation, 4 clients | 210.8 | 302.8 | 151.2 | 255.5 | 17.8 → 24.2 |

A client that runs at 10 req/s with one request in flight sends fewer than 10 requests per second
whenever answers are slow. This is why the answered rate falls in the scenarios that regressed.

The robot-like stream on its own, by face count (p50 / p95, ms):

| Faces in frame | Before | F1 |
|---|---|---|
| 0 | 68.9 / 114.6 | 29.2 / 72.0 |
| 1 | 73.6 / 145.9 | 107.5 / 307.7 |
| 2 | 103.6 / 157.8 | 214.4 / 471.9 |
| 3 | 129.7 / 227.0 | 266.9 / 571.4 |

### Where the time goes

Server-side stage medians for a six-face frame in the fixed six-face scenario (ms):

| Stage | Before | F1 |
|---|---:|---:|
| Before the handler (body receive, multipart parse, auth, thread hop; auth alone 1.5 / 1.7) | 5.2 | 6.6 |
| JPEG header check | 0.2 | 0.2 |
| Decode | 2.9 | 3.0 |
| Wait for a turn and for the model lock | 0.1 | 0.1 |
| Detection | 14.6 | 16.5 |
| Alignment | 4.4 | 3.5 |
| **Embedding** | **50.8** | **31.3** |
| Matching / name lookup / building the face list | 1.2 | 1.8 |
| After the handler (JSON encoding and send) | 0.9 | 1.4 |
| **Server total** | **85.9** | **66.2** |

Some stages could not be split further. Body receive, multipart parsing and the thread hop are
measured only as one figure. The normalization step is inside the handler total. All traffic was on
localhost, so network latency was not part of the test.

### Why the robot-like stream regressed

With this onnxruntime/cuDNN build, a single recognition-model call becomes much slower whenever
its batch size differs from the previous call. Measured on the same LIVE session:

| Batch pattern | ms per call |
|---|---:|
| Always 1 face | 5.6 |
| Always 6 faces | 16.9 (versus about 33 for six calls of 1 face) |
| Always 8 faces | 22.6 |
| Alternating 1 and 6 | 212.3 (size 1) / 150.9 (size 6) |
| Alternating 1 and 2 | 167.1 / 145.3 |
| Random sizes from 1 to 8 | 121–271 |

So whenever the face count changed, the benchmark saw **roughly 120–270 ms of extra embedding
time**. The per-face loop always uses a batch size of 1, so it never pays this cost.

None of the following removed the penalty in standalone sessions:

- `cudnn_conv_algo_search` set to EXHAUSTIVE, HEURISTIC or DEFAULT
- memory pattern
- arena extend strategy
- TF32
- cuDNN max workspace

## Accuracy

The original and F1 code ran on the real model with the same index (6 enrolled synthetic people)
and the threshold of 0.45. The set had 198 images and 210 faces:

- 3 images with no faces
- 9 single-face images, including unknown people
- 10 multi-face images with up to 7 faces
- 176 degraded single-face images (blur, noise, small size, occlusion, rotation, JPEG quality 15,
  darkening), chosen so that 24 faces scored between 0.40 and 0.50

Results:

- **No identity or threshold decision changed.** Face counts, order, boxes and detection scores
  were also identical.
- Embedding cosine between the two paths was at least 0.9999995. The largest change in a best
  match score was 0.00016; the face closest to the threshold scored 0.4475.
- On 4 multi-face frames, the returned `confidence` differed in the 4th decimal.
  Single-face frames were bit-identical.
- Each path gave bit-identical results when run twice.

**Limits of this result:**

- The images are InsightFace's public samples and composites made from them. Real event
  participants and real cameras were not used.
- The index had only 6 people.
- Only 10 faces scored within ±0.02 of the threshold.
- A score drift of 0.00016 is far below the margins seen here, but on other data a face sitting
  right at 0.45 could still flip.

## Decision

- **F1 was rejected** because of the robot-like stream regression (p95 158.7 → 378.5 ms), even
  though recognition decisions were unchanged on the test set.
- **F1 was reverted in the Dummy checkout.** The restored engine and `/api/look` files are
  identical to Main.
- **126 focused tests passed after the revert:**
  - `test_look_api` 19
  - `test_robot_contract` 5
  - `test_inference_lanes` 49
  - `test_robot_history` 12
  - `test_robot_hub_wps` 22
  - `test_inference_api_contract` 8
  - `test_machine_role` 11
- With F1 in place, the full Dummy backend suite had passed (1119/1119).
- **F1 never reached Main.** This document is the only change published about it.

## Limitations and open questions

- **The concurrent GPU work was a stand-in.** The other apps (speech transcription, LLM,
  Photobooth) are not installed on this PC.
  - The stand-in was a second process running face detection and recognition.
  - Under it, the original code was faster than when run alone, probably because the laptop GPU
    stays at higher clocks while busy. The stand-in therefore does not show how real
    contention with the speech stack behaves.
- **Speech-transcription latency was not measured.**
- **Different hardware.** Numbers are from an RTX 4050 Laptop, not the RTX 5070 Ti in the
  original speed report, and the batch-size penalty may differ on other GPU, driver or cuDNN
  versions.
- **Unverified hypothesis about `/api/look/batch`.** That endpoint embeds a variable number of faces
  per call, so it may pay the same batch-size penalty. This was not measured here; it needs its own
  benchmark with face counts that vary per batch before any conclusion or change.
- **Artifacts kept locally.** The measurement scripts, raw results and test images are kept in the
  local Dummy checkout (`patch/2026-11-25_look-f1-batch-embed/`) and are not in this repository.
