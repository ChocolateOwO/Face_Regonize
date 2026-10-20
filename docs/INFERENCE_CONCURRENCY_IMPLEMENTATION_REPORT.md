# Inference concurrency — implementation report (2026-10-20)

Request: `ขอเพื่อนแก้_2026-10-19.md` (Mice robot project): let one server recognise faces for
Face_Regonize, the Mice robot (`POST /api/look`, ≤4 frames/s per camera, one request in flight,
~0.3 s answer) and a future photobooth **at the same time**, without any of them blocking the others.

**Where:** implemented and tested in the **Dummy** (`Reconize_Dummy`, ports 8010/5180) first, per
the project's Dummy-first rule; after the user's manual test and approval it was promoted to Main
and committed — see "Main Promotion & GitHub Deployment" near the end.

## A. Executive summary

- **Two inference lanes.** LIVE (`/api/look`, kiosk, camera nodes) and BACKGROUND (Event Photo,
  video, import, enrollment, every other caller).
- **Dual mode** (default on a GPU with room): the live lane gets its **own** FaceAnalysis session and
  lock, so a live frame never waits behind an Event Photo scan or a video. Costs ~325 MB VRAM
  (measured). **Shared mode** fallback (CPU only, low VRAM, load failure, or forced): one session;
  every background lock acquisition first steps aside for a waiting live caller (bounded 0.3 s), and
  Event Photo now holds the lock per tile / per 8-face embedding chunk instead of per whole photo.
- **Fair live scheduler:** round-robin per `source`, bounded queue (64 total, 1 waiting frame per
  `/api/look` camera), deadlines, expired turns never served late.
- **`/api/look` 429:** only on real overload/timeout; `Retry-After` is an estimate in whole seconds
  (≥1, never 0) plus `X-Retry-After-Ms`.
- **`/api/health`:** keeps `status`, adds mode, device, `busy_with`, queues, `look_avg_wait_ms`,
  latency percentiles, scheduler counters — workload labels only, no camera names or people.
- **Found and fixed an existing bug** in the shared recognition index: `remove()`/`upsert()`
  mutated the id lists in place while `match_batch()` read them outside its lock, so a delete
  landing during a match could name **the wrong person**. Now copy-on-write.
- **`title` / `language`** per participant (admin-entered, optional, migration 014, UI on the
  participant page, returned by `/api/look` and `/api/people`).
- **`detected_at` with offset** (`+07:00`) in `/api/history`, dashboard recent detections and
  participant attendance history; stored values unchanged.
- **Measured on this PC (RTX 4050 Laptop, CUDA)** with Event Photo + video running: Mice `/api/look`
  p50 **459 ms → 28–34 ms (dual) / 36–38 ms (shared)**, p95 **498–517 ms → 49–63 ms / 86–100 ms**,
  429s **18–19 % → 0 % / 0.2 %**. ≤300 ms: **7 % → 99 %**.
- **Not done:** `want=persons` (BLOCKED: needs a body detector that the model pack does not contain
  and an agreed response shape). Manual UI test and the move to Main are waiting for the user.

## B. Requirement checklist

Status: PASS = implemented and verified · EXISTING = already there · PARTIAL · BLOCKED · FAIL.
Evidence test files are in `backend/tests/`; load numbers in section G.

### From the request document

