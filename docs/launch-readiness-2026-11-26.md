# Launch readiness and Main Hub integration (2026-11-26)

**Scope.** This covers the Face_Regonize items of the Mice Hub request `friend_requests_to_send_2026-11-26.md`, plus the verified web updater and Windows installation package.

**How it was built and verified.** Everything was built and checked in isolated checkouts on the one Windows PC: scratch database, storage, credentials, `LOCALAPPDATA` and port 8010. It was then published from a clean tree based on origin/main `26129fa`.

**What stays local and untouched:**

- The production Main working copy, its runtime, credentials, database and storage. They were not modified, and Main stayed stopped.
- Main's uncommitted work from another workstream (participant snapshot, event-pipeline guards and its local fix to `photo-batch-download.test.mjs`) is not part of this change.

**Evidence labels:**

- **[real GPU]** RTX 4050 Laptop, CUDA, in-process or HTTP on this PC.
- **[real Hub client]** the Hub's own watcher (`apps/faces/service.py`) and `friend_api` from a read-only clone of `final_proj_mice` `3c56e6e`, against an isolated Reconize.
- **[Hub code, local harness]** the Hub's own `tools/jobsched.py` scheduler behind a minimal HTTP wrapper with the same contract.
- **[double]** a local stand-in (MJPEG relay, scheduler failure modes).
- **[simulated]** a load or Voice stand-in. The real Voice/LLM stack is not on this PC.

## Request matrix

| # | Request | Status | Where | Evidence |
|---|---|---|---|---|
| P1-1 | Real `JWT_SECRET`, no `admin/admin123` | **Shipped** (install + warnings); real Main still needs the operator steps below | `scripts/init_env.py`, `backend/scripts/set_admin_password.py`, `services/security_defaults.py`, `GET /api/system/security` (admin only), startup log, `start.bat`, admin banner | `test_install_tools.py`, `test_security_defaults.py`, `language-mobile.test.mjs`; browser smoke |
| P1-2 | Use the Hub's MJPEG camera instead of opening it | **Shipped** (opt-in: `MICE_HUB_URL`) | `services/hub_link.py`, `services/hub_camera.py`, `api/hub_cameras.py`, `frontend/src/hubStream.ts`, `pages/CameraNode.tsx` | `test_hub_camera.py` (19), `hub-stream.test.mjs` (10) [double]; browser: Hub camera → Local Agent → "1 FACE DETECTED" [real GPU + double] |
| P1-3a | Background work yields to `/api/look` | **Already present** | `engine.hold(BACKGROUND)` steps aside for a waiting live caller in shared mode, for every background caller; dual mode gives live its own session (since 2026-10-20) | existing `test_inference_lanes.py` |
| P1-3b | Hub GPU lease around background model calls | **Shipped** (opt-in: `MICE_HUB_GPU_LEASE=1`) | `services/hub_scheduler.py`, `engine.hold`, `node_agent.py` opts out | `test_hub_scheduler.py` (14) [double]; benchmark below [real GPU, Hub code, simulated Voice] |
| P2-6 | Hybrid batch embedding | **Shipped opt-in, off by default** (robot p95 worse on this GPU) | `engine.py` (`RECONIZE_HYBRID_EMBED`), `tools/hybrid_embed_bench.py` | `docs/experiments/2026-11-26-hybrid-embedding.md` [real GPU] |
| P2-7 | Phone layout at 390 px, 404 page, `?lang=` | **Shipped**; the app's screens are still English only | `Layout.tsx`, `index.css`, `pages/NotFound.tsx`, `language.ts`, `Login.tsx` | `language-mobile.test.mjs` (8); browser smoke at 390×844 |
| P2-8a | Stable `person_id` in people / history / look | **Shipped** (additive `person_id` in `/api/people`; history and look already had it) | `api/people.py` | `test_stable_person_id.py` |
| P2-8b | Machine account `robot-hub` | **Already present**; verified separately from the participant-snapshot feature | `scripts/manage_machine_user.py`, `auth/deps.py` | `test_machine_role.py`; Hub e2e step 7 [real Hub client] |
| P2-9 | ORT `gpu_mem_limit` 2 GB, arena `kSameAsRequested`, threads 2/1 | **Evaluated, not shipped**: no measurable change | none | below [real GPU] |
| P2-10 | Reduced-resolution JPEG decode | **Evaluated, not shipped**: changed face counts | none | below [real GPU] |
| P3-12 | `detected_at` with zone offset | **Already present** (`+07:00`, `services/local_time.aware`) | `api/history.py` | `test_stable_person_id.py` |
| P3-11 | `want=persons` in `/api/look` | **Deferred** | `api/look.py` returns 422 "reserved for future body detection" | needs a body/person detection model and an agreed Hub contract |
| P3-13a | Webhook when someone is recognized | **Deferred** | none | needs the Hub contract first (below) |
| P3-13b | Lazy model load/unload | **Deferred** | none | needs a separate change plus checks of cold-start and first-look latency (first crowd frame is already 450-625 ms cold) |
| P3-13c / 17 | TensorRT FP16, DirectML, AMD/Intel | **Deferred** | none | needs matching hardware and a score comparison; CUDA and CPU stay the supported paths |
| P3-16 | NVENC/nvJPEG for recording and decode | **Deferred** for cameras | tagged video already has NVENC (2026-10-13) | browser recording uses MediaRecorder; nothing to change in Reconize |
| P1-4/5, P2-11/12, P3-14/15 | All-Jao, Photobooth | **Out of scope** (other projects) | none | none |

