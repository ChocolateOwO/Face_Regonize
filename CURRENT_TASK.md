# Reconize Current Task

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