| ID | Requirement | Status | Evidence | Files |
|---|---|---|---|---|
| D1 | 3 workloads use one server's model concurrently, none blocked unusable | PASS | Load test: 3 Mice cams + photobooth + kiosk + node + Event Photo + video, 0–0.2 % 429 (was 18–19 %) | engine.py, live_scheduler.py, look_service.py, recognition.py |
| D2 | `/api/look` answers within ~0.3 s at all times | PASS (this PC, synthetic load) | p99 ≤ 252 ms in 5 of 6 busy runs (one dual run p99 419 ms); 98.9–100 % ≤300 ms | section G |
| D3 | Remove single `inference_lock` bottleneck for live | PASS | `test_dual_mode_live_never_waits_for_a_background_scan` | engine.py |
| D4 | Every background job yields (Event Photo, video_devices, kiosk/node counted live, import, enrollment, photo processing) | PASS | All go through `engine.hold(BACKGROUND)`; `test_background_steps_aside…`, `test_tiled_event_scan_releases_the_lock_between_calls` | engine.py, event_photo_detection_service.py, video_devices.py |
| D5 | Kiosk/camera node are live work | PASS | `run_recognition` uses `live_turn` (kiosk:<user>, node:<id>) | recognition.py |
| D6 | Option A problem 2: cannot interrupt a long call → split tiled scan | PASS | Lock per tile/chunk; results identical (`test_same_faces_as_the_event_photo_scan`) | event_photo_detection_service.py |
| D7 | Option A problem 3: fair round-robin per `source` | PASS | `test_round_robin_between_sources`, `test_quiet_source_is_not_starved_by_a_flood`; fairness min/max 0.99–1.0 | live_scheduler.py |
| D8 | Option A problem 4: background still progresses | PASS | `test_background_progresses_under_continuous_live_load`; bounded 0.3 s yield; Event Photo 107–138 photos/min under live load | inference_priority.py, engine.py |
| D9 | Option B: second model for live | PASS | Dual mode on this PC: `live_session_vram_mb` 324–326 | engine.py, main.py |
| D10 | B problem 1: measure VRAM incl. video/NVENC | PASS (measured, no NVENC encode in the run) | VRAM max 1283 MB with 3 sessions (live, background, video CUDA) of 6141 MB | section G |
| D11 | B problem 3: one shared recognition index | PASS | Matching is outside the sessions; `test_enrollment_import_and_delete_are_seen_by_live_at_once` | index.py |
| D12 | B problem 4: CPU → don't run two sessions | PASS | `test_cpu_only_stays_shared_without_building` | engine.py |
| D13 | Recommended: both, fallback to A when GPU insufficient | PASS | Mode tests (VRAM low / CUDA fail / partial fail / low after load) | engine.py |
| D14 | Real `Retry-After` | PASS | `test_retry_headers_are_whole_seconds_and_never_zero`, `test_busy_429_has_a_real_retry_after` | look_service.py |
| D15 | `/api/health` busy_with, live_queue, device | PASS | `test_health_schema_and_no_camera_names`, ASGI `HealthContractTests` | health_status.py, main.py |
| D16 | Average `/api/look` wait in health | PASS | `look_avg_wait_ms`; 11.7 ms at end of a dual busy run | live_scheduler.py |
| D17 | `want=persons` | **BLOCKED** | Still 422 (`test_want_persons_is_reserved`). Needs a person/body detector not in `buffalo_l` and a response spec | — |
| D18 | `language` per person | PASS | `VoiceProfileTests`, `VoiceProfileContractTests`, frontend `voice-profile-editor.test.mjs` | models.py, 014, person_address.py, people.py, VoiceProfileEditor.tsx |
| D19 | `title` per person | PASS | same | same |
| D20 | `detected_at` with timezone (+07:00) | PASS | `LocalTimeTests`, `HistoryOffsetTests`; live Dummy shows `…+07:00` | local_time.py, history.py, reports.py, people.py |

### From the implementation plan (section 13)

| Item | Status | Evidence |
|---|---|---|
| Current code and requirements audited | PASS | Section D call-site table |
| All inference call sites identified | PASS | Section D |
| LIVE inference lane | PASS | engine.py `LIVE`, `use()`, live_scheduler.live_turn |
| BACKGROUND inference lane | PASS | engine.py `BACKGROUND`, `hold()` |
| Independent model sessions | PASS | dual mode, own lock; measured on GPU |
| GPU capability detection | PASS | provider check + nvidia-smi free VRAM before/after + provider verification of the new session |
| CPU/shared-session fallback | PASS | ModeTests (8 cases) |
| Background cooperative yielding | PASS | engine.hold; LaneTests |
| Tiled inference scheduling reviewed | PASS | Event Photo per tile; video already per tile |
| Per-source live fairness | PASS | SchedulerTests; load fairness 0.99–1.0 |
| Bounded queues | PASS | `test_total_queue_is_bounded`, `test_per_source_bound_for_drop_callers` |
| Timeout/cancellation handling | PASS (timeout); PARTIAL (client disconnect) | `test_timeout_leaves_no_ghost_turn`. A disconnected HTTP client is not detected in the sync handler; its frame still has a 0.5 s deadline, so at most one stale call runs |
| Background starvation prevention | PASS | bounded yield; progress test; load numbers |
| `/api/look` overload handling | PASS | 429 only on queue/source/deadline bounds |
| Meaningful Retry-After | PASS | tests above |
| `/api/health` observability | PASS | tests above |
| Average live wait metrics | PASS | `look_avg_wait_ms`, `p95_wait_ms` |
| Shared recognition index | PASS | one `recognition_index` |
| Enrollment/import refresh | PASS (EXISTING mechanism) | `apply_index_change` → upsert; test with live lane |
| Concurrent index safety | PASS (bug fixed) | `test_concurrent_reads_during_writes_are_consistent` failed on the old index (38 wrong matches), passes now |
| `want=persons` | BLOCKED | see D17 |
| `language` | PASS | D18 |
| `title` | PASS | D19 |
| Timezone-aware `detected_at` | PASS | D20 |
| Existing API compatibility | PASS | 1090/1090 backend incl. robot contract; only additive fields; `Retry-After` value changed as requested |
| Existing PDPA behaviour preserved | PASS | PDPA code untouched; PDPA tests in the 1090 pass |
| Automated tests passing | PASS | backend 1090/1090, frontend 70/70, `tsc -p tsconfig.app.json` clean |
| Regression tests passing | PASS | same |
| Load-test results recorded | PASS | section G, raw JSON in `patch/2026-10-20_inference-lanes/load/` |
| Actual hardware verification status | PASS (this PC) / NOT VERIFIED (other PCs) | RTX 4050 Laptop only; the robot's own PC and real ESP32 frames not tested |
| Final implementation report created | PASS | this file |
| **User manual/visual test (Dummy)** | PASS (user, round 1) — Language combobox re-test PENDING | section H, "Language Editable ComboBox Enhancement" |
| **Apply to Main** | PENDING (needs explicit approval) | section H |

