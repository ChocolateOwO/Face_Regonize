# Reconize Current Task

## Main Drive video GitHub publication - 2026-09-29

The user personally verified the Main white-screen fix and explicitly asked to
push the verified work to GitHub. Commit/push approval applies to the scoped
Drive video batch implementation, link-only Drive listing, recovery fix, tests
and documentation already in this worktree. Main code needs no new change.
Branch: main, tracking origin/main; both were f27a9d5 before publication.
Exclude unrelated .vscode and all ignored patch backups, user video/results,
database, storage, logs, .env and credentials. The existing six-video user batch
is completed; publication must not start or rerun it. Main stays on 5173/8000.
The documentation originals were hash-verified at
patch/2026-09-29_2357_github_publication/original before this status update.
Stage: USER VERIFIED MAIN / GITHUB COMMIT AND PUSH AUTHORIZED. Git history and
the push response establish the resulting commit and remote state.

## Main Drive batch white-screen correction - 2026-09-29

Explicit Main-only frontend repair; no commit/push. Earlier uncommitted work
is preserved. Pre-edit copies/hash manifest are under ignored
patch/2026-09-29_140723_video-batch-white-screen/original. HEAD f27a9d5 unchanged.

Read-only inspection confirmed the user's existing six-video batch was running.
Start returned 201; subsequent detail polling returned 200. No new real batch,
download, retry, deletion, worker interruption or backend restart was initiated.
At the latest inspection, sources 001-003 completed, 004 was downloading, and
005-006 were pending, with no source errors. Main backend PID 25536 remains
running from backend on 8000; Main Vite PID 23808 remains on 5173.

Proven cause: public batch responses intentionally omit private source_file
metadata, but VideoBatchSources dereferenced source.source_file.bytes when a
source became downloading. A synthetic browser replay of that actual response
shape captured TypeError: Cannot read properties of undefined (reading 'bytes'),
and React cleared the root despite successful Start/poll responses. The user's
browser tabs were unavailable to the UI helper; the replay used an isolated
synthetic browser, not the user's session or media.

Downloading rows now display transferred MiB safely when the total is absent.
Start selects the returned batch immediately and records its ID in the URL.
Reload/reopen reads that existing batch (or the active/listed batch), never
starts another worker. Invalid Start/status responses and polling failures
show visible actionable errors; the last valid progress stays visible.
Per-video errors, old results, names, previews, verdicts, CSV, permissions,
MKV support, frozen settings and temporary cleanup behavior are preserved.
No backend source/config/data, recognition, Event, Legacy, Dummy or UrgentBlur
changes were made for this correction.

Changed: LocalVideoExperiment.tsx, VideoBatchSources.tsx,
local-video-results.test.mjs, new video-batch-progress.test.mjs, this file,
PROJECT_CONTEXT.md and docs/drive-video-batches.md.
Focused backend regressions: 144 passed. Relevant frontend: 35 passed, including
synthetic seven-video Start/download progress, reload/reopen with only one
Start, partial failures, transient polling errors, rejected/malformed Start
responses and explicit failed-result recovery. Typecheck/build passed.
Live-5173 synthetic browser confirmed the same flow with no console exceptions;
all experiment APIs were intercepted and no Main data was written.

Manual recovery: open
http://localhost:5173/local-video-experiment?experiment=<existing-job-id>
while signed in as admin. Do not click Start again for that existing run.
Stage: FRONTEND FIX VERIFIED / EXISTING USER BATCH CONTINUES / WAITING FOR USER
VISUAL CONFIRMATION. No commit or push.

## Main link-only Drive video listing correction - 2026-09-29

Explicit Main-only user request supersedes Dummy-first workflow for this fix.
No commit/push. Preserve earlier uncommitted batch implementation and unrelated
.vscode. Pre-edit source backups/hash manifest: ignored
patch/2026-09-29_130808_drive-link-listing/original. HEAD f27a9d5 unchanged.

Actual metadata-only diagnosis: stored grant has no scope metadata; refreshed
token grants drive.file only. Drive can resolve the folder but returns zero direct
children before filters, with incompleteSearch=false. No real video download
or processing. Real child MIME/size/duration remains unverified until consent.
Separate restrictive MKV MIME gate removed; supported extensions are candidates
regardless of generic MIME, with actual decoder validation during processing.