The webhook (P3-13a) needs the Hub to publish:

- a receiving endpoint;
- the event schema `{name, person_id, node_id, tags, time}` with deduplication `ref` rules;
- the direction of authentication (the A4 `mice1` token is Hub → app; app → Hub uses `X-Mice-Key`).

The Hub has no such receiver at `3c56e6e`.

## The updater and installation package (19 files, `1e42662`)

**Unchanged since its verification earlier today.** Rehearsal tree `823d9c0` = origin/main `26129fa` + the patch with sha256 `54211a24…eabac`. Since origin/main had not moved, all 19 files were still missing and were applied as-is.

**Verified earlier on 2026-11-26 on the identical files:**

- clean-clone setup in CPU (3.5 min) and GPU (4.5 min) modes;
- first sign-in with generated credentials (`admin123` refused) and an empty database;
- login page without demo credentials and with the upstream Help link;
- updater states: unconfigured, no published release, check failed, up to date and update available, including stale cached results;
- confirmation of the exact version, the release-manifest integrity checks, refusal while jobs are active;
- automatic code and database rollback when the new version fails to start;
- `.env`, database and storage preserved, with a local git origin and a stand-in backend.

**Re-run in this final tree:** `test_update_release.py`, `test_install_tools.py`, `update-status.test.mjs`, the browser smoke of login and updates.

## Hub GPU lease measurements

[real GPU, Hub code (local harness), simulated Voice]. 45 s per run, lease OFF/ON interleaved, 2 rounds.

**Load:**

- Event-Photo-like background detection on 1600 px crowd photos, run continuously.
- A robot-like `/api/look` stream on the LIVE lane, at most 10 frames/s.
- Voice stand-in: every 2 s it takes a `speech` lease and runs a fixed GPU job.

| | Lease off | Lease on |
|---|---|---|
| Speech burst, wait + job, p50 | 452-472 ms | **302-304 ms** |
| Speech burst, wait + job, p95 | 471-490 ms | **333-334 ms** |
| Speech lease wait, p50 | 3 ms | 30-33 ms |
| `/api/look` p50 | 21.9-23.5 ms | 18.3-19.1 ms |
| `/api/look` p95 | 38.6-40.2 ms | 25.8-26.2 ms |
| `/api/look` p99 | 45.8-46.7 ms | 30.6-31.1 ms |
| Background photos/s | 24.3-24.4 | **14.0** (-42%) |
| Leases per 45 s; 408; fail-open | 0 | 620; 0; 0 |

**Decision:** opt-in, off by default. It helps speech and live work, but costs background throughput while Voice is active. Turn it on when Voice responsiveness matters during Event Photo, import or video work.

**Not measured:** the real Voice latency. Voice is not available on this PC.

## Not shipped (measured)

**ORT runtime budgets.** [real GPU, fresh processes, interleaved] Default vs `gpu_mem_limit=2GB` + `kSameAsRequested` + intra 2 / inter 1, robot-like + crowd sequence, 600 calls:

- p50 12.7 vs 12.7 ms, p95 31.0 vs 31.0 ms, 63-64 frames/s either way;
- CPU 155-157% vs 155-159% of one core;
- GPU memory +437-441 vs +427 MB;
- identical face counts.

No benefit here, so the defaults stay.

**Reduced decode** (`IMREAD_REDUCED_COLOR_2/4`, only where the image is shrunk to 960 px anyway). [real GPU] 60 QA crowd photos at 3600×2400:

- decode + resize p50 39.2 → 20.5 ms;
- but **4 of 60 photos changed face count** (for example 3 → 4).

That changes recognition behavior, so it is not shipped. Event Photo keeps full-resolution decoding and outputs.

## Tests on the final tree

The commands and results are in `Reconize_Dummy/patch/2026-11-26_launch-readiness/publication/TESTS.md`, outside Git.

**Summary:**

| Check | Command | Result |
|---|---|---|
| Backend | `python -m unittest discover -s tests` (isolated `LOCALAPPDATA`) | **1109/1109 OK** |
| Frontend tests | `node --test tests/*.test.mjs` | **114/119**: the 5 failures are the upstream baseline below |
| Type-check | `tsc -p tsconfig.app.json` | clean |
| Build | `npm run build` | OK |
| Lint | `npm run lint` | 0 errors (warnings only) |
- **Upstream baseline:** the 5 `photo-batch-download.test.mjs` cases fail on pristine origin/main `26129fa` too ("Error: ./ExportSelectionPanel": the test does not load a component the page now imports). This change does not touch that page or test. The other workstream's uncommitted local fix makes them pass (overlay run).