## C. File-by-file change log (Dummy)

| File | Function / class | What changed | Why |
|---|---|---|---|
| backend/app/face_recognition/engine.py | `LIVE`, `BACKGROUND`, `use()`, `hold()`, `init_live_lane()`, `_build_live_app()`, `release_live_lane()`, `is_dual()`, `mode_info()`, `status()`, `busy_with()`, `estimated_free_in()`, `lock_for()`; `get_face_app()` | Lane model: live session + lock, mode decision with VRAM checks and cleanup, per-call lock holding with workload labels and timings, background yield in shared mode; one-time thread-safe model load; `detect_faces` takes its lane from a context variable (signature unchanged for callers/test doubles); `InferenceBusy.retry_after` | D1–D13 |
| backend/app/services/live_scheduler.py (new) | `LiveScheduler`, `Turn`, `LiveBusy`, `live_turn()`, `LookStats` | Fair round-robin turns per source, bounds, deadlines, estimates, look latency window | D7, D14–D16 |
| backend/app/services/health_status.py (new) | `snapshot()` | `/api/health` body | D15–D16 |
| backend/app/services/local_time.py (new) | `aware()`, `to_stored()` | Attach server offset on output; accept offset-bearing filter bounds | D20 |
| backend/app/services/look_service.py | `look()`, `retry_headers()`, `source_key()`, `_busy()` | Live turn per source, real Retry-After, stats, title/language; `MAX_IN_FLIGHT` 4 → 12 | D1, D2, D14, D18 |
| backend/app/api/look.py | `look()` | Passes `source` and account to the service; docs | D7 |
| backend/app/api/recognition.py | `run_recognition()` | Kiosk/node inference through `live_turn` (30 s turn limit → 503 + Retry-After) | D5 |
| backend/app/services/event_photo_detection_service.py | `detect_event_faces()` | Lock per detector call / 8-face embed chunk | D6 |
| backend/app/services/video_devices.py | `session()`, `detect()`, `detect_tiled()` | `engine.hold(BACKGROUND, 'video')` (same lock, yields in shared mode) | D4 |
| backend/app/services/inference_priority.py | docstring | Describes the wider use | — |
| backend/app/services/import_service.py | Excel/CSV import | `detect_faces(..., workload="import")` label | D15 |
| backend/app/api/people.py | `create_person`, `update_person`, `set_voice_profile` (new), `_person_out`, `get_person` | Enrollment label; `PUT /{id}/voice-profile`; title/language out; attendance `detected_at` offset | D18–D20 |
| backend/app/face_recognition/index.py | `upsert()`, `remove()` | Copy-on-write | Index safety bug |
| backend/app/services/person_address.py | `normalize_title()`, `normalize_language()`, `address_for()` | Validation and one-query lookup | D18–D19 |
| backend/app/models/models.py | `Person.title`, `Person.language` | Optional columns | D18–D19 |
| backend/app/migrations/014_person_title_language.py (new) | `upgrade()` | Additive, idempotent | D18–D19 |
| backend/app/api/history.py | `recognition_history()` | Offset on output, filter bounds normalised | D20 |
| backend/app/api/reports.py | `dashboard()` | Offset on recent `detected_at` | D20 |
| backend/app/main.py | startup, `health()` | `init_live_lane()` after model load; new health body | D9, D15 |
| frontend/src/components/VoiceProfileEditor.tsx (new) | component | Title + language editor | D18–D19 |
| frontend/src/pages/PersonDetail.tsx | page | Shows the editor under Call-as | D18–D19 |
| backend/tests/test_inference_lanes.py (new) | 49 tests | Scheduler, lanes, modes, look/health, index, title/language, time | §11 |
| backend/tests/test_inference_api_contract.py (new) | 5 tests | ASGI: health, voice-profile, look, history offset, migration 014 | §11 |
| frontend/tests/voice-profile-editor.test.mjs (new) | 3 tests | Editor behaviour | §11 |
| backend/tests/test_look_api.py | 2 assertions + 1 patch | Retry-After ≥1 (was "0" by contract); bounded `lock_timeout` ≤0.5 s (was ==0.5); addresses patched off the dev DB | Contract change |
| backend/tests/test_inference_arbiter.py | 1 test | Asserts `hold(BACKGROUND` and `lock_for(BACKGROUND) is inference_lock` instead of a source substring | Implementation moved |
| backend/tests/test_event_photo_tiling.py | stub | Stub engine gets `BACKGROUND`, `hold` | Implementation moved |
| backend/tests/test_identity_metadata.py | 3 lists | Migration list includes 014 | New migration |
| tools/inference_load_test.py (new) | harness | Real-model load test, old vs new code | §12 |
| docs/robot-integration-api.md | sections | Lanes, health, Retry-After, title/language, offsets | §E |

