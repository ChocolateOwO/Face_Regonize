# Reconize Current Task

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
