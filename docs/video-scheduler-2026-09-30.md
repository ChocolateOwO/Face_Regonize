# Video device selection and bounded batch queue

Local/Drive video experiment only. Event Photo, check-in, model
weights/thresholds, Drive OAuth and the Dummy are unchanged by this work.

## Known limits

- **Interrupted Drive downloads start over.** Downloads are not resumed or
  retried automatically; a network drop fails that video (reported as a
  network loss, with the partial file removed). Submit it again in a new batch.
- **More than 2 workers can reduce sampled frames.** CUDA inference runs one
  call at a time for all video workers. Under contention each scan waits for
  the GPU, and that wait counts toward the scan cadence, so videos finish
  sooner but receive fewer recognition samples (measured: 620 samples at
  2 workers, 545 at 4, 352 at 12 for the same clips). Default is 2.
- **CUDA inference runs one at a time** (shared engine lock); extra workers
  overlap download, decode, checkpoints and EOF checks only. TensorRT is not
  selectable on machines without its runtime DLLs.
- A worker keeps its slot while downloading, so slow network downloads can
  occupy every slot while the GPU is idle.
- No fixed per-file size limit: each transfer needs free disk space for its
  remaining bytes, plus other active transfers' remaining bytes, plus a 1 GiB
  reserve. Per-file duration limits remain (Drive 12 h, local upload 30 min).

## Before and after

Before: `local_video_experiment.start` reserved one process-wide `_active`
slot and `_cancel` event. `drive_video_batch.run` handled a whole batch before
another could start. There was no durable queue. Main's two saved video runs
were completed at inspection; no user video was decoded or rerun for this work.

After: `video_scheduler` persists admission, frozen settings, enrollment and
per-source checkpoints. A round-robin source scheduler chooses the batch least
recently dispatched, then submission time/ID. Active work is never preempted.
All batches share a default of **2 video workers**, admin-configurable **1–4**.
At most **32 unfinished batches** may be admitted. Lowering the limit drains
existing workers before admitting more. One worker owns one source until its
download, sequential decode, EOF verification and cleanup finish.

Inference remains **one call at a time**, sharing the existing engine arbiter
without changing it. Download, decode, matching, preview/checkpoint writing and
EOF verification can overlap across workers. No inference backlog is created.
This is a single-backend-process scheduler; run the existing single Uvicorn
worker, not several API worker processes against the same experiment storage.

Admission requires 1 GiB available RAM and disk space for the prospective
download, outstanding reservations and the existing 1 GiB free-space reserve.
Reservations conservatively include each admitted source's entire stated size.
Drive's streaming size/checksum/version/free-space checks remain in place.
No new dependency or environment/configuration file change was required.

## Devices and recognition

Admin selects Auto, CPU, or a GPU provider that successfully creates AND runs
both actual buffalo_l detector and recognition models. Runtime provider order
is checked; silent whole-session CPU fallback is rejected and runtime fallback
is disabled on these **video-only** sessions. Event/kiosk sessions are untouched.
Auto tries verified CUDA then CPU. Explicit GPU selection never substitutes CPU.
The resolved device is frozen for all sources; later runtime failure stops the
affected source rather than changing providers in mid-batch.

On this RTX 4050 Laptop GPU (6,141 MiB VRAM; 12 logical CPUs; 31.71 GiB RAM),
CUDAExecutionProvider and CPUExecutionProvider passed actual detector and
embedding warmup. TensorRT's required runtime DLLs are absent, so TensorRT is
not a selectable GPU despite ORT advertising it. Models remain buffalo_l,
det_10g.onnx and w600k_r50.onnx; detection 0.5/320x320, existing match threshold
0.45. No model, enrollment, Event scorer or check-in threshold changes.

The UI shows frozen requested/actual providers, queue turns, active workers,
per-source progress, stage times, achieved sampled frames/s and machine-wide
GPU utilization/VRAM. GPU telemetry includes other apps and workflows; it is
not represented as exclusive batch consumption.