Originals: `patch/2026-10-20_inference-lanes/before/` (same relative paths).

## D. Architecture

Inference call sites found (all now on a lane):

| Call site | Lane / workload |
|---|---|
| `/api/look` → look_service | LIVE `look` (source = form `source`, else account) |
| `/api/recognition/upload` (kiosk) → run_recognition | LIVE `kiosk` (source `kiosk:<user>`) |
| `/api/node/recognize-central` → run_recognition | LIVE `camera_node` (source `node:<id>`) |
| Event Photo (`detect_event_faces`, sequential + v2 pipeline, retries) | BACKGROUND `event_photo`, per tile |
| Video jobs / tagged render (`video_devices.detect`, `detect_tiled`) | BACKGROUND `video` (own video session, shared background lock — unchanged) |
| Add/Update person, import | BACKGROUND `enrollment` / `import` |
| local_video_experiment, legacy experiment, FMR (Dummy-only) | BACKGROUND (default) |
| identity_backfill_service | own session, never the engine locks (unchanged) |

```
 /api/look ─┐                       dual:   live session ── _live_lock
 kiosk ─────┼─ LiveScheduler ─ turn ─┤
 node ──────┘ (round-robin/source)  shared: background session ── inference_lock
                                                 ▲ (background yields ≤0.3 s while a live turn waits)
 Event Photo / video / import / enrollment ── engine.hold(BACKGROUND) ── inference_lock
 both lanes ──► embeddings ──► recognition_index.match_batch (one index, copy-on-write)
```

**Model lifecycle.** `get_face_app()` loads the background session once (now under a lock).
`init_live_lane()` runs once at startup: `RECONIZE_INFERENCE_MODE` = `auto` (default) | `dual` | `shared`.
Auto → shared on CPU, on a non-CUDA provider, when nvidia-smi reports < 2048 MB free
(`RECONIZE_LIVE_VRAM_NEED_MB` 1024 + `RECONIZE_LIVE_VRAM_MARGIN_MB` 1024), when the new session's
det or rec model is not on CUDA, when warm-up fails, or when < 1024 MB is left after loading. Any
failure drops the half-built session and records the reason. Never fails startup. One uvicorn
worker = one live session; more workers would each load their own (unchanged property).

**Scheduling.** One live turn at a time (one inference per session). A `/api/look` frame waits
≤0.5 s for turn + lock; a camera may have 1 frame waiting besides the one served; total waiting ≤64.
Kiosk/node wait ≤30 s (they had no limit before). Expired turns are skipped, never served late.
Retry-After = (waiting + running) × recent turn time, or the background holder's expected remaining
call time; whole seconds, min 1, max 10.

**Decisions taken without asking (please confirm or change):**
1. Dual mode yields nothing to live (only shared mode does) — dual-mode GPU contention measured acceptable.
2. Per-camera cap = 1 waiting frame; a newer frame is refused rather than replacing the older one.
3. Kiosk/node turn limit 30 s → 503 (before: unlimited wait).
4. `language` = BCP 47-shaped tag (free text, suggestions th/en/zh/ja/ko); `title` = free text ≤40.
5. Offset added in history, dashboard recent list and participant attendance history only.

## E. API changes (all additive except the 429 header value)

- `POST /api/look`: `source` now drives fairness (send it every time). Matched faces gain `title`,
  `language`. 429 now has `Retry-After` ≥1 (was always `0`) and `X-Retry-After-Ms`. Up to 12 frames
  decode at once (was 4).
- `/api/recognition/upload`, `/api/node/recognize-central`: unchanged contract; may answer 503 +
  Retry-After after 30 s of overload (before: waited indefinitely).
- `GET /api/health`: `status` unchanged; adds `model_ready`, `inference_mode`, `device`, `busy_with`,
  `live_queue`, `background_queue`, `look_avg_wait_ms`, `inference{…}` (schema in
  `docs/robot-integration-api.md` and `services/health_status.py`).
- `PUT /api/people/{id}/voice-profile` `{title?, language?}` (admin). `/api/people*` gains `title`,
  `language`.
- `detected_at` (history, dashboard recent, attendance history): `…+07:00` suffix.
- Schema: migration 014 adds `person.title`, `person.language` (NULL).

## F. Tests and results

| Command (from `Reconize_Dummy`) | Result |
|---|---|
| `backend> python -m unittest discover -s tests -p "test_*.py"` | **Ran 1090, OK** (1036 before + 54 new), 399 s |
| `frontend> node --test tests/*.test.mjs` | **70/70 pass** (67 + 3 new) |
| `frontend> npx tsc -p tsconfig.app.json --noEmit` | exit 0 |
| Vite transform `/src/components/VoiceProfileEditor.tsx`, `/src/pages/PersonDetail.tsx` on 5180 | 200, 200 |
| Live Dummy 8010 startup | migration 014 applied; `Inference mode: dual (… ~326 MB)`; `/api/health` OK |

