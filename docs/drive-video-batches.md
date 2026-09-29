# Google Drive video batches in Local Video Experiment

The user verified the Main white-screen correction and authorized a normal
commit/push of the scoped Drive video work on 2026-09-29. Verification notes
below describe the prepublication state; Git history records the published
commit. User videos, results and local patch backups are excluded.

Admin-only, local processing. The existing local-video upload and all older
single-video records/reports remain supported. This feature does not use Legacy
Drive photo processing, Event Batch, check-in writes or a different recognizer.
It uses one read-only enrollment/model snapshot for the entire batch.

## Selection and grants

Open `/local-video-experiment`, choose **Google Drive batch**, and verify the
connected account. **Reconnect with read-only access** requests exactly
`https://www.googleapis.com/auth/drive.readonly` plus the existing
`https://www.googleapis.com/auth/drive.file`, using the same OAuth project and
registered callback. In that project's Google Auth Platform / Data Access,
declare `drive.readonly` while retaining `drive.file`. No full `drive` write
scope is requested. Allow both requested permissions in Google's one-time
consent popup. The existing callback returns to Event Photos in the popup;
the video page detects success, closes it and refreshes the account. Closing
the popup and clicking **Refresh account** is also supported.

Stored older grants with no verified read scope require reconnecting. Google's
actual token-response scopes are saved in the existing Setting table (no
schema change). Missing or declined read permission never implies access.
Readonly-only granular consent cannot replace the existing file-write grant.
Video API clients mint tokens requesting only `drive.readonly`; existing
writers and Picker still request only `drive.file`. Picker/configuration stay
available for other features, but there is no Picker step for video batches.

Paste a Drive folder link and click **List videos**. This does not download or
scan anything. Readonly consent allows listing/downloading arbitrary files
accessible to the connected account. Folder and child IDs are verified through
authenticated Drive API metadata, with a parent-membership check on selection
and again before download. Pasted URLs are never fetched. Link resource keys
are validated and passed only in Drive request headers; shortcuts are not
followed. Shared folders, Shared Drives (`corpora=drive`, `driveId`, both
all-drives flags), and paginated direct children are supported. An incomplete
listing or repeated page token produces an error instead of a false empty
result. Subfolders are counted but never recursively scanned.

An accessible empty folder, a folder containing only subfolders, unsupported
files, and unavailable/permission-denied folders have distinct messages.
Every direct file appears with its available metadata and, if unselectable,
its precise validation reason. There is no MIME query filter. A supported
extension, including case-insensitive MKV with generic/unexpected MIME, is a
candidate; Google documents/shortcuts are excluded. Actual container/codec
validation happens locally after the selected source downloads. No transcoding.