Camera FPS 30, post-scan wait 0.4 seconds and target 2.5/s retain their original
defaults and completion-paced virtual timeline. Measured inference lock wait
still contributes to scan work. Consequently samples selected may differ with
contention; aggregation and representative choice are deterministic for those
samples, not a claim that timing-dependent sampling is identical across runs.

## Persistence, counts and previews

Each source publishes replacement totals under the experiment lock. Combined
counts sum each source once. An identity counts at most once per sampled frame
in each video; detector matched/unknown face counts count individual detections.
There is no cross-camera passage deduplication or recognition-accuracy metric.

The strongest valid cosine score wins the representative. Equal scores use
source filename/Drive-ID order, then frame index, then existing detection order.
Missing scores use earliest valid match; scored matches supersede unscored.
Completion order cannot replace a stronger/earlier representative. The original
scaled full-frame bbox, protected preview endpoint and example-specific manual
verdict remain. One retained JPEG per matched identity per experiment.

On refresh: existing records are read, never resubmitted. On backend restart:
untouched pending sources continue with the stored enrollment/settings;
in-flight sources become interrupted with partial results and **are never
automatically downloaded or inferred again**. Completed sources are untouched.
An interrupted source requires an explicit new experiment if the admin wants
to process it again. Older pre-scheduler runs keep their historical behavior
and meanings. Cancelling one batch does not signal another batch.

Private storage remains `backend/local_video_experiment_storage` (Git-ignored):

- Retained: per-experiment `state.json`, private frozen `enrollment.json`,
  optional `reviews.json`, `previews/<identity-key>.jpg`, and locally uploaded
  `video.<extension>` until Delete. CSV is generated from saved records.
- Temporary: `downloads/video-NNN/source.<extension>` for each admitted Drive
  source; removed after success, failure or cancellation. Restart removes any
  leftovers before resuming pending work. Preview folders are separate.
- Global: `scheduler.json` stores the admin worker limit.
- Delete removes only the stopped experiment directory, including its frozen
  enrollment and previews. Original Drive files are never modified or deleted.

CSV retains the original fields/record meanings. Added metadata includes
requested_provider, actual_provider, worker_limit_at_submission, inference_limit,
stage_seconds, stage_time_meaning and restart_policy. Batch video rows append
requested_provider, actual_provider and stage_seconds. Stage values sum worker
time and may overlap; they must not be added and called batch wall time. Counts,
source FPS, samples, throughput, preview source filename and verdict remain.

## Synthetic measurements

Generated 1280x720 MJPEG clips, 30 FPS, 450 frames each; two batches of seven
sources, 6,300 decoded frames. Mock downloads copied a synthetic file with
100 ms/source simulated latency. Actual CUDA detector plus one synthetic
112x112 recognition embedding per scan; no real faces, identity data or Drive
request. Independent FFmpeg EOF checks and real filesystem checkpoints enabled.

| Runner | Workers / simultaneous inference | Seconds | Samples | Samples/s |
|---|---:|---:|---:|---:|
| Byte-backed-up original sequential runner | 1 / 1 | 70.60 | 477 | 6.76 |
| New scheduler, serial baseline | 1 / 1 | 63.65 | 504 | 7.92 |
| New scheduler, default | 2 / 1 | 36.56 | 502 | 13.73 |

One-to-two scheduler workers: 73% higher sampled-frame throughput and 43%
shorter elapsed time. Original-to-new elapsed time fell 48%. These are bounded
synthetic measurements, not guaranteed real Drive or crowded-video speed.
Sample totals differ because measured processing time controls sampling.

New scheduler one/two workers: mean GPU utilization 39.2%/44.5%, peak VRAM
1,301/1,321 MiB, peak process RSS 1,343/1,356 MiB, average process CPU
92.5%/135.2% (100% is one logical core). CPU monitoring includes initialization
noise. GPU values are machine-wide.