No tests skipped or weakened. Five existing assertions changed because the behaviour they pinned
changed by request (section C). The new index concurrency test failed against the old `index.py`
(38 wrong matches in one run) and passes with copy-on-write.

## G. Performance (real model, real GPU)

Environment: this PC — Intel i5-13420H (12 threads), 31.7 GB RAM, NVIDIA RTX 4050 Laptop 6 GB,
ONNX Runtime CUDAExecutionProvider, InsightFace buffalo_l (det 320 + recognition). In-process
harness `tools/inference_load_test.py` (no HTTP, no DB). Old code = copy with the original files
("legacy"). Runs interleaved legacy/dual/shared, 45 s (busy) / 40 s (event-only) / 30 s (live-only).

Load: 3 Mice cameras at 4 frames/s, one request in flight, 640×480 JPEG with 1–2 faces; 1 photobooth
at 2/s (`/api/look`); kiosk 2/s and camera node 2/s (station path). Busy adds Event Photo (3600×2400
crowd photos, tiled) and a tagged-video style tiled 1080p detector on its own CUDA video session.

**Busy (Event Photo + video):**

| run | Mice p50 | p95 | p99 | ≤300 ms | 429 | longest 429 run | fairness | kiosk p95 | node p95 | Event photos/min | video fps | VRAM max |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| legacy 1 | 463 | 517 | 531 | 7.4 % | 18.5 % | 1.0 s | 0.96 | 796 | 814 | 120 | 0.15 | 943 MB |
| legacy 2 | 459 | 498 | 513 | 6.7 % | 19.5 % | 1.5 s | 1.00 | 791 | 792 | 126 | 0.15 | 923 MB |
| dual 1 | 34 | 63 | 419 | 98.9 % | 0 % | 0 | 1.00 | 39 | 33 | 59 | 2.92 | 1283 MB |
| dual 2 | 28 | 49 | 251 | 99.1 % | 0 % | 0 | 1.00 | 39 | 55 | 57 | 2.95 | 1285 MB |
| shared 1 | 36 | 86 | 240 | 99.1 % | 0.2 % | 0.3 s | 0.99 | 92 | 87 | 43 | 2.34 | 925 MB |
| shared 2 | 38 | 100 | 252 | 99.1 % | 0.2 % | 0.2 s | 0.99 | 104 | 106 | 45 | 2.29 | 959 MB |

Legacy starved video (0.15 fps) because Event Photo held the lock for whole photos; the new code
shares background time, which is why Event Photo/min falls here. Without video:

**Event-only (Event Photo, no video):**

| run | Mice p50 | p95 | 429 | Event photos/min |
|---|---|---|---|---|
| legacy 1 / 2 | 453 / 446 | 499 / 489 | 16.2 % / 15.6 % | 132 / 134 |
| dual 1 / 2 | 68 / 66 | 119 / 113 | 0 % / 0 % | 138 / 132 |
| shared 1 / 2 | 58 / 60 | 101 / 106 | 0 % / 0 % | 110 / 107 |

**Live only (no background):** legacy p50/p95 110/176 ms, dual 107/172, shared 106/161, all 0 % 429
(idle-GPU latency is higher than busy latency because the laptop GPU clocks down at ~6 % load).

Other: CPU 7–15 % average; process RSS 1.15–1.51 GB; GPU util 54–67 % in busy runs. The one 1.1 %
photobooth refusal in new-code runs is a single request in the first second (cold turn estimate).
Throughput ~11.7 answered looks/s in every new-code run (= the offered load).

Limits: synthetic frames and crowd photos; one PC; no NVENC encoding in the load (the decode/detect
part of video only); HTTP/JSON overhead not included (earlier HTTP measurements on this PC added
~10–40 ms); not run on the robot hub's PC or with real ESP32 frames. **Target 300 ms: met on this
PC under this load; NOT VERIFIED on other hardware.**

## H. Remaining issues and next actions

| Item | Status | Next action |
|---|---|---|
| User manual/visual test of the Dummy (participant page Title/Language; kiosk; health) | PENDING | Steps below |
| Apply to Main | PENDING | Explicit approval; then patch backup, hand-merge `main.py` and `test_identity_metadata.py` (they differ between Dummy and Main), copy the rest (Main bases equal the Dummy bases for every other file, checked 2026-10-20) |
| `want=persons` | BLOCKED | Agree semantics + add a person detector model |
| HTTP client disconnect not detected | PARTIAL | Bounded by the 0.5 s frame deadline; acceptable unless measured otherwise |
| identity_backfill pauses on `inference_lock`/kiosk DB rows only; it does not see `/api/look` traffic | Unchanged (pre-existing) | Only if backfill runs while the robot is live |
| Measurements on the event PC / with the robot hub over HTTP | NOT VERIFIED | Run `tools/inference_load_test.py` there; check `/api/health` during the event |
| Index bug also exists in Main | Fixed in Dummy only | Comes with the Main promotion |

**Manual test (Dummy):** http://localhost:5180 (backend http://127.0.0.1:8010), admin / admin123.
1. People → open any participant. Under "เรียกว่า / Call as" there is "คำนำหน้า/ตำแหน่ง / Title"
   and "ภาษา / Language". Save is disabled until a value changes.