Video start adds exactly drive.readonly while retaining drive.file through the
same OAuth project/callback. One-time reconnect required for older grants.
Existing writers/Picker still request drive.file; video token requests readonly.
Google's granted scopes stored in existing Settings, no schema change. Partial
readonly-only consent cannot overwrite the existing file connection. Video UI
has paste link / List videos / checkboxes, no Picker or automatic processing.
Authenticated metadata validates folders, parents, shared-drive/resource-key
access, pagination, and account; access/empty/unsupported results differ.

New Drive limits: 16 GiB / 12 hours per file, max 32 selected, 1-120 source FPS,
4K pixels, reported size plus 1 GiB reserve before download and per-chunk disk
checks. One source at a time. Frozen limits included in new state/CSV; local
upload stays 512 MiB / 30 minutes. Existing counts, timing, previews, verdicts,
CSV field meanings, partial failures, restart protection and cleanup preserved.

Focused final backend: 144 passed (mocked Drive/OAuth, synthetic MKV/VFR and
truncation, old video/scan/Picker/destination regressions). Relevant frontend:
30 passed. Typecheck/build passed. Synthetic live-5173 browser verified seven
checkboxes with generic-MIME MKV and 2 GiB/one-hour candidate; no Picker; idle
listing/selection; one batch; matched names, per-source counts, boxed source
preview, zoom, verdict and CSV. No Main data writes in preflight. Five unchanged
photo-download frontend tests still fail because their mock omits
ExportSelectionPanel; no unrelated fix. Full final backend: 732 passed. Main
backend PID 25348 was immediately verified idle and stopped alone; updated
Main PID 25536 serves 8000. Existing Main Vite PID 23808 remains on 5173. Both
health paths pass; an unmocked browser marker reached confirmed Main backend,
unauthenticated listing returns 401 and served TSX matches Main. Post-restart
schema/row-count/old-experiment-state audit matches pre-change baseline.
Actual account verifies connected; readonly access is false until user consent.
See docs/drive-video-batches.md for final verification/manual
consent steps. Main DB/schema and older experiment protected; no environment,
credentials, thresholds, models, Event, Legacy, Dummy or UrgentBlur changes.

Stage: MAIN IMPLEMENTED / WAITING FOR USER REAL DRIVE CONSENT + MANUAL TEST.

## Earlier Main Drive batch implementation (grant flow superseded above)

User explicitly requested implementation directly in Main, no commit/push.
Dummy, UrgentBlur, Legacy Drive, normal Event Batch, recognition configuration,
model weights, enrollment and real data are protected. Original Main source
was backed up under the ignored patch folder. Initial HEAD f27a9d5; unrelated
untracked .vscode is untouched. No dependencies, schema or environment edits.

Implemented admin-only Drive input alongside the existing local upload on
/local-video-experiment. Reuses existing OAuth/Picker grant/token flow with
drive.file only; strict Drive API folder/file verification, no arbitrary URL
downloads, no Drive mutations. Folder Picker plus multi-video Picker confirms
missing per-file grants. Named batches freeze selected versions/account and
all three scan settings. One source is downloaded/decoded/checked/removed at a
time. Shared decoder keeps existing timing, threshold and count semantics.

Per-source checkpoints feed replacement aggregation (no increment duplication).
Mixed failures preserve successful/partial sources and continue; cancellation
stops safely, marks remaining sources not_processed and removes downloads.
Restart cleanup never starts/retries a worker. Best deterministic full-frame
preview per batch identity retains source filename/frame/timestamp/bbox/score;
one example's verdict stays separate from automatic counts. Existing single
results and 13-column CSV remain readable; batch CSV appends source statistics
and breakdown rows. Full details: docs/drive-video-batches.md.

Verification: full Main backend 716 passed; 20 new mocked-Drive/synthetic-video
tests plus existing video/scan regressions passed. Relevant frontend 28 passed;
typecheck/build passed. Disposable browser on live Main 5173 verified seven
checkboxes, idle selection, one named batch, frozen defaults, per-source and
combined counts, source preview box, zoom and verdict without count changes.
All experiment responses were synthetic/intercepted; no real Drive download
or user-video processing. Five extra, unrelated photo-download frontend tests
fail in unchanged code because their mock omits ExportSelectionPanel; normal
Event/photo source/test files were left unchanged.

