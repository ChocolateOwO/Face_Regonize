# Face Recognition System — Technical Review Report

**Purpose:** Evidence-based review of the current Face Recognition implementation in Reconize, for an independent engineer/AI reviewer to use when recommending further improvements. No code changes were made while producing this report — the system was inspected and exercised (live HTTP requests, one backend restart, one headless browser session) but not modified.

**Method note:** Every claim below is tagged as one of:
- **MEASURED** — captured from a live, running instance of the app during this review.
- **CODE** — verified by direct code reading, not by execution.
- **INFERRED / NOT TESTED** — a plausible consequence of the code that was not actually triggered (e.g. a real DB failure), called out explicitly as such.

Date of testing: 2026-08-19. Machine: the developer's local Windows dev environment (see §6).

---

## 1. Current Architecture — End-to-End Flow

```
Camera (getUserMedia)
  → Frontend capture (canvas.drawImage + toBlob)
  → HTTP POST multipart (fetch)
  → Backend: read bytes → decode → resize → detect → embed → compare → JSON response
  → Frontend render (React state → bottom-left panel / COMPLETE screen)
  → [response already sent] → BackgroundTask: save image, write DB rows
```

| Step | File : Function | What it does | Input → Output | Sync/Async | Blocks recognition response? |
|---|---|---|---|---|---|
| Camera capture | [Recognition.tsx:198](../frontend/src/pages/Recognition.tsx#L198) `captureBlob()` | Draws current `<video>` frame to an offscreen `<canvas>`, encodes as JPEG | `HTMLVideoElement` → `Blob` (JPEG, quality 0.9) | async (canvas.toBlob is callback-based, wrapped in a Promise) | Yes — it's the first step of `scanFrame()` |
| Frontend request | [Recognition.tsx:208](../frontend/src/pages/Recognition.tsx#L208) `scanFrame()` → [client.ts:64](../frontend/src/api/client.ts#L64) `apiPostForm()` | Builds `FormData`, POSTs to `/api/recognition/upload` | `Blob` → `RecognitionResponse` JSON | async `fetch`, no timeout/AbortController | Yes (awaited) |
| Read upload | [recognition.py:90](../backend/app/api/recognition.py#L90) `photo.file.read()` | Reads raw bytes from the multipart upload | `UploadFile` → `bytes` | sync (FastAPI runs `def` handlers in a threadpool) | Yes |
| Image decode | [storage_service.py:13](../backend/app/services/storage_service.py#L13) `decode_image()` | `cv2.imdecode` | `bytes` → `np.ndarray` (BGR) | sync | Yes |
| Resize | [engine.py:79](../backend/app/face_recognition/engine.py#L79) `resize_for_detection()` | Downscales if longest side > 960px (`cv2.INTER_AREA`) | `ndarray` → `(ndarray, scale)` | sync | Yes |
| Face detection | [engine.py:94](../backend/app/face_recognition/engine.py#L94) `detect_faces()` → `app.det_model.detect(...)` | RetinaFace-style detector (InsightFace `buffalo_l` det model) | resized image → bboxes + keypoints | sync (ONNX Runtime inference) | Yes |
| Face embedding | same function, `rec_model.get(image_bgr, face)` in a **Python for-loop, one call per detected face** | Crops/aligns via keypoints, runs the recognition model | face+kps → 512-d L2-normalized embedding | sync, sequential per face | Yes |
| Matching | [recognition.py:107-108](../backend/app/api/recognition.py#L107) → [index.py:72](../backend/app/face_recognition/index.py#L72) `match_batch()` | One matrix multiply of all query embeddings against the entire registered matrix | `(K,512)` embeddings → `K` `MatchResult`s | sync, vectorized (NumPy) | Yes |
| Response built & sent | [recognition.py:113-171](../backend/app/api/recognition.py#L113) | Builds JSON, **schedules** `_persist_recognition` as a `BackgroundTask`, returns | — | sync | N/A (this *is* the response) |
| Frontend render | [Recognition.tsx:223](../frontend/src/pages/Recognition.tsx#L223) `setResult(data)` | React state update → re-render of bottom-left panel + `useEffect` diff for COMPLETE screen | JSON → DOM | async (React scheduling) | No — happens after response received |
| Background persistence | [recognition.py:21](../backend/app/api/recognition.py#L21) `_persist_recognition()` | Saves image+thumbnail to disk, writes `Upload`, `FaceDetection`, `Attendance` rows | bytes + match data → disk + DB rows | runs in FastAPI's `BackgroundTasks` (after response is sent) | **No** — this is the entire point of the "recognize-first, persist-second" design |

**CODE-verified**: nothing between "read upload" and "response built & sent" touches the database or disk. The only DB/disk I/O in the whole recognition path happens inside `_persist_recognition`, which is registered via `background_tasks.add_task(...)` on the line immediately before `return response` ([recognition.py:169-171](../backend/app/api/recognition.py#L169)) — FastAPI guarantees background tasks run strictly after the response is sent to the client.

---

## 2. Face Recognition Algorithm — What the Code Actually Does

- **Detection model**: InsightFace `buffalo_l` pack, detection sub-model only loaded (`allowed_modules=["detection", "recognition"]`, [engine.py:39](../backend/app/face_recognition/engine.py#L39)) — the landmark and gender/age sub-models that ship in the pack are explicitly excluded since nothing in this codebase reads their output.
- **Recognition/embedding model**: the `buffalo_l` recognition sub-model, invoked directly as `app.models["recognition"].get(image_bgr, face)` ([engine.py:113-115](../backend/app/face_recognition/engine.py#L113)) — this is InsightFace's own alignment+crop+embed pipeline (keypoint-based warp done internally by `.get()`), not a custom implementation.
- **Embedding dimension**: 512 — hardcoded in `RecognitionIndex.__init__` (`np.zeros((0, 512), ...)`, [index.py:32](../backend/app/face_recognition/index.py#L32)) and in the DB comment `embedding: bytes  # float32[512] packed with numpy.tobytes()` ([models.py:33](../backend/app/models/models.py#L33)).
- **Reference (registered) embeddings**: generated by the exact same `detect_faces()` function used for live scans — called from [people.py:95](../backend/app/api/people.py#L95) (manual add/edit) and [import_service.py:147](../backend/app/services/import_service.py#L147) (CSV/Excel/Google Drive bulk import). **CODE-confirmed: there is no separate "registration" embedding path** — one function, two call sites.
- **Detected (live-scan) embeddings**: same `detect_faces()`, called from [recognition.py:103](../backend/app/api/recognition.py#L103).
- **Similarity metric**: cosine similarity, implemented as a plain dot product — `face.normed_embedding` ([engine.py:124](../backend/app/face_recognition/engine.py#L124)) is InsightFace's L2-normalized embedding, and `match_batch()` computes `embeddings @ matrix.T` ([index.py:88](../backend/app/face_recognition/index.py#L88)); dot product of unit vectors == cosine similarity.
- **Normalization**: yes — `normed_embedding` (InsightFace-internal L2 normalization) is what's stored (`face.embedding.tobytes()` in people.py/import_service.py) and what's compared. Both sides of every comparison are normalized the same way, since both go through `detect_faces()`.
- **Best-match selection**: `np.argmax(row)` per query face against all registered rows ([index.py:91](../backend/app/face_recognition/index.py#L91)) — pure best-score-wins, no margin/runner-up check (see §15).
- **Threshold**: single global float, default `0.45` ([config.py:34](../backend/app/config.py#L34)), live-tunable via Settings → cached in memory (`settings_cache`), applied as `best_score >= threshold` ([index.py:93](../backend/app/face_recognition/index.py#L93)). **MEASURED**: `/api/recognition/upload` responses in this session all reported `"threshold_used":0.45`.
- **Unknown handling**: if best score < threshold, `MatchResult(None, None, None, None, best_score)` — `person_id=None`, surfaced to the frontend as `status: "unknown"`; still gets a `FaceDetection` DB row (`status="unknown"`) but no `Attendance` row (Attendance is only created `if person_id`, [recognition.py:124](../backend/app/api/recognition.py#L124) / [persist:60](../backend/app/api/recognition.py#L60)).

This all matches the intended "embeddings + cosine similarity" design — nothing in this review found the algorithm/model/metric to differ from what the codebase's own comments describe.

---

## 3. Reference Face / Registration Pipeline

**Registration (manual, `POST /api/people`, [people.py:76](../backend/app/api/people.py#L76)):**
```
uploaded photo → decode_image() → detect_faces()
  → reject if 0 faces (422) or >1 faces (422)
  → save_person_image() [disk]
  → Person(embedding=face.embedding.tobytes(), det_score=face.det_score) [DB]
  → recognition_index.upsert(person) [in-memory]
```

**Registration (bulk import — CSV/Excel/Google Drive, [import_service.py:144-204](../backend/app/services/import_service.py#L144)):** identical steps, just triggered per-row from an `ImportJob` background worker instead of a single HTTP call; same `detect_faces()`, same "reject if 0 or >1 faces" rule, same `recognition_index.upsert()`.

**Live scan (`POST /api/recognition/upload`):**
```
frame → decode_image() → resize_for_detection() → detect_faces() (0..N faces, no rejection)
  → recognition_index.match_batch(embeddings, threshold)
  → per-face MatchResult
```

**Confirmed**: registration and live-scan sides use the *identical* detect→embed function, so there is no train/inference skew between how a reference embedding is produced and how a live-scan embedding is produced — both are literally the same code path. The only structural difference is that registration **requires exactly 1 face** (or refuses to save), while live scan accepts 0..N faces per frame.

---

## 4. Recognition Index

All in [index.py](../backend/app/face_recognition/index.py):

- **Defined**: `class RecognitionIndex`, module-level singleton `recognition_index = RecognitionIndex()` (line 103).
- **Created/loaded**: `recognition_index.rebuild(...)` is called once in `on_startup` ([main.py:62](../backend/app/main.py#L62)), fed every `Person` row from the DB.
- **MEASURED**: `registered_faces_indexed: 9` at the time of this review (via `/api/system/info` and every recognition response's `system.registered_faces_indexed` field).
- **Storage**: one Python list per field (`_ids`, `_first_names`, `_full_names`, `_participant_ids`) plus one `(N, 512)` `float32` NumPy matrix (`_matrix`) — parallel arrays, not a DB-backed structure.
- **Comparison**: vectorized — `sims = embeddings @ matrix.T` is a single BLAS matrix multiply producing a `(K, N)` similarity matrix for K query faces against N registered people in one call, then `np.argmax` per row. **CODE-confirmed**: no Python-level loop over registered people.
- **Add**: `upsert()` — thread-locked, appends to the lists and `np.vstack`s the matrix. Called from `people.py` (create) and `import_service.py` (bulk import). **No restart required.**
- **Edit**: same `upsert()` — if `person.id` already exists in `_ids`, it overwrites that row in place (`self._matrix[i] = emb[0]`) instead of appending. Called from `people.py` `update_person()`.
- **Delete**: `remove()` — `np.delete(self._matrix, i, axis=0)` plus removing from the parallel lists. Called from `people.py delete_person()`.
- **Thread safety**: a single `threading.Lock` guards every read and write (`with self._lock:`). Since FastAPI runs sync `def` route handlers in a threadpool, concurrent requests are real, and this lock is load-bearing.
- **Restart required?** No — `upsert`/`remove` mutate the live singleton in memory immediately; the DB write and index update happen in the same request (people.py) or same background worker (import_service.py), so the very next recognition scan sees the change.

---

## 5. Real Runtime Performance (MEASURED)

All numbers below are from live `curl` requests against the running backend on this machine (`?debug=true`, which returns the server's own instrumented `timings_ms`), 5 runs per scenario, plus a separate headless-browser test for the client-side legs. **`comparison`, `database_read`, `database_write`, and `image_save` are all reported by the server as effectively 0 on the hot path** (comparison genuinely is ~0.2-0.4ms measured; DB/image-save are backgrounded, so the server correctly reports them as 0 contribution to response latency — their real cost is logged separately, see below).

### 1 face (`you_1.jpg`, 8 total runs — 5 pre-restart + 3 immediately post cold-start)

| Run | Detect (ms) | Embed (ms) | Compare (ms) | Server Total (ms) |
|---|---|---|---|---|
| 1 | 27.5 | 424.7 | 0.2 | 518.3 |
| 2 | 29.3 | 214.0 | 0.2 | 318.5 |
| 3 | 38.8 | 526.2 | 0.2 | 653.0 |
| 4 | 35.5 | 265.0 | 0.2 | 378.0 |
| 5 | 24.7 | 293.9 | 0.2 | 398.9 |
| 6 (post cold-start) | 25.5 | 578.4 | 0.2 | 687.5 |
| 7 | 24.8 | 377.9 | 0.2 | 471.3 |
| 8 | 35.2 | 421.3 | 0.2 | 529.7 |

**Server Total — avg 494.4ms, min 318.5ms, max 687.5ms.**

### 3 faces (`composite_3faces.jpg`, synthetic — 2 known + 1 unknown tiled together)

| Run | Detect (ms) | Embed (ms) | Server Total (ms) |
|---|---|---|---|
| 1 | 42.2 | 681.6 | 726.5 |
| 2 | 55.3 | 728.8 | 788.5 |
| 3 | 25.1 | 753.1 | 781.6 |
| 4 | 33.6 | 690.0 | 726.9 |
| 5 | 25.4 | 721.3 | 751.0 |

**Server Total — avg 754.9ms, min 726.5ms, max 788.5ms.** Embed avg ≈715ms → **~238ms/face**.

### 5 faces (`composite_5faces.jpg`, synthetic — 4 known + 1 unknown tiled together)

| Run | Detect (ms) | Embed (ms) | Server Total (ms) |
|---|---|---|---|
| 1 | 50.2 | 950.4 | 1004.9 |
| 2 | 27.9 | 949.1 | 982.3 |
| 3 | 25.5 | 900.7 | 929.8 |
| 4 | 27.3 | 985.9 | 1017.6 |
| 5 | 26.1 | 972.6 | 1004.0 |

**Server Total — avg 987.7ms, min 929.8ms, max 1017.6ms.** Embed avg ≈951.7ms → **~190ms/face**.

> Note: 3/5-face test images are synthetic composites (real face photos tiled into a grid) built for this review since only 2 distinct registered-and-photographed people existed in the test fixtures. They exercise real detection+embedding+matching on real face pixels, but are **not** representative of natural photo composition (faces at a single fixed size/spacing) — flagged so the reviewer doesn't over-generalize from them.

### Client-side legs (MEASURED via headless Chromium hitting the real running backend, bypassing only the `getUserMedia` camera grab — see caveat below)

Two conditions were tested:

**(a) 12MP photo (3024×4032, the raw test fixture) — NOT representative of a webcam frame, included to show the effect of image size:**
Capture/encode 174-455ms, network+overhead 118-159ms, round-trip 555-684ms.

**(b) Resized to 960×1280 (~typical webcam/kiosk resolution) — 5 runs:**

| Run | Capture+Encode (ms) | Network+Overhead (ms) | Server Total (ms) | Round-trip (ms) |
|---|---|---|---|---|
| 1 | 164.2 *(cold JIT, outlier)* | 71.2 | 336.1 | 407.3 |
| 2 | 23.9 | 41.1 | 382.2 | 423.3 |
| 3 | 18.5 | 20.0 | 459.6 | 479.6 |
| 4 | 19.1 | 20.4 | 365.9 | 386.3 |
| 5 | 20.8 | 64.9 | 480.7 | 545.6 |

Frontend render (post-fetch, one `requestAnimationFrame` tick): **0.2-0.4ms in every run** — negligible.

**Caveat (stated plainly, not glossed over):** headless Chromium cannot be granted real camera access in this sandboxed environment (confirmed after several mocking attempts using `canvas.captureStream()`, all unable to deliver a non-zero-dimension video frame — see prior session notes). The numbers above substitute `drawImage(<img>, ...)` for `drawImage(<video>, ...)`, which is the correct proxy for the *encode* cost (identical `canvas.toBlob` call, identical JPEG quality setting) and exercises the *real* network path to the *real* running backend — but it does not measure actual camera frame-grab latency (which is normally near-instant, sub-millisecond, since it's just a GPU-backed texture copy) or real webcam-driver variability. **Real-camera, real-browser verification is still outstanding** and should be done by a human with a physical camera.

### TOTAL user-perceived time (composed from the measurements above, 960×1280 condition, steady state excluding the one warm-up outlier)

| Face count | Capture+Encode | Network+Overhead | Backend Total | Frontend Render | **Estimated TOTAL** |
|---|---|---|---|---|---|
| 1 | ~20ms | ~35ms | ~495ms avg | ~0.3ms | **~550ms avg** (min ~360ms, max ~830ms using backend max) |
| 3 | ~20ms (not separately measured at this size; JPEG encode cost is dominated by resolution, not face count) | ~35ms (assumed, not separately measured for 3/5-face images) | ~755ms avg | ~0.3ms | **~810ms avg** |
| 5 | ~20ms (assumed) | ~35ms (assumed) | ~988ms avg | ~0.3ms | **~1043ms avg** |

All well under the 3-second target under the tested conditions. The 3/5-face "capture+encode"/"network" columns are **extrapolated from the 1-face client measurement**, not independently measured (the composite test images were only exercised via direct backend `curl`, not through the browser) — flagged as an extrapolation, not a fresh measurement.

---

## 6. Hardware / Environment (MEASURED)

| Item | Value |
|---|---|
| OS | Windows 11 Home Single Language, build 10.0.26200 |
| CPU | Intel64 Family 6 Model 186 Stepping 2 (GenuineIntel), 8 physical / 12 logical cores |
| RAM | 23.7 GB total, 11.3 GB available (52.5% used) at test time |
| GPU | NVIDIA GeForce RTX 4050 Laptop GPU, 6141 MiB VRAM (316 MiB used, 38% util at test time) |
| Python (backend venv) | 3.13.0 |
| Node.js | v25.2.1 |
| Browser (automated testing only) | headless Chromium via patchright (Playwright fork) — no real-camera manual browser test done in this review |
| Backend framework | FastAPI 0.141.1, uvicorn 0.52.1 |
| Frontend framework | React ^19.2.8, Vite ^8.2.0, TypeScript ~6.0.2, Tailwind ^4.3.3, react-router-dom ^7.18.2 (per `package.json`; not independently re-verified against `node_modules`) |
| InsightFace | 1.0.1 |
| ONNX Runtime | `onnxruntime-gpu` 1.29.0 (the GPU-capable wheel is installed; there is no separate plain `onnxruntime` package in this venv) |
| Available ONNX providers | `TensorrtExecutionProvider`, `CUDAExecutionProvider`, `CPUExecutionProvider` (i.e., the runtime *can* see CUDA) |
| **Active inference device** | **`CPUExecutionProvider`** — confirmed via `/api/system/info` and the startup log line `"GPU init failed (CUDA provider not active, got ['CPUExecutionProvider']), falling back to CPU"` |

**CPU or GPU inference? — Definitively CPU.** The GPU is physically present and ONNX Runtime enumerates CUDA as available, but `get_face_app()`'s own runtime self-check (`if "CUDAExecutionProvider" not in active: raise RuntimeError`, [engine.py:43-44](../backend/app/face_recognition/engine.py#L43)) catches this and falls back to CPU every time — consistent with the previously-identified missing CUDA Toolkit runtime DLL (`cublasLt64_13.dll`), not re-verified in this pass since it wasn't the review's focus, but the fallback behavior itself is freshly confirmed live.

---

## 7. Model Loading (MEASURED)

- **When loaded**: once, in `on_startup` ([main.py:58](../backend/app/main.py#L58) `get_face_app()`), before the app begins serving. `get_face_app()` itself is a lazy-singleton (`if _face_app is None: ...`) — CODE-confirmed it can only ever run its loading branch once per process lifetime; every subsequent call just returns the cached `_face_app`.
- **Remains in memory**: yes — module-level global, never reset per-request.
- **Reload per request?** No — CODE-confirmed no path calls the loading branch outside the startup event, and MEASURED evidence supports this (no anomalous "first-request-only" spike, see below).

**Cold-start test** (backend process killed and restarted fresh for this review):

- Startup log: `GPU init failed (...) falling back to CPU` → `Model warm-up inference took 29ms` → `Startup complete in 3996ms total (model load + warm-up inference: 3977ms, 9 faces indexed)`.
- Wall-clock from process launch to the health endpoint responding: ~7.2s (includes Python/import startup overhead beyond the in-app 3996ms timer, which only starts once `on_startup` begins running).

| Request | Total server time (ms) |
|---|---|
| 1st (immediately after restart) | 687.5 |
| 2nd | 471.3 |
| 3rd | 529.7 |

**No dramatic first-request penalty** beyond the general run-to-run variance already seen throughout §5 (the 1-face range across all 8 measured runs was 318.5-687.5ms) — consistent with the code's explicit warm-up inference (a throwaway `detect_faces()` call on a blank 320×320 image, [engine.py:60](../backend/app/face_recognition/engine.py#L60)) already having paid ONNX Runtime's first-call kernel-selection cost before the server accepts real traffic.

---

## 8. Database / File I/O — Execution Order (CODE + MEASURED)

**Confirmed order, from reading `recognition.py` top to bottom:**

1. Read upload bytes → decode → resize → detect → embed → match — **no DB/disk touched**
2. Build JSON response
3. `background_tasks.add_task(_persist_recognition, ...)` — registered but not yet run
4. `return response` — **this is what the client receives**
5. *(after the response has been sent)* `_persist_recognition` runs: `storage_service.save_event_image()` (disk) → open new DB session → insert `Upload` + commit → per face: insert `FaceDetection` + commit, then insert `Attendance` (if matched) + commit

So: **database read = never, on this path** (index is in-memory); **database write, image save, attendance update, and "history" (FaceDetection) update all happen strictly AFTER the response**, confirmed both by code structure and by the server log (`background persist: image_save=97ms db_write=30ms (both async — did not add to response latency)`, MEASURED from the live log during this review).

One structural note (**CODE-confirmed**, relevant to §15): `_persist_recognition` commits once per `FaceDetection` insert and again per `Attendance` insert inside a `for` loop, rather than batching all inserts into a single commit — i.e., an event photo with N faces produces up to `1 (Upload) + N (FaceDetection) + M (Attendance, M≤N)` separate commits in the background thread. This doesn't add response latency (it's backgrounded) but is worth a reviewer's attention as a DB-efficiency smell if event photos routinely contain many faces.

---

## 9. Frontend Scan Loop (CODE, cross-checked against §5 measurements)

- **Frame capture**: `canvas.getContext('2d').drawImage(video, 0, 0)` then `canvas.toBlob(resolve, "image/jpeg", 0.9)` — [Recognition.tsx:198-205](../frontend/src/pages/Recognition.tsx#L198).
- **Canvas resolution**: `canvas.width = video.videoWidth; canvas.height = video.videoHeight` — i.e., **whatever the browser's actual camera stream resolution is**. `getUserMedia({ video: { facingMode: "user" } })` sets no explicit `width`/`height`/`frameRate` constraints, so the resolution is left entirely to the browser/OS/webcam driver default — this review could not determine what that will be on the user's actual kiosk hardware (headless testing has no real camera). Flagged as an open variable, not something this review can measure.
- **JPEG quality**: hardcoded `0.9`.
- **Scan interval**: **not** `setInterval` — a self-paced `while (activeRef.current) { await scanFrame(); await sleep(SCAN_DELAY_MS) }` loop ([Recognition.tsx:163-169](../frontend/src/pages/Recognition.tsx#L163)), `SCAN_DELAY_MS = 200`.
- **Can requests overlap?** No — CODE-confirmed by construction: the loop `await`s `scanFrame()` to completion before starting the delay timer for the next iteration, so a second scan literally cannot begin until the first one's fetch has resolved (or thrown).
- **`busyRef`**: does not exist in the current code — an earlier version of this file used a `busyRef` boolean guard together with `setInterval`, but that was removed when the loop was rewritten to be self-paced (sequential-by-construction made the guard redundant, not merely unused).
- **API method**: `POST` multipart `FormData` to `/api/recognition/upload` (`?debug=true` appended when debug mode is on).
- **Result rendering**: `setResult(data)` triggers a normal React re-render; a separate `useEffect` on `[result]` diffs newly-matched faces against `seenIdsRef` to decide whether to (re)show the COMPLETE screen.

**Theoretical minimum gap between scans** = `SCAN_DELAY_MS` (200ms) + however long the previous `scanFrame()` took. Using the MEASURED 1-face client round-trip (§5b, ~386-546ms steady-state): worst-case gap ≈ 200 + 546 ≈ **~746ms** between the end of one scan and the start of the next becoming "due." This replaces the old fixed-`setInterval(scanFrame, 2000)` design, whose worst case was up to 2000ms of dead waiting *in addition to* the request time — i.e., this was the single largest lever affecting the 3-second budget, and it's already been changed (see conversation history; this report does not re-verify that change beyond what's shown in the current code, since no regression was found).

---

## 10. Multi-Face Recognition (MEASURED)

**3-face composite** (`composite_3faces.jpg`, built by tiling 3 distinct real photos — 2 of registered people, 1 of an unregistered person):

| Actual faces | Detected | Matched | Unknown |
|---|---|---|---|
| 3 | 3 | 2 | 1 |

Consistent across all 5 runs.

**5-face composite** (`composite_5faces.jpg`, the same 3 photos tiled with repeats to fill 5 slots — 4 tiles of registered people, 1 of the unregistered person):

| Actual faces | Detected | Matched | Unknown |
|---|---|---|---|
| 5 | 5 | 4 | 1 |

Consistent across all 5 runs. Per-face detail from one representative response:

```
matched  นางสาวนิชชิมา  bbox=[397.0, 108.1, 491.5, 245.7]  conf=0.620
unknown  —              bbox=[699.5, 180.6, 791.9, 296.1]  conf=0.148
matched  นางสาวนิชชิมา  bbox=[396.8, 507.8, 492.1, 643.5]  conf=0.617
matched  นางสาวนิชชิมา  bbox=[145.1, 592.4, 174.9, 631.8]  conf=0.849
matched  นางสาวนิชชิมา  bbox=[145.3, 191.8, 175.0, 230.8]  conf=0.853
```

Each face gets an independent, distinct bounding box and an independent match decision — no cross-contamination between faces in the same frame.

**Batch vs sequential — CODE-confirmed, both are true, at different stages:**
- **Comparison** (embeddings vs the registered index) is fully vectorized/batched: `sims = embeddings @ matrix.T` ([index.py:88](../backend/app/face_recognition/index.py#L88)) — one matrix multiply for all K detected faces against all N registered people, regardless of K.
- **Detection→embedding is sequential per face**: `for face in faces: rec_model.get(image_bgr, face)` ([engine.py:114-115](../backend/app/face_recognition/engine.py#L114)) is a plain Python loop, one ONNX inference call per detected face — this is *why* embedding time scales roughly linearly with face count (§5: ~190-238ms/face) while comparison stays flat (~0.2-0.4ms) no matter how many faces are in frame.

**Frontend multi-result display**: `result.results.map(...)` is used in both the bottom-left compact panel and the COMPLETE screen's name list ([Recognition.tsx:275](../frontend/src/pages/Recognition.tsx#L275), [:311](../frontend/src/pages/Recognition.tsx#L311)) — CODE-confirmed every result in the array is rendered, not just `results[0]`.

---

## 11. Current UI Flow (CODE)

- **Where the recognized first name appears**: a small pill in a bottom-left vertical stack (`absolute bottom-20 left-4`), one pill per currently-detected face, refreshed every scan tick — `✓ FirstName` (green) for matched, plain `Unknown` (gray) for unmatched. No confidence %, no participant ID, no full name, no bounding box drawn.
- **Multiple names**: all entries in `result.results` are mapped to their own pill — simultaneous people all show together, not just the first.
- **COMPLETE state — trigger logic** ([Recognition.tsx:130-143](../frontend/src/pages/Recognition.tsx#L130)): a `useEffect` on `[result]` filters for `status === "matched"` entries whose `keyFor()` (participant_id, falling back to name/first_name) is **not already** in `seenIdsRef` (a `useRef<Set<string>>`, scoped to the current camera session). Newly-matched keys are added to the set, their labels merged into `completeScreen.labels` (appending to an already-visible card rather than replacing it, so two people matched a beat apart still end up on one card), and a `setTimeout` is (re)armed.
- **How long COMPLETE stays visible**: `COMPLETE_SCREEN_MS = 1800` — a **hardcoded constant** in the component file, not read from Settings/DB. (The original design spec for this feature asked for this duration to be configurable; as currently implemented it is not — see §15.)
- **"Already checked in" decision**: purely a frontend, in-memory `Set` cleared on `stop()` ([Recognition.tsx:176](../frontend/src/pages/Recognition.tsx#L176)) — i.e., scoped to one Start→Stop session, not to the database. Stopping and restarting the camera resets who's "already greeted," even though the backend has no matching concept of a "session" at all.
- **Can the same person trigger repeated visual check-ins?** Not within one continuous session (the `Set` prevents it) — but see the important distinction in §15: the visual "already seen" gate is UI-only. The backend still writes a new `Attendance` row for **every single matched scan**, regardless of whether the COMPLETE card has already been shown for that person. A person standing in frame for several seconds (being re-scanned every ~200ms+recognition-time) will accumulate multiple `Attendance` rows for one visit even though the UI only celebrates once.
- **Unknown person handling**: shown as a plain gray "Unknown" pill, never enters the "newly matched" filter (only `status === "matched"` is considered), so it can never trigger the COMPLETE screen. No other special-cased behavior exists for unknowns in the UI.

---

## 12. Error Handling (MEASURED where marked, CODE otherwise)

| Scenario | Behavior | Evidence |
|---|---|---|
| No face detected | `200 OK`, `faces_total: 0`, empty `results` array | **MEASURED** live (solid-gray test image → exactly this response) |
| Multiple faces in a **live scan** | Handled normally, one entry per face | **MEASURED** (§10) |
| Multiple faces in a **reference photo** (registration) | Rejected: `422 "Multiple faces detected. Please upload an image containing only one person."` | CODE ([people.py:98-99](../backend/app/api/people.py#L98), [:148-149](../backend/app/api/people.py#L148)) |
| Unknown face | `status: "unknown"`, gray pill, gets a `FaceDetection` row but no `Attendance` row | CODE + MEASURED |
| Camera unavailable | `getUserMedia` rejection caught, `setError(err.message)` shown as a red banner, `running` never becomes `true` | CODE ([Recognition.tsx:145-157](../frontend/src/pages/Recognition.tsx#L145)) |
| Invalid image bytes | `400 "Could not decode the uploaded image."` | **MEASURED** live (garbage bytes uploaded → exactly this response) |
| Missing/invalid auth | `401 "Not authenticated"` | **MEASURED** live |
| Backend unavailable / network failure | `fetch` throws → caught in `scanFrame()`'s try/catch → generic error banner (`"Recognition request failed."` for non-`ApiError`) — **the scan loop itself does not stop**; `activeRef` is never flipped false by a failed fetch, so the loop will keep retrying every `SCAN_DELAY_MS` indefinitely with no backoff | CODE ([Recognition.tsx:237-241](../frontend/src/pages/Recognition.tsx#L237)) — not triggered live in this review (would require actually killing the backend mid-session, which risks disrupting other testing in this pass) |
| Model fails mid-request | No `try/except` around the recognition handler body beyond the GPU-init fallback in `get_face_app()`; an exception during `detect_faces()` at request time would propagate to FastAPI's default 500 handler | CODE-inferred, **not triggered live** |
| Database fails during background persistence | `_persist_recognition` has **no** `try/except` at all — a DB or disk failure there would be logged by FastAPI's background-task error handling, but the client already received a `200` with a correct-looking recognition result. Attendance/history data would be silently missing with no user-facing signal. | CODE ([recognition.py:21-73](../backend/app/api/recognition.py#L21)) — no error path exists to test |
| Recognition takes too long | No timeout anywhere in the chain — `apiPostForm`'s `fetch` call has no `AbortController`, and the backend has no per-request time limit. A pathologically slow request would simply make the frontend wait; the scanning indicator (`scanning` state) just stays on. | CODE ([client.ts:64-71](../frontend/src/api/client.ts#L64)) |

---

## 13. Code Map

**Frontend:**
- Recognition page: [`frontend/src/pages/Recognition.tsx`](../frontend/src/pages/Recognition.tsx)
- Camera logic: same file — `start()`, `stopCamera()`, `captureBlob()`
- API client: [`frontend/src/api/client.ts`](../frontend/src/api/client.ts)
- Result UI (bottom-left panel): `Recognition.tsx` lines ~271-290
- COMPLETE UI: `Recognition.tsx` lines ~299-319, animation keyframes in [`frontend/src/index.css`](../frontend/src/index.css)

**Backend:**
- Recognition endpoint: [`backend/app/api/recognition.py`](../backend/app/api/recognition.py)
- Detection + embedding: [`backend/app/face_recognition/engine.py`](../backend/app/face_recognition/engine.py)
- Matching / index: [`backend/app/face_recognition/index.py`](../backend/app/face_recognition/index.py)
- Registration (reference embeddings, manual): [`backend/app/api/people.py`](../backend/app/api/people.py)
- Registration (reference embeddings, bulk import): [`backend/app/services/import_service.py`](../backend/app/services/import_service.py)
- Background persistence: `_persist_recognition` inside `recognition.py`
- Settings cache (threshold, debug mode): [`backend/app/services/settings_cache.py`](../backend/app/services/settings_cache.py)
- Startup / model warm-up / index build: [`backend/app/main.py`](../backend/app/main.py)

**Database (all in [`backend/app/models/models.py`](../backend/app/models/models.py)):**
- Person model: `Person` (embedding, det_score, image_path, participant_id, ...)
- Attendance model: `Attendance` (one row per matched scan)
- Detection/history model: `FaceDetection` (one row per detected face, matched or unknown), `Upload` (one row per recognition request that got persisted)

---

## 14. Performance Bottlenecks — Based Only on Measurements

**#1 — Embedding, sequential per face, is the dominant cost at every face count tested.**
Evidence: §5 — embedding averaged ~388ms for 1 face, ~715ms for 3 faces (~238ms/face), ~952ms for 5 faces (~190ms/face); detection stayed flat at ~25-55ms regardless of face count; comparison stayed at ~0.2-0.4ms regardless of face count. Embedding is 65-93% of total server time across all three tested scenarios.
Why: `detect_faces()` runs the recognition model in a plain Python `for` loop, one ONNX inference call per detected face ([engine.py:114-115](../backend/app/face_recognition/engine.py#L114)), rather than a single batched inference call across all faces in the frame.
Potential fix (not implemented, not evaluated for correctness/risk here): batch all detected face crops into one inference call. Note this changes an internal implementation detail of the *same* model, not the model/algorithm/metric itself — but because it touches the recognition engine's inference path, it's flagged here for the reviewer/user to weigh in on rather than assumed pre-approved.

**#2 — CPU-only inference; the GPU is present but inactive.**
Evidence: §6/§7 — `onnxruntime` enumerates `CUDAExecutionProvider` as available, but `get_face_app()`'s own active-provider check fails and falls back to CPU every startup (`GPU init failed (CUDA provider not active, got ['CPUExecutionProvider'])`, MEASURED from a fresh restart's log in this review).
Why: consistent with a missing CUDA Toolkit runtime component on this machine (not re-diagnosed in depth in this pass, since it was already identified previously and is outside this report's "measure, don't fix" scope).
Potential fix: install the missing CUDA Toolkit runtime. Explicitly on the user's own "STOP AND ASK" list ("requiring a GPU") — not something to pursue without the user's sign-off, noted here only as a measured fact (GPU unused) with its likely performance implication, not a recommendation to act.

**#3 — High run-to-run variance for the identical request.**
Evidence: §5 — 1-face embedding time ranged 214ms to 578ms across 8 runs of the *same* image against a *warm, unchanged* model; server total for the same scenario ranged 318.5ms-687.5ms.
Why (inferred, not proven): during testing this machine was concurrently running the Vite dev server, a headless Chromium instance, and the FastAPI background-task thread pool doing prior tests' DB/disk writes — i.e., OS-level CPU scheduling contention on this specific dev machine, not a property of the recognition code itself (same code, same input, same model, very different timings only makes sense as external contention). This means the "worst case" numbers throughout this report are likely worse than what a dedicated, otherwise-idle kiosk machine would show — flagged so the reviewer weighs the *average* figures more heavily than the *max* figures when judging headroom against the 3-second target.

---

## 15. Risk / Correctness Review

### CONFIRMED PROBLEMS

1. **"Minimum Detection Confidence" setting is dead.** It's exposed as an editable field in the Settings UI (`Settings.tsx:50-51`, labeled "Minimum Detection Confidence"), persisted to the DB (`face_detection_confidence` key), and returned by `GET /api/settings` — but it is never read anywhere on the actual detection path. `detect_faces()`/`get_face_app()` never reference it, and `settings_cache` has no getter for it (only `get_threshold()`/`get_debug_mode()` exist). An admin who changes this value in the UI will see no effect whatsoever on real detections. Verified by grepping the entire backend for the constant's usage — only `config.py` (definition) and `settings_routes.py` (passthrough default) reference it.

2. **Background persistence has no error handling.** `_persist_recognition` ([recognition.py:21-73](../backend/app/api/recognition.py#L21)) has no `try/except` anywhere in its body. A disk-full condition, a DB lock, or any other failure during image save or the `Upload`/`FaceDetection`/`Attendance` writes would fail silently from the end user's perspective — the client already received a `200` with a correct-looking identity result before this code ever runs.

3. **`COMPLETE_SCREEN_MS` (1800ms) is hardcoded**, not wired to Settings — `Recognition.tsx` declares it as a plain top-of-file `const`, and `Settings.tsx`'s `DEFAULTS` dict has no corresponding key.

4. **No backoff/circuit-breaker on a failed recognition request.** If the backend becomes unreachable mid-session, `scanFrame()`'s catch block just shows an error banner — `activeRef` is never set to `false`, so the self-paced loop keeps firing a new request every `SCAN_DELAY_MS` + timeout, indefinitely, until a human clicks Stop.

5. **`Attendance` is a scan log, not a check-in log.** A new `Attendance` row is written for every matched scan (`_persist_recognition`, unconditional on `if person_id`), while the frontend's "don't re-celebrate the same person" logic (`seenIdsRef`) exists purely in the browser and is never communicated to the backend. One visit by one person, if they linger in frame, can and will produce multiple `Attendance` rows. Anyone building Reports/History views on top of `Attendance` counts should know this counts *scans*, not *visits*.

6. **Per-face commits in the background writer.** `_persist_recognition` calls `session.commit()` once per `FaceDetection` insert and again per `Attendance` insert inside its `for` loop, rather than batching to a single commit at the end. Not a latency problem (it's backgrounded) but a DB-efficiency smell worth a reviewer's attention if event photos commonly contain many faces.

### POSSIBLE PROBLEMS (evidence suggestive, not conclusive)

1. **Similarity score varies with crop size in a counterintuitive direction.** On the 5-face composite (§10), two large (~95×140px) crops of the same person scored 0.617-0.620, while two much smaller (~30×40px) crops of the *same underlying reference photo* scored 0.849-0.853 — higher confidence for the smaller, lower-detail crop. This is a single synthetic (tiled, non-realistic) test image, not a systematic study across many real photos at varying distances — flagged as "possible," not proven.

2. **No face-size/quality gate before matching.** Any detected face, however small or blurry, is embedded and compared with equal weight; combined with observation (1) above, a low-quality face could plausibly produce a falsely confident match. Not demonstrated against a real false-positive in this review — the current 9-person roster contains no lookalikes to test against.

3. **No margin/runner-up check on matches.** `match_batch()` only looks at the single best score (`np.argmax`) with no check of how much better it is than the second-best candidate. Two visually similar registered people (e.g. siblings) could cause one to silently and consistently win over the other, with no signal surfaced anywhere in the response or logs. Not demonstrated live — no such pair exists in the current roster.

4. **Reference photo quality is unvalidated beyond face count.** Registration only checks "exactly one face detected" — no blur, lighting, angle, or minimum-size check. A poor registration photo would silently produce a poor embedding with no warning to the admin uploading it.

### NOT A PROBLEM (checked and ruled out)

- **Embedding normalization consistency**: registration and live-scan embeddings go through the identical `detect_faces()` function — same detector, same crop/align, same model, same L2 normalization. No train/inference skew found.

---

## 16. Final Summary

**CURRENT SYSTEM STATUS:**

| Area | Status |
|---|---|
| Recognition concept (embeddings + cosine similarity) | **PASS** |
| Multi-face recognition | **PASS** |
| In-memory matching (no DB query on hot path) | **PASS** |
| Model caching (loaded once, never reloaded per request) | **PASS** |
| Background persistence (writes happen strictly after response) | **PASS** *(with the caveat that it has no error handling — §15.2)* |
| Real-time UI (per code review) | **PASS**, but **NOT YET VERIFIED with a real camera in a real browser** — headless camera mocking proved unreliable in this environment across multiple attempts; this remains an outstanding manual verification step |
| 3-second kiosk requirement | **PASS under tested conditions** (1-5 simultaneous faces, this machine, CPU-only inference) — worst observed total ≈1.0-1.1s server-side even at 5 faces, plus ~750ms max scan-loop overhead ≈ well under 3s. Not tested under sustained concurrent multi-kiosk load or on different/lower-spec hardware. |

**TOP 3 THINGS TO FIX NEXT** (not fixed in this review — evaluation only):

1. **Wire up or remove the dead "Minimum Detection Confidence" setting** (§15.1) — it currently misleads whoever administers the kiosk into believing they can tune detection filtering, and nothing happens when they do.
2. **Add error visibility (at minimum logging, ideally a retry or alert) to `_persist_recognition`** (§15.2) — right now a database or disk failure during background persistence is completely invisible; attendance data could go silently missing.
3. **Decide and document `Attendance` row semantics** (§15.5) — either dedupe server-side to one row per visit, or clearly document that it's a scan log so Reports/History consumers don't misinterpret counts.