2. Enter Title `ดร.` and Language `TH` → Save → Language shows `th`. Reload: values stay.
3. Language `thai language` → Save → red error, nothing saved. Clear both → Save → empty after reload.
4. Call-as still saves on its own and is not changed by step 2.
5. History page: times display as before (the API now sends `+07:00`).
6. Recognition (kiosk) page: scanning still works.
7. Open http://127.0.0.1:8010/api/health → `inference_mode: "dual"`, `device: "cuda"`.

## Language Editable ComboBox Enhancement (2026-10-20, round 2)

The user's manual test of the Dummy passed everything above (recognition, Title, Call as,
History, `/api/health`). One UI change was requested before Main: Language becomes an editable,
searchable combobox. Dummy only; Main untouched.

**1. Additional requirements.** Pick from a list *or* type any other code (never restricted to the
list); search; keyboard, mouse and touch; save only on Save; show codes that are not in the list;
clear to null; error under the field; no change to the backend, schema, API, recognition, index,
scheduler, PDPA, Call as or Title.

**2. Listed languages (saved as the code):** ไม่ระบุภาษา → `null` · ไทย → `th` · English → `en` ·
中文 (จีน) → `zh` · 日本語 (ญี่ปุ่น) → `ja` · 한국어 (เกาหลี) → `ko` · Français (ฝรั่งเศส) → `fr`.

**3. Typing a code.** Type in the box; the list filters by name, code, or English/Thai name
(`fren`, `ญี่`, `korean`). A known name or code resolves to its code (`ไทย`, `TH`, `french` →
`th`/`th`/`fr`). Anything else is saved as typed and normalised by the backend (`de-de` → `de-DE`,
`pt_br` → `pt-BR`). A valid unlisted code is also offered as a row: `ใช้ "de-DE" (พิมพ์เอง)`. A saved
listed code shows its name; an unlisted one shows the code itself (`pt-BR`), never blank.

**4. Validation.** Unchanged backend rule (`person_address.normalize_language`): BCP 47 shape
`^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$`, ≤35 characters, canonical case (language lower, region
upper, script title). The same check runs in the browser before sending, so `thai language`,
`hello world`, `12345` show "Language must be a language tag such as th, en or en-US." under the
field (red border, `aria-invalid`, `role=alert`) and nothing is sent; a backend 422 lands in the
same place. The backend still rejects them independently (tested).

**5. Files changed (Dummy).**

| File | Change |
|---|---|
| frontend/src/components/LanguageComboBox.tsx (new) | Combobox component + `LANGUAGE_OPTIONS`, `resolveLanguage`, `displayLanguage`, `filterLanguages`, `isLanguageTag` |
| frontend/src/components/VoiceProfileEditor.tsx | Uses the combobox; browser-side validation; language errors shown under the field; Save row wraps on narrow cards; `<datalist>` removed |
| frontend/tests/voice-profile-editor.test.mjs | Rewritten: 18 tests rendering editor + combobox together against a fake API that applies the backend's rules |
| backend/tests/test_inference_api_contract.py | +3 ASGI tests: 23 listed/typed codes round-trip, invalid text 422 and unchanged, clear to null |

Not changed: backend code, models, migration 014, `PUT /voice-profile` contract, PersonDetail.tsx,
CallAsEditor, recognition, index, scheduler, PDPA.

**6. Component behaviour.** ARIA combobox (`role=combobox`, `aria-expanded`, `aria-controls`,
`aria-activedescendant`, listbox/option roles). Click or focus opens the full list; typing filters;
↑/↓ move (wrapping), Home/End, Enter picks, Esc closes, Tab leaves the typed value. Options use
`pointerdown` preventDefault + click, so a mouse click or a touch tap selects without blurring
first; ▾ opens/closes the full list, × clears. The list opens upward when there is no room below,
aligns right when it would leave the screen, may be wider than the field (max 18rem, no wrapping),
`z-50`. Nothing is saved until Save.

**7. Automated tests (run this round).**

| Command | Result |
|---|---|
| frontend `node --test tests/*.test.mjs` | **85/85 pass** (voice-profile-editor 18) |
| frontend `npx tsc -p tsconfig.app.json --noEmit` | exit 0 |
| backend `python -m unittest discover -s tests -p "test_*.py"` | **Ran 1093, OK** (+3) |
| Vite transform of both components on 5180 | 200 / 200 |
| Headless browser on the running Dummy (read-only, Save never clicked) | 7 rows correct; every option visible and on top (not covered/clipped) at 1366×768; `fren` → Français; ↓ + Enter picks it; typing `de-DE` offers the typed row and Tab keeps `de-DE`; with OS dark scheme the field, list and card stay `rgb(255,255,255)` |