Main runs on 5173 with its existing proxy to Main 8000. Backend must run from
Reconize/backend: .env DB/storage paths are relative to that working directory.
Read-only audit confirmed actual Main has no pending migrations; startup uses
130 enrolled identities and preserves schema/counts/old experiment state.
An initial root-working-directory audit saw the older root DB; corrected
before startup, with no DB modification. Synthetic health probe reaches the
confirmed Main backend. Protected recognition/Event source hashes are unchanged.

Existing OAuth connection is present. Initially Picker lacked
GOOGLE_PICKER_API_KEY and GOOGLE_CLOUD_PROJECT_NUMBER. The user supplied both
settings and explicitly requested backend reload on 2026-09-29. After checking
Main PID 2292's port/command/backend working directory and no active jobs, only
that backend PID was stopped. Main backend now runs as PID 25348 on 8000;
frontend PID 23808 on 5173 was preserved. Health on 8000 and through 5173 passed;
Picker config reports enabled=true, connected=true, missing=[]. Read-only
post-start audit confirms schema/row counts and old experiment state unchanged.
No agent code/config/credential edit, Drive request, download, commit or push.
CURRENT_TASK.md and docs/drive-video-batches.md runtime notes updated only.
Actual Google grant/file access still awaits the user's manual test.

Stage: IMPLEMENTED / AUTOMATED + SYNTHETIC VISUAL VERIFIED / WAITING FOR USER
REAL DRIVE MANUAL TEST. No commit or push. Main left running for manual test.
Original Drive files are never changed. Batch retains state.json, at most one
previews/<identity_key>.jpg per person and reviews.json; CSV generated on demand.
downloads/source.<container> removed after success/failure/cancel/recovery.

## Main 5173 video navigation/runtime correction - 2026-09-29

User requested Main-only diagnosis/correction and authorized a focused commit
and push if source changes are needed. Dummy, UrgentBlur files, media, storage,
databases and credentials are protected and untouched by this task.

Cause established: port 5173 listener PID 3708 is Vite whose working directory
is Reconize_UrgentBlur/frontend. Its HTTP-served App/Layout modules lack the
video route/sidebar entry. Initial Main HEAD 7374368 contained both. Main backend PID
24908 runs from Reconize/backend on port 8000. An HTTP 200 for a deep link was
only Vite's SPA fallback and did not prove the video route existed in the
running frontend. Earlier activation reporting failed to check that distinction.

Source correction: Layout reads authenticated /api/auth/me; only a verified
admin sees Local Video Experiment, with Admin experiment label, near CCTV.
Other navigation and kiosk chrome behavior are preserved. Focused fake-role
navigation tests added. Mandatory Main originals were backed up/byte-verified.
23 frontend regressions, npm run typecheck and build passed. Disposable browser
using the Main build, synthetic admin/video/results and intercepted APIs verified
visible sidebar, direct page, selection idle, explicit Upload/Start, default
controls, named result count and hidden non-admin entry, with no real API writes.
The user's signed-in browser is inaccessible to the available UI helper.

The user then explicitly authorized stopping only PID 3708 after an immediate
working-directory, command and port-owner recheck. All three checks passed;
only that PID was stopped. No process-tree stop, other existing process stop,
UrgentBlur file/data edit, backend restart or Dummy change occurred.

Main Vite now runs as PID 10800, explicitly rooted/configured in Reconize/frontend
on strict port 5173; Vite version 8.2.1, application VERSION unchanged. Served
App/Layout source maps match the actual Main files, including the video route
and admin-labelled entry. A disposable browser at http://localhost:5173 verified
the visible admin sidebar, direct video page, selection idle, explicit synthetic
Upload/Start with defaults, named result count 3 and hidden non-admin entry.
All experiment API responses in that test were synthetic/intercepted, with no
real media/data writes and no console errors. A separate real health fetch from
that browser reached Main backend 8000, proven by its unique request marker in
the confirmed Main backend log. Anonymous experiment API access remains denied.