Two-worker summed stage time: decode 30.71 s, checkpoints 12.44 s, EOF verification
10.75 s, detector 6.19 s, synthetic embedding 5.54 s, inference wait 1.06 s,
download 1.80 s, probe 0.87 s. Work overlaps; sum is not elapsed time.
Separate short model-only benchmark: independent one/two CUDA sessions delivered
51.10/82.49 detector+embedding calls/s, peak VRAM 1,629/1,649 MiB. Two inference
sessions were feasible in that microbenchmark; serial inference is a deliberate
shared-workflow/resource choice, not a claim that two GPU sessions are impossible.
The end-to-end workload benefits from stage overlap without adding GPU sessions.

Artifacts and byte-verified pre-change backups are in ignored
`patch/2026-09-30_video-scheduler/`. No videos, DBs, credentials or results are
added to source control.

## Follow-up: preview lock contention (Claude, 2026-09-30)

The measurements above used clips with no enrolled faces, so the preview path
never ran. With synthetic matched faces (InsightFace's bundled public sample
`t1.jpg`, three faces enrolled in an isolated DB) and storage on E:, the
original scheduler was slower with 2 workers than with 1: every matched face
published, re-read and re-wrote the whole batch state under the global lock,
and reading a just-replaced state.json on E: took median 27.5 ms (p95 165 ms,
max ~1 s) on this machine. Fixed by an in-memory best-preview check and a
stat-validated state text cache; see patch/2026-09-30_0338_video-preview-lock.

Isolated instances, real CUDA, fake Drive (0.5 s/download), seven 20 s
1280×720 clips, storage on E:, API polled every 1.5 s like the UI:

| Scenario | Before: wall / sampled per s | After: wall / sampled per s |
|---|---:|---:|
| 1 batch × 7, 1 worker | 405 s / 0.40 (first run, cold files) | 286 s / 0.85 (cold); 43 s / 7.16 (warm) |
| 1 batch × 7, 2 workers | 571 s / 0.13 | 24 s / 12.99; 25 s / 12.74 |
| 2 batches × 7, 2 workers | 1,226 s / 0.09 | 43 s / 14.44; 268–354 s / 1.0–1.8 under outside GPU load |

Worker time in preview handling (summed): before 145 / 249 / 505 s; after
≤ 16 s in every run. In the slow after-runs, inference itself (timed inside the
lock) took 175–219 s versus 29 s in the fast run while the GPU peaked at 98%,
i.e. another GPU user on the machine; those numbers are not lock cost.
Previews, strongest-score selection, bboxes and counts matched between runs'
selection rules; live UI re-verified on the fixed code with synthetic data.

## Worker ceiling, file size, Clear all (Claude, 2026-09-30 later)

**Why the limit stopped at 4:** `WorkerSettings.max_workers` had `le=4`, mirrored
by `max={4}` in the page. No resource check backed it. Workers are CPU/IO slots
(download, decode, checkpoint, EOF check); GPU inference is separately limited
to one call at a time by the shared engine lock. The ceiling is now one worker
per logical CPU (12 on this machine); RAM (1 GiB free) and disk are checked
again at every admission, across all batches.

Sweep: isolated instance, storage on E:, two batches × 7 synthetic 20 s
1280×720 clips (three enrolled synthetic faces), fake Drive 0.5 s/download,
real CUDA, laptop on AC:

| Workers | Wall | Videos/min | Sampled frames | Sampled/s | GPU mean | RSS |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 85 s | 9.9 | 622 | 7.3 | 26% | 1.74 GB |
| 2 | 45 s | 18.8 | 620 | 13.9 | 39% | 1.76 GB |
| 3 | 39 s | 21.7 | 584 | 15.1 | 42% | 1.85 GB |
| 4 | 35–37 s | 22.4–24.1 | 545 | 14.5–15.7 | 44% | 1.92–2.02 GB |
| 6 | 33 s | 25.6 | 471 | 14.4 | 44% | 1.95 GB |
| 8 | 28 s | 30.5 | 423 | 15.4 | 51% | 2.43 GB |
| 12 | 26 s | 32.1 | 352 | 13.4 | 47% | 2.58 GB |