Request test map: T1 `TEST 1`; T2–T4 pick + save + reload; T5–T6 typed `de-DE`/`es-MX`; T7 `TH` → `th`
(and `pt_br` → `pt-BR`); T8 three invalid inputs; T9 × and "ไม่ระบุภาษา" → null; T10/T11 Call as and
Title unchanged (frontend and ASGI); T12 `pt-BR`/`de-DE`/`zh-Hant-TW` shown and editable; T13
keyboard; T14 touch `pointerType: "touch"` tap and ▾ tap; T15 palette (see below); T16 failed save
shows error, nothing reported saved, not stuck in "Saving..."; T17 full suites above.

**8. Regression.** Backend 1093/1093 and frontend 85/85 including Call-as editor, robot contract,
look/health, PDPA and migration tests; migration 014 untouched and its test passes; API contract
unchanged (same endpoint, body and validation).

**9. Open points.**
- **Dark mode:** the app has no dark theme (no `dark:` styles anywhere; Tailwind default). The
  combobox uses the page's own light palette and `color-scheme: light`, so under an OS dark scheme it
  stays consistent with the page (verified headless). The old native `<datalist>` popup followed the
  OS instead. A real dark theme for the app would be a separate task. Status: PASS for "renders
  correctly in light and dark OS settings", not a themed dark mode.
- **Phone width (390 px):** the participant page's fixed sidebar pushes the whole card off-screen
  at this width (pre-existing layout, Title input included); the list lines up with the field. At
  768 px the field fits the card, Save wraps below and the list stays on screen.
- **Real touch device** not tested (only simulated touch events in tests); please tap through once.
- Pre-existing, not touched: the Call-as row is cramped at 768 px.

## Main Promotion & GitHub Deployment (2026-10-20)

Approved by the user ("Promote Dummy to Main + Commit + Push"). Everything from both rounds,
except `want=persons` (still BLOCKED) — client-disconnect detection remains a known limitation.

**1. Starting Main HEAD:** `55bfae8` (= origin/main after `git fetch`, 0 ahead / 0 behind).
**2. Final Main HEAD:** see the follow-up docs commit that records the pushed hash (a commit cannot
contain its own hash).

**Pre-existing uncommitted work in Main (another session) — preserved, NOT committed:**
`CURRENT_TASK.md`, `PROJECT_CONTEXT.md`, `backend/app/auth/deps.py`, the `integration_snapshot`
lines of `backend/app/main.py`, `event_pipeline_service.py`, `photo_processing_service.py`,
`scripts/manage_machine_user.py`, `tests/test_machine_role.py`, `docs/privacy_policy.md`,
`frontend/tests/photo-batch-download.test.mjs`, and the untracked participant-snapshot / audit files.
The full list and diff as found are saved in the backup folder.

**3. Files promoted (31 copied, 2 hand-merged; 32 files committed).** For every modified file the
current Main content was checked equal (line-endings normalised) to the base the Dummy change started
from; each file kept Main's own line endings.

| File | Changes | Test | Status |
|---|---|---|---|
| backend/app/face_recognition/engine.py | LIVE/BACKGROUND lanes, dual session, GPU checks, fallback, `hold()`, status | test_inference_lanes (Lane/Mode), test_look_api, test_inference_arbiter | PASS |
| backend/app/face_recognition/index.py | Copy-on-write (wrong-person race fix) | SharedIndexTests | PASS |
| backend/app/services/live_scheduler.py (new) | Fair per-source turns, bounds, deadlines, look stats | SchedulerTests | PASS |
| backend/app/services/health_status.py (new) | `/api/health` body | HealthContractTests, LookApiTests | PASS |
| backend/app/services/local_time.py (new) | `detected_at` offset | LocalTimeTests, HistoryOffsetTests | PASS |
| backend/app/services/look_service.py | Live turn, Retry-After, title/language | test_look_api, LookApiTests, robot contract | PASS |
| backend/app/services/inference_priority.py | Docstring | — | PASS |
| backend/app/services/event_photo_detection_service.py | Lock per tile / embed chunk | test_event_photo_tiling, test_video_render, arbiter | PASS |
| backend/app/services/video_devices.py | `engine.hold(BACKGROUND,'video')` | test_video_render | PASS |
| backend/app/services/import_service.py | Workload label | import tests | PASS |
| backend/app/services/person_address.py | Title/language validation, `address_for` | VoiceProfileTests, LanguageComboBoxBackendTests | PASS |
| backend/app/models/models.py | `Person.title`, `Person.language` | contract tests | PASS |
| backend/app/migrations/014_person_title_language.py (new) | Additive columns | Migration014Tests, test_identity_metadata, production-copy check | PASS |
| backend/app/api/look.py | `source` → fairness | test_look_api | PASS |
| backend/app/api/recognition.py | Kiosk/node on the live lane | test_recognition_persist, robot contract | PASS |
| backend/app/api/people.py | `PUT /voice-profile`, fields, attendance offset | VoiceProfileContractTests | PASS |
| backend/app/api/history.py, reports.py | Offset on `detected_at`, filter bounds | HistoryOffsetTests, robot history | PASS |
| **backend/app/main.py (hand-merged)** | `init_live_lane()` + new health; Main's uncommitted `integration_snapshot` lines kept in the working tree but **not** committed (index built from HEAD + these 3 hunks) | full suites | PASS |
| **backend/tests/test_identity_metadata.py (hand-merged)** | Main's lists (no 011) + `014` | itself | PASS |
| backend/tests/test_inference_lanes.py, test_inference_api_contract.py (new) | 57 tests | themselves | PASS |
| backend/tests/test_look_api.py, test_inference_arbiter.py, test_event_photo_tiling.py | Contract updates (section C) | themselves | PASS |
| frontend/src/components/LanguageComboBox.tsx, VoiceProfileEditor.tsx (new), pages/PersonDetail.tsx | Title + Language combobox | voice-profile-editor.test.mjs (18), tsc | PASS |
| frontend/tests/voice-profile-editor.test.mjs (new) | 18 tests | itself | PASS |
| tools/inference_load_test.py (new) | Real-GPU load harness | used for section G | PASS |
| docs/robot-integration-api.md | Was untracked in Main (robot API doc from 2026-10-10..15, same content as the Dummy base) + this work's sections; committed whole | — | PASS |
| docs/INFERENCE_CONCURRENCY_IMPLEMENTATION_REPORT.md (new) | This report | — | PASS |