Official references: [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
[incremental consent](https://developers.google.com/identity/protocols/oauth2/web-server),
[Shared Drives](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives),
[resource keys](https://developers.google.com/workspace/drive/api/guides/resource-keys).

Choose checkboxes (or **Select all supported videos**), enter a batch name and
click **Start selected videos as one batch**. Limit: 1–32 distinct files;
16 GiB each, 12 hours, 1–120 source FPS, at most 3840×2160 pixels. Supported
containers: MP4, MOV, AVI, MKV, WebM, M4V; the codec must decode with the existing
OpenCV/FFmpeg runtime. Folder listing stops with an actionable error beyond
1000 direct children. Unsupported files remain visible but not selectable.
These limits accommodate longer seven-camera recordings without reserving
space for seven downloads. Each source requires its reported size plus 1 GiB
of available disk space; the reserve is checked again during every chunk.
Drive limits are frozen in new experiment/source metadata and CSV metadata
keys `input_max_bytes` and `input_max_duration_seconds`. Original local-upload
limits remain 512 MiB / 30 minutes; older results retain their original meaning.

The server freezes source IDs, filenames, versions, size/checksum metadata,
connected account ID and scan settings. Sources run in filename casefold order,
then Drive ID. A changed account/file/grant causes an explicit source error;
the worker does not silently substitute content, retry or re-download it.

## Progress and reopening a batch

After Start succeeds, its returned batch appears immediately, with per-video
pending/downloading/processing/terminal statuses. The page records its ID in
`/local-video-experiment?experiment=<id>`. Refreshing that URL reads the same
record and resumes status polling; it does not start, download or retry work.
Opening the plain experiment page restores an active batch, or the latest
listed Drive batch when no active job exists. Other records remain selectable.

During download, rows show transferred MiB. The public API omits private Drive
source metadata, so a total may be unavailable until validation; the UI states
that explicitly. Overall progress still comes from the backend. Failed polling
keeps the last valid result visible with an error and retries status reads.
Rejected or malformed Start responses show an actionable message. If Start's
response is incomplete, refresh the experiment list to check for a created
batch before trying Start again. Per-source errors remain visible alongside
completed and partial results.

## Timing and counts

Same existing combined rule for every source:

`next scan start >= max(previous scan finish + wait, previous scan start + 1/target)`

- Camera FPS: default 30, range 1–120; virtual availability limit, not a re-encode.
- Wait after completed scan: default 0.4 s, range 0–30 s.
- Target detections per second: default 2.5, range 0.1–30; maximum intended starts.

Each clip starts its own virtual clock at zero. Sequential decode discards
intervening frames, takes the latest distinct frame exposed by the virtual
camera, includes measured inference/matching work, and never queues scans or
duplicates a low-FPS source frame. Source FPS is unchanged. Camera/network
latency, VFR gaps and real capture drops cannot be reconstructed exactly from
nominal FPS. EOF is independently checked using the existing decoder audit;
reported frame counts are estimates, not proof of corruption.

Per-person `detection_count` counts sampled frames containing that identity,
at most once per identity per sampled frame **per video**. The combined count
is the sum of the source counts once. It does not deduplicate a person across
cameras, count passages, or measure accuracy. Matched and unknown face counts
count individual detector results across sampled frames. A duplicate face
match in a frame can raise matched-face count but not that person's frame count.
Unknown faces have no persistent identity. Unique matched people use enrolled
identity IDs, not display names. Total sampled/decoded frames sum source totals.

First/last timestamps remain nominal clip-relative seconds. Batch first is
the minimum source first; batch last is the maximum source last. They do not
describe the earliest/latest recording in a shared camera clock. Source rows
provide exact per-clip ranges; previews also retain observed source PTS.

Per-video achieved rate is completed sample intervals / virtual capture span.
Aggregate achieved rate is sum(source intervals) / sum(source spans), not a
concurrent seven-camera rate. Wall throughput is samples / batch wall time.
Batch/source `processing_seconds` includes downloads, validation, recognition,
checkpoints and EOF verification. Requested FPS/target may exceed achieved rate.

## Preview and manual review

One full JPEG per matched identity per batch, maximum image side 1920. Select
the strongest valid existing cosine match; equal scores retain earliest source
order, then earliest frame/detection order. Without a comparable score, keep
the earliest valid example. The box is that same detection's face box, scaled
using actual saved image dimensions. No crop or per-detection archive.

**View match** shows predicted name, source filename, timestamp, frame number,
score when meaningful, one boxed face and zoom. Reviews wait for the batch to
stop because the strongest example may still change. `Not reviewed`, `Correct`,
`Incorrect`, `Unsure` is saved separately for the exact displayed example.
It never changes automatic counts and never verifies every appearance.
Older records without a preview remain readable and are never reprocessed.

## CSV layout

UTF-8 with BOM; spreadsheet formulas in text are escaped. The original first
13 columns keep their meanings: `record_type,key,value,identity_key,name,
detection_count,first_timestamp_seconds,last_timestamp_seconds,manual_verdict,
review_meaning,preview_frame_number,preview_timestamp_seconds,preview_match_score`.
Single-video reports retain their original 13-column layout.

Batch reports append `source_video_key,source_filename,status,source_fps,
reported_frame_count_estimate,decoded_frame_count,verified_total_frames,
sampled_frame_count,detected_face_detections,matched_face_detections,
unknown_face_detections,processing_seconds,actual_video_detection_hz,
duration_seconds_nominal,error`.

- `metadata`: batch summary in key/value, frozen settings, count/timestamp/rate
  definitions, model/providers/thresholds, processing time and cleanup status.
- `video`: one source's status, FPS, duration, actual decoded/sampled frames,
  readable EOF total, face counts, achieved rate, time and error.
- `person`: one combined identity count, clip-relative min/max, verdict and the
  representative source filename/key, frame, timestamp and score.
- `video_person`: identity/source count and first/last timestamps in that clip.

Do not sum `person` and `video_person` rows together; they are two views of the
same counts. No accuracy metric is calculated without labeled ground truth.
No credentials, preview URL, absolute server path or exported face crop.

## Local lifecycle and retained files

All storage is isolated under the existing private experiment job directory:

- Temporary: `downloads/source.<container>` (may be a partial download).
  Exactly one source at a time; chunked download, bounded size and at least
  1 GiB disk reserve. Remove after its scan and EOF check stop, including
  validation failure, download failure and cancellation. HTTP timeout 30 s;
  cancellation is checked between chunks and inference completes safely.
- Retained: `state.json` with source checkpoints and aggregate results;
  `previews/<identity_key>.jpg`, at most one per matched batch identity;
  `reviews.json` after an admin saves a verdict. CSV is generated on demand,
  not a retained copy of a source video. Atomic `.writing` files are temporary.

Startup removes stale batch `downloads` directories, preserves completed and
partial checkpoints, marks active work interrupted, and never starts a worker.
Totals are recomputed by replacement from source checkpoints, not increments.
There is no in-place retry for a stopped batch. To retry, explicitly create a
new batch; the old results remain separately identifiable.

A failure in one source preserves its completed samples and continues with
later sources; mixed outcomes are `completed_with_errors`. Cancellation marks
remaining sources `not_processed`. Disk/cleanup errors are explicit; after
fixing permissions use Delete on the stopped experiment if cleanup failed.
Delete uses the existing confined job-directory action, removing its retained
records, reviews and previews, without affecting other experiments/data.

Original Google Drive files are **never modified or deleted**. The feature
calls Drive `about.get`, `files.list`, `files.get`, `files.get_media` only.
Results/previews/verdicts/CSV and folder/batch APIs require authenticated admin
access. Preview bytes use the existing authenticated endpoint, not public URLs.

## Main verification and changed files (2026-09-29)

No commit or push; HEAD remains `f27a9d5` on `main`. No dependency, environment,
credential, schema, model, threshold, Dummy or UrgentBlur changes. Existing
recognition/Event/page/config hashes and old experiment state remain unchanged.

Changed/new files:

- `backend/app/api/local_video_drive.py`
- `backend/app/api/local_video_experiment.py`
- `backend/app/services/drive_video_source.py`
- `backend/app/services/drive_video_batch.py`
- `backend/app/services/local_video_experiment.py`
- `backend/app/main.py`
- `backend/tests/test_drive_video_batch.py`
- `frontend/src/components/DriveVideoBatchPanel.tsx`
- `frontend/src/components/VideoBatchSources.tsx`
- `frontend/src/components/VideoMatchPreview.tsx`
- `frontend/src/pages/LocalVideoExperiment.tsx`
- `frontend/tests/drive-video-batch.test.mjs`
- `frontend/tests/local-video-results.test.mjs`
- `frontend/tests/video-match-preview.test.mjs`
- `docs/drive-video-batches.md`
- `CURRENT_TASK.md`
- `PROJECT_CONTEXT.md`

Results:

- Focused Drive/OAuth/single-video/scan/Picker/destination suite: **144 passed**. Includes
  seven sources, per-frame identity deduplication, aggregate replacement,
  unknown faces, representative selection/scaling/source timestamp, CSV and
  verdict retention, partial failure, cancel, interrupted download, restart,
  no duplicate replay, admin access, size/checksum/version/disk validation,
  zero matches, low FPS, slow inference and terminal-path cleanup. Existing
  synthetic MKV/VFR and genuine truncation tests remain passing.
- Relevant frontend suites: **30 passed** (link-only Drive input, video results/modal,
  navigation, scan settings and Always On regressions).
- Full final Main backend suite: **732 passed**, isolated DB/storage, no real Drive.
- `npm run typecheck` and frontend build: passed. No dependencies added.
- Live 5173 disposable-browser preflight: seven checkboxes; selection idle;
  generic-MIME MKV and larger/longer candidates; no Picker request; one named
  Start with frozen defaults; source breakdown and combined names;
  boxed `cam07.avi` representative; proportional zoom; exact example verdict
  with unchanged automatic count. All experiment API responses intercepted
  with synthetic fixtures; no real user-media/Drive processing or Main writes.
- Separate unmocked browser health request from 5173 reached Main 8000,
  proven by its marker in the confirmed Main backend log. Served frontend
  source maps match Main page/panel files. Anonymous video/Drive APIs return 401.
- Read-only audit before/after startup confirms correct Main backend working
  directory/data, no pending migrations, unchanged schema/row counts and older
  experiment state. Existing recognizer warmed with 130 enrolled identities.
- Additional unrelated photo-download frontend suite: **5 existing failures**
  because its mock lacks `ExportSelectionPanel`. Its source/tests are unchanged;
  the normal Event/photo feature was not modified to address this separate issue.

Current runtime: Main frontend `http://localhost:5173`, Main backend
`http://127.0.0.1:8000`, launched from `frontend` and `backend` respectively.
Backend DB/storage paths in the existing `.env` are relative: always launch
from `backend`, not the repository root. Both services are left running.

## Listing diagnosis and consent change - 2026-09-29

Read-only metadata diagnosis against the existing connected account confirmed
its refreshed grant contains only `drive.file`, with no stored scope metadata.
Drive returned the folder but zero direct children (`incompleteSearch`
false), before any local filename/MIME filter ran. Therefore that zero result
was an API/grant visibility issue, not caused by the MKV filter. The real
children's MIME/size/duration could not be inspected under that grant and must
be verified after the user grants readonly consent. No real media downloaded.

Separately, the former MIME allowlist rejected supported MKV extensions unless
the MIME started `video/` or was exactly octet-stream/x-matroska. That gate is
removed and tested with generic/unexpected MIME. The former 512 MiB / 30 minute
Drive caps are replaced by the frozen Drive-only limits above. Synthetic MKV
VFR/metadata-overestimate and genuinely truncated media still produce honest
completed/partial-error outcomes, retaining names, previews and CSV.

The user previously added Picker configuration; it remains available to other
features. Video batches now need readonly OAuth consent rather than Picker.
No `.env`, credentials or project configuration were edited. Only authenticated
folder/grant metadata was read during diagnosis; automated tests never use real
Drive. Main data and existing experiment state are protected.

Final runtime verification: only confirmed idle Main backend PID 25348 was
stopped, with immediate port/command/cwd and active-job guards. Main now listens
on 8000 as PID 25536; Main Vite PID 23808 remains on 5173. The browser's unmocked
health marker reached that backend, unauthenticated Drive listing returned 401,
and served TSX source maps match Main. Read-only post-restart audit matches the
pre-change database schema/row counts and existing experiment state. Connected
account is verified; `read_access=false` and `requires_reconnect=true` correctly
identify the user's old grant. Real seven-file listing awaits user consent.

The existing EOF verifier retains its 600-second / 2 MiB-per-log resource bound.
Exceeding it produces an explicit unverified partial result, never a false
corruption claim. Codec/container decode errors still fail honestly. No videos
are re-encoded, no real-media experiment was started, and no commit/push.

Files changed by this listing correction (earlier uncommitted work preserved):

- Backend: `app/services/google_drive_oauth_service.py`, `drive_video_source.py`,
  `drive_video_batch.py`, `local_video_experiment.py`; `app/api/local_video_drive.py`.
- Tests: `backend/tests/test_drive_video_batch.py`, new
  `backend/tests/test_video_drive_oauth.py`, `frontend/tests/drive-video-batch.test.mjs`.
- UI: `frontend/src/components/DriveVideoBatchPanel.tsx`.
- Documentation: this file, `CURRENT_TASK.md`, `PROJECT_CONTEXT.md`.

Manual test:

1. In the existing OAuth project's Data Access, declare `drive.readonly` and
   retain `drive.file` ([Google's scope setup](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)).
   No client/secret/redirect change, new project or full Drive write scope.
2. Sign in as admin at `http://localhost:5173/local-video-experiment`; choose
   **Google Drive batch**, verify the account, click **Reconnect with read-only
   access** once and allow both requested permissions. The video page refreshes
   after successful consent; close the popup and Refresh account if needed.
3. Paste the camera folder link; **List videos**. Check names, MIME/size/duration
   details and disabled-file reasons. No Picker. Only direct children are scanned.
4. **Select all supported videos**, confirm seven checked files, name the
   batch and choose settings. Defaults are 30 FPS / 0.4 s / 2.5 starts/s.
5. **Start selected videos as one batch**. Check one downloading/processing
   source at a time, batch progress and source FPS/frame/face counts. A source
   failure must remain visible while others finish.
6. When stopped, check combined names/counts and each source breakdown. Open
   **View match**, confirm source filename, timestamp, frame and boxed face;
   zoom, save a verdict and verify it in downloaded CSV without count changes.
7. Refresh/restart: completed work must remain and never restart automatically.
   For a separate test, Cancel during download/scan and check partial results.
   Delete a stopped test batch: only its local records/previews should disappear;
   original Drive videos and other experiments must remain.

One reviewed representative example does **not** verify every detection.

## White-screen correction verification - 2026-09-29

Read-only inspection found the existing user batch running with six selected
videos. Its Start response was 201 and detail polls
were 200. The API deliberately removed private `source_file` fields; the source
table nevertheless read `source.source_file.bytes` only when downloading.
A synthetic replay of this response shape captured the exact render TypeError
and blank React root. This frontend mismatch caused the white screen; successful
backend processing continued. No real video was rerun or copied, and neither
the backend nor its active worker was restarted/interrupted for the fix.

The correction changes only `frontend/src/pages/LocalVideoExperiment.tsx`,
`frontend/src/components/VideoBatchSources.tsx`,
`frontend/tests/local-video-results.test.mjs`, new
`frontend/tests/video-batch-progress.test.mjs`, and the three project task/context
documents. Safe download display and status recovery are described above.
Earlier uncommitted batch/listing work, backend behavior and HEAD are preserved.

Current correction checks: **144 focused backend tests**, **35 relevant frontend
tests**, typecheck and build passed. Synthetic seven-video browser testing on
live Main 5173 verified immediate pending progress, download rows, reload/reopen
with only one Start, partial source errors, names and CSV access, and visible
Start failure, with no console exceptions. Experiment requests were intercepted;
no real media/Drive or Main data writes. The full 732-test backend result above
belongs to the earlier listing change; no backend code changed in this repair.

Main remains on 5173/8000. To recover the user's current run, sign in and open
`http://localhost:5173/local-video-experiment?experiment=<existing-job-id>`.
Do not Start that batch again. At the latest read-only check, three sources were
completed, the fourth downloading and two pending, with no source errors.
User visual confirmation is pending. No commit or push.
