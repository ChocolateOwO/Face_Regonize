# Local Video Experiment

Admin page: /local-video-experiment (development URL http://localhost:5173/local-video-experiment).
Existing Event Photo and Google Drive pipelines remain unchanged. No separate
Legacy Drive Experiment is included in this feature. No Event Batch, attendance, consent,
enrollment, model weights, threshold setting or database schema is modified.

## Workflow

1. Sign in as admin. Select a local video. Selection makes no request.
2. Click **Upload video**. Raw bytes stream to a server-generated filename.
   Media header, first-frame decode, dimensions, FPS, frame count, duration and
   byte count are validated. Upload progress reaches 100% before validation
   finishes. Recognition has not started when status becomes **ready**.
3. Click **Start experiment**. One worker decodes sequentially and counts faces
   only on sampled frames. Other local video experiments cannot run concurrently.
4. Inspect progress, elapsed processing time, matched names, unknown counts and
   expandable per-sample frame indices/timestamps. Download CSV at any time.
5. **Cancel** stops before the next decode/inference checkpoint.
   An in-flight inference finishes and its complete frame results are saved.
   **Delete** becomes available after stopping and removes only that job directory.
   Completed, cancelled, failed and interrupted jobs cannot be started again;
   upload a fresh experiment for another run.

## Media, sampling and reproducibility

Limits: 512 MiB per upload, 1800 seconds nominal duration, 1-120 source FPS, no
more than 3840 x 2160 pixels per frame. MP4/MOV/M4V, AVI, MKV and WebM container
extensions are accepted; actual media must have a supported container header
and decode using existing OpenCV's FFmpeg backend. Unsupported codec, empty,
incomplete or invalid upload produces an error and its temporary job is removed.
Playlists, image sequences, paths and network sources are rejected. Re-export
problem files as constant-frame-rate MP4/H.264 or AVI/MJPEG.

Sampling method **configured-camera-v3**. Admin selects Camera FPS (default 30,
range 1-120), wait after completed scan (default .4s, range 0-30), and target
detections/s (default 2.5, range .1-30) before Start. Controls freeze at Start.

Next virtual scan deadline = max(previous capture + measured scan work + wait,
previous capture + 1/target). Select the latest source frame available at the
latest virtual-camera tick before that deadline. If already inferred, wait for
the next tick exposing a distinct frame. Capping virtual availability does not
alter or re-encode the uploaded video. Sequential decode discards intervening
frames; no queue, duplicate saved-frame inference or real-time sleep.

Defaults reproduce historical completion +400ms; target adds no second delay.
See [scanning settings](scanning-settings.md) for exact virtual tick/index formulas,
all controls, rate definitions and Always On behavior. Read [historical evidence](local-video-historical-rate.md)
for the old camera source. Maximum samples assumes zero work with selected
controls; it is not an expected result. Early decode failure preserves partial
results. Frame timestamps are nominal index/source FPS, not VFR container PTS.

Measured work varies with hardware/load, so actual selected indices can differ
between runs. Per-sample indices, nominal timestamps, virtual capture time and
work are stored for audit. Synthetic tests inject deterministic work.
Old completed/partial nominal-fps-v1 and historical-camera-v2 records retain
their stored rules and counts. Old ready uploads adopt v3 only on explicit Start.

Shared engine.detect_faces() performs a whole-frame single pass with existing
buffalo_l model and shared inference lock. No tiled Event detector, extra model,
resize, tracker or temporal smoothing is introduced. Private RecognitionIndex
uses existing match_batch() and enrolled identity embeddings/names read
once at run start. Current cached match threshold, detector threshold/size and
actual providers are captured, not changed. Later enrollment edits do not
change the running experiment.

Compare runs using the same video SHA-256, source FPS/frame count, sampling rule,
enrolled identity snapshot, model weights, providers, thresholds and OpenCV
version and stored per-sample work/index/capture times. Measured work can change
frame selection between runs. Provider numerical differences can change borderline matches. Metadata
captures provider/configuration and enrolled identity count; embeddings and
credentials are never exported. EOF verification uses the installed FFmpeg executable on PATH (no new Python dependency).
Processing time measures wall-clock time from
worker start through snapshot preparation, sequential decode, inference, matching
and durable checkpoints plus EOF validation; upload and initial validation are excluded.

## Exact count meanings

- Per-person **detection_count**: successfully processed sampled frames containing
  that enrolled identity. At most one increment per identity per sampled frame,
  even if multiple faces match it. Equal names remain separate identity rows.
- **unique_matched_people**: enrolled identities matched at least once, not all
  people visible in the source video.
- **matched_face_detections**: individual detected faces matched to identities
  across sampled frames; duplicates within a frame count here.
- **unknown_face_detections** (UI: Unmatched face detections): detected faces
  without a threshold-passing match, summed across sampled frames. Unknown faces
  are not tracked; repeated sightings count again.
- **frames_with_unknown**: sampled frames containing at least one unknown face.
- **sampled_frame_count**: frames whose inference/count checkpoint completed,
  including frames with zero faces.
- **decoded_frame_count**: decoded frames, including nonsampled frames. Cancel
  during decode can leave a frame decoded but not inferred; that frame does not
  contribute to sampled_frame_count.
- Per-sample **faces**: individual detector results.
  **matched_people**: distinct matched identities in that frame.
  **unknown_detections**: unmatched face results in that frame.
- **video_frame_count**: source metadata, not successful decode count.
  **enrolled_identities**: identity rows in the frozen matching snapshot.

Counts are not camera passages, unique unknown people or recognition accuracy.
No labeled ground truth is available; no accuracy is calculated. First/last
timestamps identify sampled sightings, not entry/exit times.

## CSV layout

One rectangular CSV uses UTF-8 with BOM (Excel-friendly), standard CSV quoting
and CRLF. Spreadsheet formula-looking text starts with an apostrophe.

Columns (13):
record_type,key,value,identity_key,name,detection_count,first_timestamp_seconds,last_timestamp_seconds,manual_verdict,review_meaning,preview_frame_number,preview_timestamp_seconds,preview_match_score

- **record_type=metadata**: key/value contains metadata; other columns blank.
- **record_type=person**: one row per matched identity; key/value blank.
  identity_key is an experiment-local pseudonymous key distinguishing equal
  names. Name is enrolled full name. detection_count is sampled-frame count.
  First/last timestamps use nominal seconds.
- Zero matches produce header/metadata with no person rows.
  Active/cancelled/failed/interrupted reports explicitly flag partial results.
  Download snapshots the latest durable complete frame.

Metadata keys: experiment_id, status, partial_results, filename, video_sha256,
video_duration_seconds_nominal, video_fps, video_frame_count, decoder,
opencv_version, sampling_hz, sampling_method, post_scan_delay_seconds,
camera_fps, target_detections_per_second, effective_available_fps_limit,
historical_reference, timing_differences, max_scan_hz, actual_video_detection_hz,
processing_throughput_fps, decoded_frames_not_inferred, scan_work_seconds,
max_sample_count_zero_work, sampled_frame_count,
decoded_frame_count, processing_seconds, model, provider, detector_provider,
match_threshold, detection_threshold, detection_size, enrolled_identities,
unique_matched_people, matched_face_detections, unknown_face_detections,
frames_with_unknown, started_at, finished_at, error, count_meaning.

CSV is generated from experiment state on authenticated request; no temporary
report file is placed in shared storage.

## Isolation and restart

Storage: backend/local_video_experiment_storage/<32-hex-experiment-id>/.
Generated video filenames, atomic state snapshots, per-identity preview images
and separate example-review records live there.
Outside normal storage, Git-ignored, not statically served.
Every upload/list/detail/start/cancel/delete/video/CSV route checks authenticated
user **role=admin**. Media/CSV responses have Cache-Control: no-store.
Video retrieval requires bearer authentication; no token in URL.

Delete validates resolved target, rejects active jobs and removes only that
directory. No Drive request or database write exists in this runner.
At startup persisted uploading/running/cancelling jobs become **interrupted**
without spawning workers. Checkpointed counts remain; ready/completed jobs retain
status. Nothing silently restarts.

## Verification / manual test

Tests use generated 64 x 64 MJPEG videos, fake detector outputs, temporary
identity DBs and guards against real Google Drive. Coverage: validation/limits,
streamed bytes/hash, fractional/low FPS latest-frame sampling, slow/variable scan
work, preserved earlier reports, equal names/per-frame deduplication,
unknown counts, zero-result/CSV/BOM/formula escaping, cancellation, decode failure,
restart, Delete isolation, unauthenticated/non-admin access, read-only snapshot.

Manual check: select your video and verify no upload/processing starts; Upload
and verify ready with zero samples; Start, inspect progress/timestamps/names;
download CSV; on a longer experiment Cancel, verify partial CSV and Delete.
No real Drive experiment is run as part of development preflight.

## EOF and frame totals (2026-09-28 fix)

OpenCV's CAP_PROP_FRAME_COUNT is a reported estimate, not a count of decoded
images. Its FFmpeg backend may calculate duration * nominal FPS when nb_frames
is absent. A VFR MKV or recording with timestamp gaps can therefore have fewer
readable images than this estimate and still end normally. Neither a short count
nor a long count alone establishes corruption.

After the recognition pass reaches read(False), the runner performs an independent
sequential FFmpeg decode to the null output, with error detection and passthrough
frame timing. This does not re-encode the uploaded file, export images, or repeat
recognition. Completion requires a clean verifier exit, end marker and the same
readable-frame count as OpenCV. Known premature-file/corrupt-packet diagnostics
remain partial failures even if FFmpeg exits zero. Decoder disagreement is a
partial failure, never a guessed clean EOF. Missing FFmpeg is explicitly unverified,
not described as corruption. The verifier has a 600 second and 2 MiB per-log limit;
Cancel stops its child process. Temporary metadata logs are removed on exit.
FFmpeg is already installed here; no environment or dependency installation occurred.

While running or partial, progress is explicitly an estimate against the original
reported count. A verified completed run shows actual decoded / verified total,
and 100 percent; the original estimate remains visible separately. The zero-work
sample ceiling is recalculated from actual decoded frames for new completed runs.
Observed OpenCV source timestamp range/largest gap is shown separately. Scan v3
still uses nominal index/FPS and measured work; source timestamp gaps are not
replayed. Scan settings, source FPS, model, thresholds and per-person counting
are unchanged. Wall processing time now includes independent EOF verification.

The original first eight CSV columns keep their meanings; match review adds five columns described below. Additional metadata rows:
reported_frame_count_estimate, verified_total_frames, decode_outcome,
decode_verifier, decoded_timestamp_first_seconds, decoded_timestamp_last_seconds,
decoded_timestamp_max_gap_seconds, decoded_nominal_duration_seconds,
actual_max_samples_zero_work, detected_face_detections. video_frame_count retains
its original reported meaning; decoded_frame_count is the actual successful
OpenCV reads and sampled_frame_count counts complete inference checkpoints.
verified_total_frames is blank unless a clean EOF was independently confirmed.
Old states are never rewritten/recalculated; their audit outcome is
not_verified_in_original_run. Old reported counts, status and person counts stay
as originally recorded. Partial matches remain visible for failed/cancelled runs.
Empty results distinguish no detected faces from detected but unmatched faces.

Source evidence: [OpenCV FFmpeg get_total_frames](https://github.com/opencv/opencv/blob/4.x/modules/videoio/src/cap_ffmpeg_impl.hpp)
and [FFmpeg progress, xerror and passthrough options](https://ffmpeg.org/ffmpeg.html).

Retry: keep the old experiment for comparison; upload the same original as a NEW
experiment, leave 30/.4/2.5 or choose settings, then explicitly Start. Check clean
EOF, actual totals, unmatched/matched counts and CSV. Matching names is not
promised by the EOF fix. Do not delete the old run just to retry.

## Representative match review

Each matched identity row keeps its unchanged sampled-frame detection count and
adds View match. New runs retain at most one full-frame JPEG per identity under
that experiment's private previews directory. The strongest valid cosine
similarity wins; equal scores retain the earliest sampled frame and first
stable detector order within a frame. If no comparable score exists, the first
valid matched bbox is retained. Missing/invalid bboxes do not remove automatic
counts. Only improvements replace the one file; no per-detection archive or
face crop is saved. Preview storage/encoding is excluded from simulated scan
work but contributes to wall processing time.

The entire frame is retained, resized only for preview display to a maximum
1920 pixel side. Bbox coordinates come from that exact matched detection in
source-image coordinates and scale by actual output width/source width and
height/source height. The modal overlays two contrasting strokes around the
same one face, shows predicted name, one-based frame number (and zero-based
index), cosine score where valid, and source PTS when OpenCV exposes it (else
nominal index/FPS timestamp). Zoom is 1x to 4x with scrolling; box scales with
image content. No extra inference/model or threshold change is used.

Images are fetched with the existing authenticated API client and displayed
through a temporary Blob URL revoked on close. Admin-only endpoint:
GET /api/local-video-experiment/{id}/people/{identity_key}/preview?example_key=...
No public image URL, arbitrary path, credential or Drive transport is exposed.
Opening final examples and saving verdicts waits until processing stops. Image
fingerprints detect interrupted replacements; preview/state mismatches do not
silently reprocess the source video.

POST /api/local-video-experiment/{id}/people/{identity_key}/review accepts
verdict (Not reviewed, Correct, Incorrect, Unsure) and the exact example_key.
Verdicts are stored in separate reviews.json, keyed by identity/example, and
never change state.json recognition counts, settings or samples. Old experiments
with no preview say Preview unavailable for this older run and remain unchanged.
Delete removes that experiment's previews/reviews along with its upload/state;
other experiments and application storage are untouched.

CSV now has 13 columns, retaining the original first eight and adding:
manual_verdict, review_meaning, preview_frame_number, preview_timestamp_seconds,
preview_match_score. preview_timestamp_seconds is nominal index/source FPS,
consistent with existing sampled timestamps; the modal additionally displays
observed source PTS when available. Missing preview values are blank. Each
person's verdict covers only its one shown representative example and is NOT
recognition accuracy or verification of every counted appearance. Zero-match
results explicitly show sampled, detected-face and matched-face counts.

Verification: synthetic video/fake identities with known names/bboxes test
strongest selection, earliest ties, one file per identity, full frame/scaled bbox,
source/nominal timestamps, separate persisted verdicts/CSV, immutable automatic
results, admin restrictions, interrupted images, old results, zero matches,
Delete isolation, cancellation and restart. No user video was processed.

Promotion verification: Dummy focused video backend 41 passed, full backend 772
passed; Main focused video/scan backend 45 passed, full backend 696 passed.
In both environments 18 frontend regressions, typecheck and build passed.
Actual modal visually checked at 1x and 2x using synthetic frames with one
selected bbox. Main/Dummy direct and proxied health passed after activation;
anonymous video/preview requests return 401. No user video was processed during
development and no experiment data was copied to Main.

## Tester: upload and review a boxed example

1. Sign in as admin and open /local-video-experiment. Select a local video and
   click Upload video; selection alone does not upload or run recognition.
2. Choose the three controls before Start experiment. Defaults are 30 FPS,
   .4 seconds after completion and 2.5 maximum scan starts/second.
3. Wait for processing to stop. Check sampled-frame, detected-face, matched-face
   and per-person counts. Failed/cancelled runs retain any checkpointed matches.
4. Click View match at the end of a named row. Check predicted name, timestamp,
   frame number and score. Zoom and inspect the one boxed face in full context.
5. Select Correct, Incorrect, Unsure or Not reviewed; Save example verdict.
   Reopen to verify persistence and download CSV to check the person's verdict.
6. An older run without an image shows Preview unavailable for this older run.
   Upload a new experiment to obtain previews; no old video silently reruns.
7. Reviewing one example does not verify every detection or calculate accuracy.
   Cancel preserves partial counts; Delete removes only that experiment.