**4. Hand-merged:** `backend/app/main.py`, `backend/tests/test_identity_metadata.py` (above).

**5. Database migration.** Main had 001–010, 012, 013 (no 011; 014 does not depend on 011 — the
runner applies by id). Checked first on a copy of the production DB through the real `init_db()`:
applied `014` only; `person` gained `title`, `language` (all NULL); all 130 people unchanged (id,
participant id, names, call_as, embedding); every other table's rows and columns unchanged except
`schemamigration` +1; a second start applied nothing. Then Main was started: log "Applied 1
migration(s): 014", 130 faces indexed, `/api/people` total 130.

**6. Backup:** `Reconize/patch/2026-10-20_1126_inference-lanes-language/` (git-ignored): originals
of all 22 touched existing files (incl. `CURRENT_TASK.md`) at their relative paths, byte-identical
(hash-checked); `backend/database/app.db` before migration (34,283,520 bytes, hash-identical);
`git-status-before.txt`, `head-before.txt`, `main-uncommitted-before.diff`; NOTES.md.

**7. Test results.**

| Tree | Backend | Frontend | tsc |
|---|---|---|---|
| Main working tree (incl. the other session's uncommitted work) | **1014/1014 OK** | **88/88** | 0 |
| Exact committed content (`git checkout-index` export of the index) | **990/990 OK** | 83/88 — the 5 failures are in `photo-batch-download.test.mjs` and fail identically on pure HEAD `55bfae8` (65/70); the other session's uncommitted edit to that test fixes them | 0 |
| Pure HEAD `55bfae8` (reference) | — | 65/70 (same 5) | — |

**8. Regression.** None: the only failures in any tree are the 5 pre-existing ones above. Live Main
(read-only, nothing saved): `/api/health` dual/cuda; admin login; `/api/people` (title/language
present, null); `/api/history` `…+07:00`; dashboard; PDPA summary 200; `/api/look` 200 with
`kept:false`, `want=persons` 422; frontend 200 on 8000 (rebuilt `dist`, git-ignored) and 5173;
headless browser on 5173: Language combobox lists the 7 rows, search `fren` → Français, Enter picks,
typed `de-DE` kept, nothing covered, dark OS scheme keeps the page palette. Kiosk, camera node,
enrollment, import, Event Photo and video were verified by the automated suites only (live runs
would write production data).

**9. Known limitations.** `want=persons` BLOCKED (needs a body detector + response spec). A
disconnected HTTP client is not detected; its frame is bounded by the 0.5 s deadline. App has no dark
theme (combobox keeps the page palette). Phone width: page layout (sidebar) pre-existing. Load numbers
are from this PC only. identity_backfill does not pause for `/api/look` traffic (pre-existing).

**10–11. Commit / push:** commit message `feat: add concurrent face inference lanes and language
profiles`; hash and push verification recorded in the follow-up docs commit.

**12. Rollback (this work only).**
- Code: `git revert <commit>` (a new commit; never reset). Or copy the files from the backup folder
  back over Main and delete the 11 new files listed in section C.
- Database: migration 014 only added two NULL columns; old code ignores them, so a code rollback
  needs no DB change. Restoring `backup/backend/database/app.db` is possible with the backend stopped,
  but loses anything recorded since 2026-10-20 11:42.
- Force shared mode without a rollback: set `RECONIZE_INFERENCE_MODE=shared` and restart.

## I. Git status

- Repository: Main `E:\งาน\491\Face_Reconize\Reconize`, branch `main`.
- Dummy rounds 1–2: Main untouched, HEAD `55bfae8`, nothing committed.
- Promotion: one feature commit on top of `55bfae8` with the 32 files of this work only, then a
  docs commit recording its hash and the push result (see "Main Promotion & GitHub Deployment").
  No force push, reset, rebase or merge. The other session's uncommitted changes stay uncommitted.
