# Per-run scanning settings

Applies only to Always On Check-in and Local Video Experiment. Historical
e4dbf60 remains a read-only reference. Legacy Drive, normal Event Batch, tap
recognition/consent/check-in semantics, models, thresholds and enrollment are
unchanged. No dependencies, environment or database schema changes.

## Controls and validation

| Setting | Default | Inclusive range | Always On | Saved video |
| --- | --- | --- | --- | --- |
| Camera FPS | 30 | 1-120 | getUserMedia frameRate ideal/max; negotiated track FPS shown when available | Virtual camera frame availability limit; source FPS unchanged |
| Wait after completed scan (seconds) | 0.4 | 0-30 | Wait after awaited scan completes | Same wait on simulated video clock |
| Target detections per second | 2.5 | 0.1-30 | Maximum intended scan starts/s | Same ceiling on virtual video time |

All values are finite numbers. Empty, nonnumeric, Boolean, out-of-range,
NaN/Infinity and unknown backend fields are rejected. HTML limits plus client
validation prevent invalid Start; Pydantic independently validates API inputs.
Invalid settings return 422 without starting or changing a ready experiment.
Defaults apply when the admin leaves controls unchanged or omits API fields.

Settings are per-run drafts, not global recognition settings and not persisted
as app-wide preferences. Reload/navigation restores defaults. Start copies and
freezes them. Always On shows a read-only run summary; stop, wait for any
in-flight request to finish, edit and Start again to apply different values.
Video metadata freezes at Start; existing runs cannot be restarted or configured
again. Upload a fresh experiment to compare another configuration.

Authenticated GET /api/scan-settings returns defaults/ranges and can_configure
from the server-side user role. POST /api/scan-settings/validate is admin-only
and returns the validated snapshot without DB writes. Local video Start is
admin-only and independently validates the same schema. Non-admin Always On
stations retain automatic startup with server defaults and no editable controls.
Admin Always On waits for explicit Start, including pinned activity/camera URLs.

## Combined rule

Let S be previous scan start, F its completed scan time, D the selected wait,
and R target detections/s. Earliest next start:

next_start >= max(F + D, S + 1/R)

Each run awaits one complete scan. No interval queue, overlapping scans or
catch-up burst. A late timer takes a fresh latest frame and starts a new interval
from its actual start. Default D=.4, R=2.5 gives max(F+.4,S+.4)=F+.4 for F>=S;
target therefore adds no second delay. Slow inference lowers achieved rate.

Always On uses the existing video stream and current-frame canvas JPEG/API
path. It freezes camera selection behavior and requested FPS on start, reads
MediaStreamTrack.getSettings().frameRate where available, and keeps the stream
open during scans. JPEG encoding, network, server work and response handling
are included in the awaited browser scan. Stop/navigation invalidates the run;
any old inference drains before another run starts and stale results are not
added to the new run. Tap retains completion + 200ms, its existing consent,
check-in/result mapping and unmodified camera constraints.

Negotiated FPS is track-reported capture configuration, not a measured scan
rate. Some browsers/cameras do not report it; UI says unavailable. If live
capture updates slower than scans, the latest displayed live image may repeat,
as in historical capture. Saved-video inference never repeats a source frame.

## Virtual camera and sequential saved-video replay

The uploaded media bytes, codec and source FPS S_fps are untouched. No re-encode,
seek, frame array or inference queue. Only the current decoded image and decoder
buffers are held; all intervening frames decode sequentially without inference.

Start at source frame 0 and virtual capture time T=0. For measured inference/
embedding/matching work W (including shared inference-lock wait):

deadline = max(T + W + D, T + 1/R)
virtual_tick = floor(deadline * C), where C is selected Camera FPS
source_index = floor(virtual_tick * S_fps / C)

These zero-based virtual ticks are anchored at video time zero. At each tick,
the virtual camera sees the latest available source frame. If source_index was
already inferred, advance to the first virtual tick exposing a distinct source
frame:
virtual_tick = max(virtual_tick+1, ceil((previous_index+1)*C/S_fps))
source_index = floor(virtual_tick*S_fps/C)
next_T = max(deadline, virtual_tick/C)

A 1e-9 epsilon handles integer rounding boundaries. This means requested C
above source FPS never invents duplicate frames. C below source FPS discards
intervening source frames. The next scan sees the latest virtual-camera frame,
not an earlier queued frame. No real-time sleeps are used in offline replay.