### End-to-end session [real Hub client, isolated Reconize on 8010, QA synthetic people]

| Step | Result |
|---|---|
| Start | 7.5 s |
| Watcher signed in and live | 1.5 s |
| Look | 6 faces, 6 known |
| Delivery of a recognized check-in to the watcher | 0.26-0.29 s |
| Reconize killed | the watcher reports off in 0.3 s; look answers "off" |
| Restart | live again in 1.6 s; the old token still works |
| Restart with a new signing secret | the old token gets "login"; the watcher signs in again and is live in 9.5 s |
| `robot-hub` machine account | live in 1.0 s; look, present, dashboard, settings subset and node status allowed; recording upload refused (403); observe-only upload allowed |

### Browser smoke [real GPU, Hub camera = double, Local Agent real]

- **Login:** `?lang=th` sends Help to the Thai guide and sets `<html lang>` to `th`; no demo credentials shown; the language is kept after login and refresh.
- **Security banner:** shows the steps for the test install's default password.
- **404:** page shown, also after refresh.
- **Updates panel:** "not configured".
- **CCTV:** "Hub · QA Cam" listed without browser camera permission; the live preview is 640×806; a Detection Session gives "1 FACE DETECTED" through the Local Agent.
- **390 px:** page width 390 (no sideways scroll), the menu button opens the drawer and it closes after navigating.
- **Console errors:** 0.

## Before starting the real Main (operator, with the owner's approval)

Never paste a secret into a chat, a log or Git.

1. **Main is stopped.** Nothing is listening on 8000 or 5173.
2. **Back up first.** Copy Main `.env` and `backend\database\app.db` (plus any `-wal`/`-shm` files) into a dated `patch\` folder and verify the hashes.
3. **Update the Main checkout to this publication.** The other workstream's uncommitted edits must be committed or set aside first; `backend/app/main.py` is the only file both touch, and it merges cleanly.
4. **Check the current state:** `backend\.venv\Scripts\python.exe scripts\init_env.py --check`.
5. **Replace the signing secret.** In `.env` set `JWT_SECRET` to a new random value; everyone is signed out once. Also replace `ADMIN_PASSWORD` with a random value: it is unused after the first start, but must not stay `admin123`.
6. **Change the admin password:** `cd backend`, then `.venv\Scripts\python.exe scripts\set_admin_password.py` (asks twice, hidden).
7. **Machine account:** `.venv\Scripts\python.exe scripts\manage_machine_user.py create --username robot-hub`. Use `reset` if it already exists. Type the password, shown once, directly into the Hub's `main_python\faces_login.json`.
8. **Local Agent stations** default to `admin/admin123`: restart them with `--ask-credentials`.
9. **Optional `.env` settings:** `MICE_HUB_URL=http://127.0.0.1:8642`, `MICE_HUB_GPU_LEASE=1`, `RECONIZE_HYBRID_EMBED=1`.
10. **Start and check:**
    - start with `start.bat`; there should be no SECURITY warning in the backend window;
    - `admin123` is refused;
    - the Hub watcher's `:8769/health` shows `loggedIn: true` and `live: true`.

## Hub-side prerequisites (Hub project, not Reconize)

- **Login file:** `faces_login.json` with the `robot-hub` login.
- **Partner entry:** `config/partners.json` → `reconize.api` = `http://127.0.0.1:8000`.
- **Hub cameras:** keep the Hub's camera relay running and do not also let the Hub's own face watcher scan a camera that a Reconize station scans, or inference runs twice.
- **GPU leases:** `config/scheduler.json` must keep the `background` and `speech` classes (verified at `3c56e6e`).
- **Language links:** the Hub's links may carry `?lang=th|en|zh|ja`. Reconize remembers the choice for Help; the app's screens are not translated yet.

## First release (needed to exercise the web updater end to end)

1. **Install the current version separately.** Clone the repository into a separate test folder (VERSION 1.0.1) and install it with `setup-windows.ps1`. Run it on port 8010 with its own `LOCALAPPDATA`.
2. **Make a release commit:** bump `VERSION` (for example to 1.0.2), commit, push, then `git tag -a v1.0.2` and push the tag.
3. **Build the manifest:** `python tools\release\make_release_manifest.py v1.0.2`.
4. **Publish:** `gh release create v1.0.2 dist-release\release-manifest.json dist-release\release-manifest.json.sha256`, then check that the GitHub asset `digest` equals the `.sha256` file.
5. **Update the test install:** Settings → Check for Updates → Update Now → confirm "Install v1.0.2". Expect UPDATE COMPLETE, a history row, a verified backup, and `.env` and storage unchanged.

**Manual rollback** (the Rollback button) needs the previous version to be a published release as well. Automatic rollback after a failed start does not.

## Still unverified

- A genuinely fresh PC (missing prerequisites, first model download).
- A real published release and its GitHub `digest`.
- The live Hub PC with its cameras and Voice.
- Real speech latency under load.
- Hub cameras on a real Hub; only a protocol-faithful double was used.
- The first crowd frame after start is still 450-625 ms cold.