VRAM ~2.0 GB of 6 GB; ≥ 7 GiB RAM available throughout. Sampled frames/s
saturates at ~14–15/s from 2–4 workers (one GPU inference at a time). Beyond
that, videos finish sooner only because each gets fewer recognition samples
(scan cadence includes GPU wait). **Default stays 2**: fastest measured with no
coverage loss. On battery the same sweep was 2–4× slower and noisier.
E: is a SATA SSD: 356 MiB/s sustained write; real footage is ~3.8 MB/s per
camera, so disk is not the limit. For real Drive input the network is: the
user's four parallel downloads totalled 4.4 MB/s.

**Size:** no fixed per-file byte ceiling anywhere (Drive 16 GiB and local
512 MiB removed). A shared disk budget reserves each transfer's remaining bytes;
see patch/2026-09-30_0752_video-workers-large-files/NOTES.md. Drive network
failures are no longer reported as disk errors.

**Clear all** sits beside Select all in the Drive selection; draft-only.

## Verification and manual test

Automated (on the committed tree): full backend suite, focused video suites
(scheduler, Drive batch, local video), frontend node tests, typecheck and
production build (>500 kB bundle warning only). Tests use synthetic media,
fake Drive and temporary storage only.

Live checks used an isolated instance of this code (temporary DB/storage, fake
Drive, synthetic clips and faces from InsightFace's bundled sample image): two
concurrent batches incl. seven videos, worker cap, explicit TensorRT refused,
Auto→CUDA, cancel isolation, broken-clip failure, previews/bbox/CSV, temp
cleanup, worker ceiling 1–12, Select all → Clear all → individual picks → Start,
Clear all while a batch runs (no request sent), live per-stage worker display.

Files:

- `backend/app/services/video_devices.py`, `video_scheduler.py` (new).
- `backend/app/services/local_video_experiment.py`, `drive_video_batch.py`,
  `drive_video_source.py`.
- `backend/app/api/local_video_experiment.py`, `local_video_drive.py`.
- `backend/app/main.py` (video scheduler startup/shutdown only).
- `backend/tests/test_video_scheduler.py`, `video_scheduler_fixture.py` (new).
- `backend/tests/test_local_video_experiment.py`, `test_drive_video_batch.py`.
- `frontend/src/pages/LocalVideoExperiment.tsx`.
- `frontend/src/components/DriveVideoBatchPanel.tsx`.
- `frontend/tests/video-batch-progress.test.mjs`, `drive-video-batch.test.mjs`.
- This document.

Backend regressions cover seven-video batches, two batches, worker bounds,
round-robin fairness, explicit unavailable GPU, Auto fallback, both-model probes,
frozen enrollment reload, cancellation isolation, source failure, restart without
duplicate counts, temporary cleanup, deterministic previews/bboxes, verdict/CSV,
admin-only APIs and older results. Frontend tests cover Start/progress/recovery,
multiple batches, device/worker controls, errors, previews and CSV rendering.

Main URL: http://localhost:5173/local-video-experiment (backend 8000).

1. Sign in as admin and refresh. Confirm Auto, CUDA and CPU choices; TensorRT
   should appear only as unavailable explanatory text on this machine.
2. Keep workers=2. Paste a Drive folder link, List videos, select seven and name
   batch A. Choose settings/device and Start explicitly.
3. While A runs, submit a separately named batch B. Both should remain in the
   experiment list with independent status, queue turn and result links.
4. Open each result; verify per-video progress and actual provider. Refresh or
   reopen its URL; no new experiment should be created.
5. Cancel B if desired. A must continue. Review a matched person's boxed example
   after its batch stops; verify filename/frame/time, zoom and manual verdict.
6. Download CSV. Compare combined and video_person rows without summing both
   categories together. Reviewing one example does not verify every detection.
7. Delete a stopped synthetic experiment if desired. Other experiments and all
   original Drive files must remain. Do not restart or retry real videos merely
   to exercise recovery; automated tests cover interruption with synthetic media.