Stage: MAIN ACTIVATED / WAITING FOR USER MAIN VERIFICATION. Focused commit/push
is explicitly authorized after staged audit; publication outcome reported in
the handoff. Refresh http://localhost:5173/ and check Local Video Experiment near
CCTV, or open /local-video-experiment directly. No real video was processed.

## Local Video Experiment and example match review - 2026-09-29

User explicitly authorized Dummy implementation/verification, Main promotion,
commit and push. Scope: local video and required three per-run scan settings.
Separate Legacy Drive Experiment remains Dummy-only.

Dummy verification: 41 focused video backend tests, 772 full backend tests
(235.359 s), 18 frontend result/modal/scan regressions, typecheck/build passed.
Actual modal visually checked at 1x and 2x using synthetic cartoon media; only
selected face boxed. No user video or real Drive was processed/contacted.

Main: surgically integrated local video routes/page/navigation and Always On
controls. Existing Event Batch, check-in/consent, CCTV, recognition configuration,
enrollment, schema and app data are preserved. Main originals backed up and
byte-verified before edits. No user data, credentials or absolute paths copied.
Defaults: requested/virtual camera 30 FPS, completed-scan wait .4s, target 2.5
starts/s; one awaited scan, no backlog, immutable run settings. Local media
stays under ignored per-server experiment storage. Independent EOF verification
separates metadata estimates from decode failures. One strongest valid cosine
whole-frame example per identity, earliest tie; same detection bbox scaled for
zoom preview. Admin-only image/verdict routes. Verdicts live separately from
automatic counts/state and cover one shown example only, never overall accuracy.
Older results remain unchanged without silent reprocessing.

Main verification: 45 focused video/scan backend tests, 696 full backend tests
(210.817 s), 18 frontend result/modal/scan regressions, typecheck/build passed.
Both backend/frontend pairs are active: Main 8000/5173; Dummy 8010/5180.
Direct/proxied health and video pages return 200; anonymous video/preview access
returns 401. Main excludes Legacy Experiment routes. Main schema and protected
identity/attendance/consent/user row counts are unchanged after activation, as
are 11 protected Main code/configuration and 13 existing Dummy result/config
hashes. No Dummy media or experiment storage was copied to Main.

Stage: VERIFIED AND ACTIVATED. User authorized normal commit/push after final
staged source/data audit; publication outcome reported in the handoff. User
hardware/manual verification remains needed. No version/tag/release changes.


## Urgent Event Photo Crowd Detection — 2026-09-08

The user paused Phase A and explicitly authorized this isolated urgent patch,
including a normal push to origin/main. This section supersedes older task labels.
Base: 41ba9b6bb896edbe6bdc7c555a88f081608eec88, verified against GitHub main.
Worktree: E:\งาน\491\Face_Reconize\Reconize_UrgentBlur.

Paused original Main remains untouched. At freeze, tracked and staged diffs were
empty; untracked files included .vscode/ and
backend/app/migrations/004_photo_batch_local_first.py. Phase A migration 004 had
already added source_type, source_status, local_status, drive_status and
workflow_version in Main's backend/database/app.db. It was NOT rolled back.
SQLite user_version remained 0. The sole batch
36a03b2808dd4700bd929ba7c90d6e39 was completed; no Python backend or port 8000
listener was present. Existing Phase A backups remain in
patch/2026-09-08_0851_phase-a-local-first-main/. Do not resume Phase A automatically.

Urgent implementation: Event-only global detection plus sequential 640px tiles
with 192px overlap. The shared detector retains its 320px input configuration.
Boxes and five-point landmarks map back to the original. Invalid boxes are
discarded; valid boxes are clipped. Deterministic geometry suppression prefers
non-boundary crops, then detector confidence and stable source order. It uses
IoU >= 0.4, or containment >= 0.8 with close centers. Surviving faces receive
existing normalized 512-dimensional embeddings from original-image alignment.
No image upscale, concurrent tile inference or additional model is introduced.

Only the Event worker's detection import changes. Existing matching thresholds,
per-face explicit-declined-only MEDIA blur and subsequent logo remain intact.
Detection alone never causes blur. Unknown, missing-consent and consented faces
remain visible. Download, Drive phases, stop/delete, recognition engine, kiosk,
schema and frontend are unchanged. Existing outputs are not reprocessed.

