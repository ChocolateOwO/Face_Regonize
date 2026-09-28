# Historical continuous-camera rate and saved-video replay

The old rule/evidence below remains valid. The v2 implementation discussion is
an archived description of the previous Dummy runner. Current configurable v3
adds virtual capture FPS and target-rate ceilings; defaults retain completion
+400ms. See [current controls and exact replay rule](scanning-settings.md).

Reference commit: e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559 (read-only).
The saved 2026-09-08_0851_phase-a-local-first-main patch contains Phase A Event
Photo changes, not the continuous-camera source. Its NOTES.md lists the patch
contents. The supplied commit contains the integrated CCTV recognition and
recording implementation, so no speculative older rate was substituted.
The current check-in camera is not the comparison target.

## Code evidence

All links below pin the historical commit, not the current branch.

- [CameraNode.tsx:54](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/frontend/src/pages/CameraNode.tsx#L54):
  SCAN_DELAY_MS = 400.
- [CAMERA_CONSTRAINTS:112-116](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/frontend/src/pages/CameraNode.tsx#L112):
  requested 1280x720 and frameRate ideal/max 30. This is a capture constraint,
  not a measured camera FPS or detection FPS.
- [startCameraForSelection:769-845](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/frontend/src/pages/CameraNode.tsx#L769):
  getUserMedia with constraints (804), assigns stream to video element (810-811),
  marks active (841), launches runScanLoop (845).
- [runScanLoop:900-906 and scanOnce:908-974](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/frontend/src/pages/CameraNode.tsx#L900):
  await scanOnce, then await setTimeout(400ms). scanOnce returns without capture
  unless detection is active, agent ready and video available (913-916).
  drawImage(video, 0, 0) captures the latest displayed frame (920);
  awaits JPEG encoding (921), local /recognize (929), response JSON (932).
  A 503 drops this attempt (930). Recognized matches reuse the same result for
  the face count and live/session feed (948,954), then await Central event
  submission (970); that latency also delays the next scan.
- [Agent queue:82-105](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/backend/node_agent.py#L82):
  semaphore(1) serializes inference; _MAX_WAITING = 4 bounds admitted requests.
- [create_app.recognize:261-295 and _decode_and_infer:298-315](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/backend/node_agent.py#L261):
  count includes the running request and pending requests (265-269,280).
  await asyncio.to_thread(_decode_and_infer, file_bytes) (277).
  Image decode occurs before semaphore; detect_faces and match_batch occur
  inside semaphore (306-314). At most four admitted requests total across
  cameras, not four waiting plus one running. New arrivals at capacity get 503.
- [engine.detect_faces:103-137](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/backend/app/face_recognition/engine.py#L103):
  whole supplied image passed to det_model.detect (112); embeddings obtained
  per detected face (123-124). No further temporal frame selection.
- [startRecordingFor:1112-1165](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/frontend/src/pages/CameraNode.tsx#L1112):
  cloned camera stream feeds MediaRecorder. recorder.start(1000) controls
  one-second recording chunks, not one-second detection.
- [Continuous single-camera branch:1732-1771](https://github.com/ChocolateOwO/Face_Regonize/blob/e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559/frontend/src/pages/CameraNode.tsx#L1732):
  captureBlob draws current video; startScanLoop also awaits scanOnce then
  SCAN_DELAY_MS; scanOnce awaits /api/node/recognize-central. Same completion
  pacing even without the local agent.

## Established old rule

Capture continues independently. While detection is active and ready, each
camera has at most one outstanding scan. Scan start-to-start time is all awaited
scan work PLUS at least 400ms browser timer delay. Work includes encoding,
request/response, decode, queue wait, inference, matching, response processing
and, for recognized results, Central submission. Intervening camera frames are
skipped, not buffered for later per-camera replay. Across cameras the agent has
a bounded queue and serialized inference as above; rejection loses that capture
and the next loop captures a fresh frame.

Therefore detection was neither every camera frame, every Nth frame, nor a
fixed wall-clock interval independent of inference. 2.5 scans/s is the ideal
zero-work ceiling, not established actual historical throughput. Static code
provides no historical timing measurements, dropped-frame log or achieved FPS.

## Before and after in Dummy

Before: nominal-fps-v1 selected ceil(k * FPS), k=0,1,2,...; fixed 1 sample/s.

After: historical-camera-v2 starts at saved frame 0 with virtual capture time T=0.
After each completed inference/matching call, measure work W using perf_counter.
Advance next capture deadline to T + W + 0.400 seconds. Select the latest
available saved frame floor(deadline * nominal FPS), clamped to at least the
previous frame index + 1. At low FPS advance capture time to the chosen distinct
frame's nominal timestamp if necessary. The 1e-9 rounding epsilon prevents
floating-point boundary artifacts. Keep the virtual clock between captures;
do not reset it to the rounded frame timestamp.

Example, 16 frames at 5 FPS:
- Zero work: indices 0,2,4,6,8,10,12,14; captures 0,.4,.8,...,2.8s.
- Work .2s each: indices 0,3,6,9,12,15; captures 0,.6,1.2,...,3s.
- At 29.97 FPS and zero work: 0,11,23,35,47,59; selects the latest
  available frame rather than a future frame.
- At 1 FPS: each distinct frame once, effective cadence 1/s.

All decoded frames remain sequential, one current image plus decoder buffers.
Frames before the next selected index are discarded without inference.
No saved-frame inference queue and no 400ms real sleep: replay can run faster
than real time while representing the completion-paced camera clock.
Only Local Video Experiment service/API/page/tests/docs changed in this task.
Detection model, matcher, settings, identity enrollment, normal Event Batch,
current camera, legacy Drive, Main and historical sources remain unchanged.

## Limits of alignment and reproducibility

This is a single-camera approximation. Measured W includes existing inference
lock wait, whole-frame detector/embedding and identity matching. It excludes
browser JPEG encoding, local HTTP/image decoding, UI/JSON processing, Central
event submission, original multi-camera queue/503 behavior and browser timer
jitter. Offline video decode, identity/model preparation and checkpoint writing
affect measured wall throughput but are not added to virtual camera time.
No recorded original scan timings exist to replay those missing latencies.

Nominal FPS/index timestamps cannot recover actual live dropped frames,
variable-frame-rate PTS, capture jitter or a missing original camera frame.
The decoder processes only frames available in the saved video. Premature
decode termination fails with partial results preserved; no missing frame is
invented. At low source FPS, original live capture could reuse the same displayed
frame. Dummy deliberately waits for the next distinct saved frame to preserve
sampled-frame count meaning; it never infers a saved frame twice.
First frame anchors at video time zero; original detection start offset is
unknown. Saved recordings may continue while historical detection was off;
Dummy processes the uploaded video as one explicitly started detection session.

Measured inference time varies with hardware, provider, faces and lock contention.
Thus repeated runs can select different frames. Reproducible tests inject a
deterministic scan clock; real results store each selected index, nominal frame
timestamp, virtual capture time and measured work. Preserve state.json with CSV
when exact frame replay matters. Video hash, source FPS, model/provider/threshold,
rule and reference are recorded; no claim of exact historical timings.

## UI / CSV rate meanings

- sampling_hz = variable for new runs (earlier completed runs retain 1).
- post_scan_delay_seconds = 0.4; max_scan_hz = 2.5, before work/FPS limits.
- actual_video_detection_hz = (completed samples - 1) /
  (last virtual capture time - first virtual capture time).
  Blank until at least two samples. This is represented video-time scan cadence,
  not achieved historical camera FPS.
- processing_throughput_fps = completed samples / processing_seconds.
  Blank with no positive elapsed time. This is actual offline wall throughput;
  it is not camera FPS and need not be below 2.5.
- max_sample_count_zero_work is an upper bound, not expected actual samples.
- scan_work_seconds sums measured scan work; decoded_frames_not_inferred equals
  decoded frames minus completed sampled frames (including decode-only skips
  and a possible cancelled in-flight decode).

UI shows both rates and per-sample timing. CSV metadata includes both rates,
sampling_method, historical_reference and timing_differences. Person rows,
first/last nominal sampled timestamps and count meanings are unchanged.
Completed/partial older 1/s experiments are never resampled or rewritten and
reports retain their stored method/rate. An older ready upload adopts the new
rule only when explicitly started. Backend restart never resumes processing.