Examples with zero work and 0 wait, target 30:
- Source 30 FPS, C=2: infer indices 0,15,30,45 at virtual times 0,.5,1,1.5.
- Source 1 FPS, C=120: infer each source frame once at 0,1,2,3; achieved 1/s.
- Default C=30,D=.4,R=2.5, source 5 FPS: 0,2,4,6,... at 0,.4,.8,1.2,...
- Work .2s, defaults, source 5 FPS: 0,3,6,9,... at 0,.6,1.2,1.8,...
- Wait .1, target 1/s, work .2: next start is 1s, not .3s or 1.3s.

Camera/network/JPEG/Central-event latency, multi-camera historical backlog/503,
capture jitter and missing original frames cannot be recovered exactly. Offline
decode/checkpoint and identity preparation affect wall throughput but not W.
Nominal FPS cannot reconstruct VFR presentation timestamps or missing live
camera frames; export constant FPS for exact nominal timeline work. Truncated
decode preserves partial results and fails without inventing frames. Work and
lock contention can differ across machines/runs, so exact selected frames may
differ; stored indices/work/virtual times make each actual run auditable.

## Requested versus achieved rates

Always On achieved completed scan rate = (successful completed scans - 1) /
(last successful scan start - first successful scan start), in wall seconds.
Blank until two completed scans. Empty/unknown-only successful responses count;
failed requests and captures without a successful response do not. This counts
scans, not people or check-ins. No second recognition call for telemetry.

Video actual_video_detection_hz = (completed sampled frames - 1) /
(last virtual capture time - first virtual capture time).
Blank before two samples. This is represented video-time cadence, not
historical measured camera FPS.
processing_throughput_fps = completed sampled frames / processing_seconds:
actual accelerated offline wall throughput, distinct from camera/target FPS.
UI also shows source FPS, selected virtual FPS, selected wait/target, elapsed
time, sampled count and every sampled timestamp/work/capture time.

max_scan_hz = min(target, 1/wait), or target if wait=0, before scan work and
virtual/source-FPS limits. max_sample_count_zero_work is an upper bound computed
with the selected controls, not a promised actual count.

## Immutable video metadata and CSV

state.json stores a separate scan_settings snapshot:
camera_fps, post_scan_delay_seconds, target_detections_per_second.
No endpoint edits it after Start. The frozen controls are used throughout run.

UTF-8 BOM CSV preserves the original first eight rectangular columns:
record_type,key,value,identity_key,name,detection_count,first_timestamp_seconds,last_timestamp_seconds

New metadata camera_fps, target_detections_per_second and
effective_available_fps_limit accompany existing post_scan_delay_seconds,
video_fps, sampled_frame_count, actual_video_detection_hz,
processing_throughput_fps, sampling_method and historical_reference.
effective_available_fps_limit = min(source FPS, selected camera FPS), an upper
bound rather than a measured capture rate. Every person row still counts
sampled frames containing that identity, at most once per frame. Unknown
detections remain individual unmatched face results, not unique people.
All other count definitions/CSV metadata are in local-video-experiment.md.

Old nominal-fps-v1 (1/s) and historical-camera-v2 results are never rewritten,
reprocessed or assigned inferred new settings. Their stored method/counts/times
and previous report meanings remain. Unavailable new settings are blank in CSV.
Old ready uploads adopt selected v3 controls only on explicit Start. Restart
continues to mark active jobs interrupted and never resumes processing.

## Manual test

1. Sign in as admin. Open
   http://localhost:5173/recognition?mode=always.
   Verify camera stays off until Start; controls show 30/.4/2.5.
2. Set custom values (for example 12/.8/1), Start. Confirm requested FPS,
   negotiated actual FPS if reported, all frozen values and achieved scan rate.
   Recognition/check-in banners must retain existing behavior.
3. Stop to change settings. During pending inference, Start/inputs stay disabled.
   After draining, edit values and restart. Verify new camera request/run values.
   Also verify normal Tap consent/result behavior.
4. Open http://localhost:5173/local-video-experiment. Upload local video;
   ready has zero samples. Choose controls and Start explicitly.
5. Inspect source FPS versus virtual limit, selected wait/target, actual cadence,
   wall throughput, sampled timestamps and per-person counts. Download CSV.
6. Compare different settings using separate new uploads. Old reports retain
   their old rules. Test Cancel/partial CSV/Delete on a longer experiment.
7. Invalid/out-of-range/empty inputs must prevent Start. Non-admin authenticated
   users must not be able to submit changed settings.


Match review appends manual_verdict, review_meaning, preview_frame_number, preview_timestamp_seconds and preview_match_score; verdicts cover one representative example only. See local-video-experiment.md for CSV and EOF semantics.