Verification: 15 isolated tiling/privacy tests and 13 existing download/privacy
tests passed (the latter repeats three privacy tests). Python compileall passed.
AST comparison confirms all existing Event worker executable logic is unchanged
except the detection import. Geometry tests use generated arrays and fake model
responses; they are not evidence of real-world recall or identity accuracy.
No production DB/storage or credentials are used in tests.

Read-only image-header audit found 79 existing originals at 3600x2400; none were
copied, decoded for recognition, or modified. The 640px tile choice limits the
unchanged 320px detector's reduction to 2:1 instead of 11.25:1 on those originals.
Real CUDA inference on generated blank arrays measured 1920x1080 global 14.08ms
versus tiled 133.51ms (8 tiles), and 6000x4000 global 22.15ms versus tiled 1914.85ms
(117 tiles), after model warmup. Both had 0 global/unique faces and 0 duplicates.
These single-run measurements show added detection overhead only, not crowd
recall, identity accuracy, or representative per-face embedding cost.

This worktree is prepared for the user-authorized urgent commit/push. Push outcome
is verified and reported separately. The original Main runtime is not deployed
or restarted by this task. Actual crowd-photo visual verification remains needed.

## Event Photo Download During Drive — 2026-09-07

Current task supersedes older active-task labels. User explicitly authorized
direct Main implementation of Download after local Phase 1, including existing
syncing_drive batches. Prior logo/explicit-deny/delete work was manually approved
and published as e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559.

Implementation: authenticated GET /api/photo-batches/{batch_id}/download;
local completion status/counters plus batch-owned photo/file manifest checks;
SORTED, optional REVIEW, MEDIA only. No ORIGINAL/AMBIENCE/LOGO, arbitrary files,
secrets or other batches. Temporary ZIP uses bounded server memory, lives outside
batch storage and is removed after send/failure/disconnect. Supported Chromium
localhost/HTTPS browsers stream the response to the chosen disk file; other
browsers use the existing Blob-download pattern (large ZIPs need more resources).
Batch list/detail expose Download; processing/stopping/deleting stay disabled.

Drive Phase 2 was audited: final local image bytes are read, not rewritten or
deleted. Download holds no processing lock and closes its DB read transaction
before packaging. Processing/Drive functions, blur/logo, cancellation, schema,
GPU and all unrelated features are unchanged. Historical Gallery stays PAUSED;
future manual Drive upload remains out of scope.

Backup: patch/2026-09-07_1436_event-photo-download-during-drive/.
Five existing source/docs originals verified SHA-256 equal before edits.
Changed: backend/app/api/photo_batches.py, frontend/src/pages/PhotoBatches.tsx,
frontend/src/pages/PhotoBatchDetail.tsx, CURRENT_TASK.md, PROJECT_CONTEXT.md.
Added: backend/app/services/photo_batch_download_service.py,
backend/tests/test_photo_batch_download.py,
frontend/src/components/PhotoBatchDownload.tsx,
frontend/tests/photo-batch-download.test.mjs. Patch NOTES.md records the backup.

Isolated verification: 13 backend tests (10 download plus 3 existing MEDIA policy
checks), 5 frontend behavior tests, production build/TypeScript and lint (same
three unrelated warnings). Real Phase 2 functions ran against temporary SQLite
and fake Drive: ZIP completed while upload was blocked; upload then continued to
the next image and completed. No production test records or output writes.

Read-only Main eligibility audit: batch 36a03b2808dd4700bd929ba7c90d6e39 was
syncing_drive, 79/79 processed, no local failures. Full manifest validated:
207 SORTED, 48 REVIEW, 79 MEDIA files (1,423,478,361 bytes). No reprocessing needed.

Status: IMPLEMENTED / TESTED; WAITING FOR SAFE BACKEND RESTART, THEN MAIN
VERIFICATION. Current backend was deliberately NOT restarted: its active Drive
upload must not be interrupted. Health 200; running OpenAPI still lacks Download.
On-disk implementation is not yet a live endpoint. Do not claim otherwise.
After upload finishes, safely reload Main backend, refresh /photo-batches, and
verify Download ZIP, folder contents, current blur/logo and unchanged Drive
behavior. Concurrent syncing behavior is automated-tested; user verification
remains required after activation. Do not restart an active upload to test it.
No commit/push/version/tag/release for this task.

## Event Photo explicit-deny MEDIA blur — 2026-09-07

Current task: user-authorized direct Main policy change. Status: WAITING FOR
MAIN VERIFICATION. This section supersedes older active-task labels below.

New MEDIA rule: use the existing reliable match and consent snapshot; blur a
face only when person_id exists and consent_status_at_processing is declined.
Consented, pending, missing-consent and unknown/unmatched faces remain visible.
Decisions remain per face. Existing outputs are not reprocessed by this change.

Only the MEDIA predicate and its comments changed in photo_processing_service.py.
Existing matching, consent storage, ORIGINAL/SORTED/REVIEW, logo ordering,
cancellation/delete and Drive phase logic are preserved. Historical Gallery
remains PAUSED / DUMMY EXPERIMENT ONLY. Manual Drive upload is out of scope.

Backup: patch/2026-09-07_1400_event-photo-explicit-deny-blur/; current logo/delete
baseline and this document were SHA-256 verified before edits.
Verification: modified-file py_compile, backend compileall, three isolated tests
(nine decision cases, mixed-face pixels, logo-after-blur and empty face list)
passed. AST comparison confirms the predicate is the only executable production
change. Cancellation behavior was preserved by comparison, not revalidated as
a complete delete integration test. No production DB/storage was used in tests.
No frontend changes or tests. Main backend must load the changed module before
manual verification; it has not been restarted as part of this task.

Files: backend/app/services/photo_processing_service.py,
backend/tests/test_event_photo_explicit_deny.py, CURRENT_TASK.md, patch NOTES.md.
User verification on a newly processed batch is still required.

## Event Photo MEDIA Logo Overlay — 2026-09-07

Active task: Main implementation and verification. Historical Face Gallery is PAUSED / DUMMY EXPERIMENT ONLY. Event Photo now stores an optional per-batch PNG and JSON configuration under that batch's local storage; logo compositing is MEDIA-only after existing PDPA blur. No schema, recognition, PDPA, or Drive Phase 1/Phase 2 change. Status: WAITING FOR MAIN VERIFICATION after automated checks.

## Reproducible Windows Installation — 2026-09-07

Active task: make current Main reproducibly installable on a new Windows PC.
Main-only setup/documentation work adds pinned GPU runtime dependencies, CPU/GPU
Windows setup, actual InsightFace CUDA preflight, Node declaration, credential
ignore coverage and current-feature documentation. No Main database, storage,
participant data, recognition configuration, CCTV behavior, Event Photo
behavior or Dummy content was changed. Automated verification passed and user
approved one normal GitHub commit/push. No release or VERSION change is part of
this task. Existing Main CCTV and navigation manual-verification status below
remains unchanged.

## Main GitHub Publication — 2026-09-07

The user authorized publishing the current approved Main source and rebuilding
only the single unpublished local commit above published base
`e26a6e4e0ad4f64d224c0397f43edad614f057a5`.

Publication excludes local patch backups and the five real face test JPGs.
Their local copies are preserved and ignored. Previously published face images
remain in older GitHub history; published history is not rewritten.
No Dummy-only Gallery, Logo, Event Photo, or CUDA changes are included.

Pre-publication checks passed: frontend production build including TypeScript;
lint with three existing warnings; syntax validation of 57 backend Python
files; isolated in-memory recognition-index, node-presence, and live-event
checks. No Main database connection, migration, seed, or application data
mutation was performed by these checks.

This commit records the prepared publication state; remote push confirmation
is reported separately after verification. Publishing does not mark the Main
CCTV or navigation manual-verification tasks below complete.

## Current Status

The current active task is Main verification after applying the approved CCTV feature set.

The CCTV feature had already passed Dummy testing and received explicit user approval to move to Main.

The approved CCTV functionality includes:

- one unified CCTV Live View
- support for multiple physical cameras on one Windows PC
- approximately 7 cameras
- persistent camera checkbox selection
- stable deviceId → cameraId mapping
- camera restore after navigation / refresh
- smooth live MediaStream previews
- no recognition-driven camera flicker
- face count per camera
- Activity selection
- Start Detection
- Stop Detection
- integrated per-camera recording
- multiple simultaneous camera recordings
- Live Recognition Feed
- 5-second participant visibility refresh
- Global Session History
- Per-Camera Session History
- old separate Camera workflow removed from visible navigation

---

## Current Main Verification Issue

After the CCTV feature was applied to Main, the frontend had a syntax error in:

`frontend/src/pages/CameraNode.tsx`

The reported Vite / TypeScript errors were:

- Expected ',' or ')' but found ';'
- TS1005: ')' expected
- TS1005: '}' expected

The issue was diagnosed as one missing closing line for the `tick` arrow function inside:

`startAgentHealthPoll()`

The only required syntax fix was inserting:

```tsx
    };
```

immediately after the outer `.catch(() => { ... })` chain and before:

```tsx
    tick();
```

No other logic or code was intended to change.

---

## Current Verification State

The corrected `CameraNode.tsx` has been prepared.

The syntax correction was checked independently and the original parser errors disappeared.

However:

**THE USER HAS NOT YET CONFIRMED THAT MAIN BUILD AND MAIN CCTV NOW PASS AFTER THE FIX.**

Therefore this task is still ACTIVE.

Do not mark it complete.

Do not begin another feature automatically.

---

## What Codex Must Do If Asked To Continue This Task

If the user asks Codex to continue the current task:

1. Read `PROJECT_RULES.md` first.
2. Inspect the current Main file:
   `frontend/src/pages/CameraNode.tsx`
3. Confirm whether the missing `};` fix is already present.
4. Do not make unrelated code changes.
5. Do not refactor `CameraNode.tsx`.
6. Do not modify recognition logic.
7. Do not modify Main data.
8. Run only safe build/type checks as needed.
9. If another syntax/build error appears, diagnose only that error.
10. Report the exact cause and exact minimal fix.
11. Wait for the user to verify Main.

---

## Do Not Start Future Features

Do not start:

- multi-PC camera nodes
- phone/iPad recognition
- WebRTC remote CCTV
- real-time People page
- real-time PDPA page
- networking / recognize.local fixes
- other architecture work

unless the user explicitly requests one of them.

---

## Completion Condition

This current task is complete only after the user personally confirms that:

- Main frontend builds successfully
- CCTV Live View opens successfully
- the approved CCTV behavior still works on Main

Until then:

**STOP after reporting current-task results and wait for user instruction.**

---

## Approved Admin Navigation Performance Change

The user manually tested the isolated Dummy performance change and explicitly approved a surgical Main application on 2026-09-06.

Applied Main scope: return-navigation background-refresh caching for Dashboard, People, Attendees, PDPA, and Connect; successful mutation cache invalidation; and a 10-second Connect network-information cache. No schema or Main data change is involved.

Patch backup: `patch/2026-09-06_2303_admin-navigation-performance/`, with byte-level SHA-256 verification completed before Main edits.

Status: **WAITING FOR MAIN VERIFICATION.** The user must verify the listed performance behavior, data freshness, other sidebar pages, and CCTV. This does not resolve or alter the separate unresolved CCTV Main verification above.

---

## Approved Full-Menu Navigation Performance Follow-up

The user manually tested the latest Dummy full-menu performance result and explicitly approved Main application on 2026-09-07.

Applied Main files: `frontend/src/pages/Reports.tsx`, `backend/app/api/reports.py`, `backend/app/api/people.py`, and `backend/app/api/attendees.py`. The change adds Reports return-navigation refresh/immediate shell and replaces Dashboard, Reports, People, and Attendees full-table or per-person attendance summary reads with database aggregates. No schema or Main data change occurred.

Patch backup: `patch/2026-09-07_0001_admin-navigation-performance/`, with SHA-256 verification completed before edits.

Main automated checks: frontend production build/TypeScript passed; lint completed with existing unrelated warnings; backend syntax validation passed.

Status: **WAITING FOR MAIN VERIFICATION.** This does not resolve or alter the separate unresolved CCTV Main verification above.
