import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { apiGet, apiPostJson, apiDelete, downloadFile, ApiError } from "../api/client";
import { Button, Card, PageHeader } from "../components/ui";

import VideoMatchPreview, { type MatchedPerson } from "../components/VideoMatchPreview";
import ScanSettingsFields from "../components/ScanSettingsFields";
import DriveVideoBatchPanel from "../components/DriveVideoBatchPanel";
import VideoBatchSources, { type VideoSourceResult } from "../components/VideoBatchSources";
import { DEFAULT_SCAN_SETTINGS, scanSettingsError, type ScanSettings } from "../api/scanSettings";

const API = "/api/local-video-experiment";
const ACTIVE = ["queued", "running", "cancelling"];
// Used only until the backend reports its own ceiling (older backends did not).
const LEGACY_WORKER_CEILING = 4;
type Person = MatchedPerson;
interface Execution {
  options: { value: string; label: string }[]; max_workers: number; active_workers: number; inference_limit: number;
  max_workers_limit?: number; default_workers?: number; downloading_workers?: number; processing_workers?: number;
  inference_in_flight?: number; queued_sources?: number; queued_batches?: number; reserved_download_bytes?: number;
  unavailable?: Record<string, string>; gpu?: { utilization_percent: number; used_vram_mib: number; total_vram_mib: number } | null;
}
interface Sample {
  frame_index: number; timestamp_seconds: number; faces: number;
  matched_people: number; unknown_detections: number;
  capture_time_seconds?: number; scan_work_seconds?: number;
}
interface Experiment {
  requested_provider?: string; actual_provider?: string; queue_position?: number | null; active_workers?: number;
  waiting_reason?: string; stage_seconds?: Record<string, number>;
  kind?: "drive_batch"; batch_name?: string; videos?: VideoSourceResult[];
  total_videos?: number; finished_videos?: number; completed_videos?: number; failed_videos?: number;
  temporary_downloads_cleaned?: boolean;
  id: string; filename: string; status: string; created_at: string;
  started_at: string | null; finished_at: string | null; error: string | null;
  media: { duration_seconds: number; fps: number; frame_count: number; expected_samples: number | null; max_samples?: number; sha256: string } | null;
  model: { name: string; provider: string; threshold: number; detection_threshold: number; detection_size: number[]; enrolled_identities: number } | null;
  sampled_frames: number; decoded_frames: number; unknown_detections: number;
  frames_with_unknown: number; matched_face_detections: number; unique_matched_people: number;
  processing_seconds: number; progress_percent: number; sampling_hz: number | null;
  sampling_method: string; post_scan_delay_seconds?: number; historical_reference?: string;
  timing_differences?: string; actual_video_detection_hz: number | null;
  processing_throughput_fps: number | null; max_scan_hz: number;
  decoded_frames_not_inferred: number;
  detected_face_detections: number; verified_total_frames: number | null;
  progress_is_estimate: boolean; decode_phase?: string;
  decode_audit?: { outcome: string; verified_frame_count: number | null };
  decoded_timestamp_first_seconds?: number; decoded_timestamp_last_seconds?: number;
  decoded_timestamp_max_gap_seconds?: number; actual_max_samples_zero_work?: number;
  scan_settings?: ScanSettings | null;
  partial: boolean; can_start: boolean; can_cancel: boolean; can_delete: boolean;
  people?: Person[]; samples?: Sample[];
}

function experimentResponse(value: unknown): Experiment {
  const state = value as Experiment | null;
  if (!state || typeof state.id !== "string" || !state.id || typeof state.filename !== "string" ||
      typeof state.status !== "string" || !Number.isFinite(state.progress_percent) || !Number.isFinite(state.processing_seconds) ||
      (state.kind === "drive_batch" && !Array.isArray(state.videos))) {
    throw new Error("The server returned an incomplete experiment response. Refresh the experiment list to check whether Start created a batch before trying again.");
  }
  return state;
}

function uploadVideo(file: File, progress: (value: number) => void): Promise<Experiment> {
  // Blob goes directly to request.stream(); no base64, arrayBuffer or multipart spool.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API + "?filename=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    const token = localStorage.getItem("token");
    if (token) xhr.setRequestHeader("Authorization", "Bearer " + token);
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) progress(Math.round(e.loaded / e.total * 100));
    };
    xhr.onload = () => {
      if (xhr.status === 401) window.dispatchEvent(new Event("auth:unauthorized"));
      let data;
      try { data = JSON.parse(xhr.responseText); } catch { /* server/network error */ }
      if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data);
      else reject(new ApiError(xhr.status, data?.detail || "Video upload failed."));
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error during upload."));
    xhr.send(file);
  });
}
const message = (err: unknown) => err instanceof Error ? err.message : "Experiment request failed.";
const seconds = (value: number | null | undefined) => value == null ? "\u2014" : value.toFixed(3) + " s";

export default function LocalVideoExperiment() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedId = searchParams.get("experiment");
  const [sourceMode, setSourceMode] = useState<"local" | "drive">("local");
  const [scanSettings, setScanSettings] = useState<ScanSettings>({ ...DEFAULT_SCAN_SETTINGS });
  const [isAdmin, setIsAdmin] = useState(false);
  const [device, setDevice] = useState("auto");
  const [workerLimit, setWorkerLimit] = useState(2);
  const [execution, setExecution] = useState<Execution | null>(null);
  const workerCeiling = execution?.max_workers_limit ?? LEGACY_WORKER_CEILING;
  const [list, setList] = useState<Experiment[]>([]);
  const [selected, setSelected] = useState<Experiment | null>(null);
  const [reviewPerson, setReviewPerson] = useState<Person | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadPercent, setUploadPercent] = useState(0);
  const [error, setError] = useState("");
  const [clock, setClock] = useState(Date.now());
  const input = useRef<HTMLInputElement>(null);
  const selectedId = useRef<string | null>(null);
  const recovered = useRef(false);
  const resultTitle = useRef<HTMLHeadingElement>(null);
  const selectionRequest = useRef(0);
  const requestedIdRef = useRef(requestedId);
  requestedIdRef.current = requestedId;
  selectedId.current = selected?.id ?? null;

  const loadList = useCallback(async () => {
    const data = await apiGet(API);
    if (!Array.isArray(data?.experiments)) throw new Error("Unable to read the experiment list. Refresh this page; no new batch was started.");
    setList(data.experiments as Experiment[]); setIsAdmin(true);
    if (data.scheduler) setExecution(old => old ? { ...old, ...data.scheduler } : old);
  }, []);
  useEffect(() => { void loadList().catch(err => setError(message(err))); }, [loadList]);
  useEffect(() => {
    if (!isAdmin) return;
    void apiGet(API + "/execution").then(value => { setExecution(value); setWorkerLimit(value.max_workers); })
      .catch(err => setError(message(err)));
  }, [isAdmin]);
  const anyActive = list.some(exp => ACTIVE.includes(exp.status));
  useEffect(() => {
    if (!anyActive) return;
    let disposed = false;
    let timer: number;
    async function refreshQueue() {
      try { await loadList(); } catch (err) { if (!disposed) setError(message(err)); }
      if (!disposed) timer = window.setTimeout(refreshQueue, 1500);
    }
    timer = window.setTimeout(refreshQueue, 1500);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [anyActive, loadList]);
  useEffect(() => {
    // Reopen an explicit result, otherwise recover the active/latest Drive
    // batch. This reads existing status only: never call Start on recovery.
    if (selectedId.current || recovered.current) return;
    const candidate = requestedId ?? list.find(exp => ACTIVE.includes(exp.status))?.id ??
      list.find(exp => exp.kind === "drive_batch")?.id;
    if (!candidate) return;
    const controller = new AbortController();
    const request = ++selectionRequest.current;
    void apiGet(API + "/" + encodeURIComponent(candidate), controller.signal).then(value => {
      if (controller.signal.aborted || selectionRequest.current !== request || selectedId.current) return;
      const fresh = experimentResponse(value);
      recovered.current = true;
      setSelected(fresh); setClock(Date.now());
      if (fresh.kind === "drive_batch") setSourceMode("drive");
      if (requestedIdRef.current !== fresh.id) setSearchParams({ experiment: fresh.id }, { replace: true });
    }).catch(err => { if (!controller.signal.aborted && selectionRequest.current === request) setError(message(err)); });
    return () => controller.abort();
  }, [list, requestedId, setSearchParams]);
  useEffect(() => { resultTitle.current?.scrollIntoView({ block: "start" }); }, [selected?.id]);
  const active = selected != null && ACTIVE.includes(selected.status);
  useEffect(() => {
    if (!active || !selected) return;
    const id = selected.id;
    let disposed = false;
    let timer: number;
    async function poll() {
      try {
        const fresh = experimentResponse(await apiGet(API + "/" + id));
        if (disposed || selectedId.current !== id) return;
        setSelected(fresh); setClock(Date.now()); setError("");
        if (!ACTIVE.includes(fresh.status)) { await loadList(); return; }
      } catch (err) { if (!disposed) setError(message(err)); }
      if (!disposed) timer = window.setTimeout(poll, 1000);
    }
    timer = window.setTimeout(poll, 500);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [active, selected?.id, loadList]);

  async function upload() {
    if (!file || busy) return;
    setBusy(true); setUploading(true); setError(""); setUploadPercent(0);
    try {
      const fresh = await uploadVideo(file, setUploadPercent);
      selectionRequest.current++; recovered.current = true; setSelected(experimentResponse(fresh)); setFile(null);
      setSearchParams({ experiment: fresh.id }, { replace: true });
      if (input.current) input.current.value = "";
      await loadList();
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); setUploading(false); }
  }
  async function select(id: string) {
    setError("");
    const request = ++selectionRequest.current;
    try {
      const fresh = experimentResponse(await apiGet(API + "/" + encodeURIComponent(id)));
      if (selectionRequest.current !== request) return;
      recovered.current = true;
      setSelected(fresh); setClock(Date.now());
      setSearchParams({ experiment: id }, { replace: true });
    } catch (err) { if (selectionRequest.current === request) setError(message(err)); }
  }
  async function action(kind: "start" | "cancel") {
    if (!selected || busy) return;
    if (kind === "start") {
      const problem = scanSettingsError(scanSettings);
      if (problem) { setError(problem); return; }
    }
    setBusy(true); setError("");
    try {
      setSelected(experimentResponse(await apiPostJson(API + "/" + selected.id + "/" + kind, kind === "start" ? { ...scanSettings, device } : {})));
      setClock(Date.now()); await loadList();
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!selected || !window.confirm("Delete this experiment's local results, previews and any remaining local video? Original Google Drive files and other experiments are preserved.")) return;
    setBusy(true); setError("");
    try { await apiDelete(API + "/" + selected.id); selectionRequest.current++; recovered.current = true; setSelected(null); setSearchParams({}, { replace: true }); await loadList(); }
    catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }
  async function download() {
    if (!selected) return;
    try { await downloadFile(API + "/" + selected.id + "/report.csv", "local-video-" + selected.id + ".csv"); }
    catch (err) { setError(message(err)); }
  }
  const elapsed = active && selected?.started_at
    ? Math.max(selected.processing_seconds, (clock - Date.parse(selected.started_at)) / 1000)
    : selected?.processing_seconds;
  const batch = selected?.kind === "drive_batch";
  return (
    <div>
      {reviewPerson && selected && <VideoMatchPreview jobId={selected.id} person={reviewPerson} active={active}
        onClose={() => setReviewPerson(null)} onReviewed={state => {
          const fresh = state as Experiment; setSelected(fresh);
          setReviewPerson(fresh.people?.find(p => p.identity_key === reviewPerson.identity_key) ?? null);
        }} />}
      <PageHeader title="Local Video Experiment" subtitle="Count sampled frames containing enrolled identities" />
      <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        EXPERIMENT ONLY. Choose a local upload or read selected Google Drive videos. Results stay on this server; no Google Drive upload or deletion.
        Counts measure sampled-frame detections, not camera passages or recognition accuracy.
      </div>
      {error && <p role="alert" className="mb-3 text-sm text-red-600">{error}</p>}
      {isAdmin && <Card className="mb-4">
        <h2 className="font-semibold">Video processing device and queue</h2>
        <label className="my-2 block">Device for the next experiment
          <select aria-label="Video processing device" value={device} disabled={busy} onChange={e => setDevice(e.target.value)} className="ml-2 rounded border p-2">
            {(execution?.options ?? [{ value: "auto", label: "Auto (verify on Start)" }]).map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </label>
        <p className="text-xs">Frozen on Start. Auto verifies CUDA, otherwise CPU. An explicit GPU choice fails if unavailable. Existing runs keep their device.</p>
        <label className="my-2 block">Global simultaneous video workers (1–{workerCeiling})
          <input aria-label="Simultaneous video workers" className="ml-2 w-20 rounded border p-2" type="number" min={1} max={workerCeiling} step={1} value={workerLimit} onChange={e => setWorkerLimit(Number(e.target.value))} />
        </label>
        <Button disabled={!Number.isInteger(workerLimit) || workerLimit < 1 || workerLimit > workerCeiling || busy} onClick={() => {
          void apiPostJson(API + "/execution", { max_workers: workerLimit }).then(value => setExecution(old => old ? { ...old, max_workers: value.max_workers } : old)).catch(err => setError(message(err)));
        }}>Apply worker limit</Button>
        <p className="mt-2 text-xs">Current limit: {execution?.max_workers ?? "loading"}
          {execution?.default_workers != null && <> (measured default {execution.default_workers}; up to {workerCeiling}, one per logical CPU)</>}.
          More workers are not automatically faster: they share one GPU inference at a time.</p>
        <p role="status" aria-label="Video work in progress" className="mt-1 text-xs">
          {execution?.downloading_workers != null
            ? <>Active video workers: {execution.active_workers} ({execution.downloading_workers} downloading, {execution.processing_workers} decoding/recognizing).
              {" "}GPU inference: {execution.inference_in_flight ?? 0} of {execution.inference_limit} at a time.
              {" "}Queued: {execution.queued_sources ?? 0} videos, {execution.queued_batches ?? 0} batches waiting for a worker.</>
            : <>Active video workers: {list.reduce((sum, exp) => sum + (exp.active_workers ?? 0), 0)}.</>}
          {execution?.reserved_download_bytes ? <> Disk reserved for transfers still in progress: {(execution.reserved_download_bytes / 1024 ** 3).toFixed(2)} GiB.</> : null}</p>
        <p className="mt-1 text-xs">Downloads, decoding and output overlap within this cap; model inference stays globally serialized. Lowering the limit lets existing workers finish. Queued batches survive refresh/restart; interrupted videos retain partial results without automatic replay.</p>
        {execution?.unavailable && Object.entries(execution.unavailable).map(([key, reason]) => <p className="text-xs" key={key}>{reason}</p>)}
        {execution?.gpu && <p className="mt-2 text-xs">Machine-wide GPU: {execution.gpu.utilization_percent}% utilization; {execution.gpu.used_vram_mib} / {execution.gpu.total_vram_mib} MiB VRAM. Includes other apps and workflows; refreshed while batches are active.</p>}
      </Card>}
      <Card className="mb-4">
        <div className="mb-4 flex gap-2" role="group" aria-label="Video input source">
          <Button aria-pressed={sourceMode === "local"} disabled={busy} onClick={() => setSourceMode("local")}>Local video upload</Button>
          <Button aria-pressed={sourceMode === "drive"} disabled={busy} onClick={() => setSourceMode("drive")}>Google Drive batch</Button>
        </div>
        {sourceMode === "drive" && isAdmin ? <DriveVideoBatchPanel settings={scanSettings} onSettings={setScanSettings} device={device}
          disabled={busy} onStarted={state => {
            const fresh = experimentResponse(state);
            selectionRequest.current++; recovered.current = true; setSelected(fresh); setClock(Date.now()); setError("");
            setSearchParams({ experiment: fresh.id }, { replace: true });
            void loadList().catch(err => setError(message(err)));
          }} /> : <>
        <label htmlFor="local-video-file" className="mb-2 block text-sm font-medium">Local video file</label>
        <input ref={input} id="local-video-file" type="file" accept=".mp4,.mov,.avi,.mkv,.webm,.m4v"
          disabled={busy} onChange={e => { setFile(e.target.files?.[0] || null); setError(""); }}
          className="block w-full text-sm" />
        <p className="my-2 text-xs text-gray-600">
          MP4, MOV, AVI, MKV, WebM, M4V; codec must decode with OpenCV/FFmpeg.
          No fixed file-size limit: the server needs free disk space for the whole file plus a 1 GiB reserve and space held by other active videos. Maximum 30 minutes, 4K pixels, 1-120 FPS.
          File selection does nothing. Upload validates media; Start experiment runs recognition.
        </p>
        <Button disabled={!file || busy} onClick={upload}>Upload video</Button>
        {uploading && <div className="mt-2 text-sm" role="status">
          Upload {uploadPercent}% {uploadPercent === 100 && "\u2014 validating video; recognition has not started"}
          <progress aria-label="Video upload progress" value={uploadPercent} max={100} className="block w-full" />
        </div>}
        </>}
      </Card>
      <div className="grid gap-4 md:grid-cols-[260px_1fr]">
        <Card>
          <h2 className="mb-2 font-semibold">Video experiments</h2>
          {!list.length && <p className="text-sm text-gray-500">No video experiments yet.</p>}
          <ul className="space-y-1">
            {list.map(exp => <li key={exp.id}>{exp.queue_position != null && <span className="text-xs">Queue turn {exp.queue_position}; {exp.active_workers ?? 0} active workers</span>}<button disabled={busy} onClick={() => void select(exp.id)}
              className={"w-full break-words rounded-lg px-3 py-2 text-left text-sm " + (selected?.id === exp.id ? "bg-indigo-50 text-indigo-700" : "hover:bg-gray-50")}>
              <div className="font-medium">{exp.filename}</div>
              {exp.kind === "drive_batch" && <div className="text-xs">Drive batch | {exp.total_videos} videos</div>}
              <div className="text-xs text-gray-500">{exp.status} | {exp.sampled_frames} sampled frames</div>
            </button></li>)}
          </ul>
          <Button className="mt-3" disabled={busy} onClick={() => void loadList().catch(err => setError(message(err)))}>Refresh list</Button>
        </Card>
        {selected ? <Card>
          <h2 ref={resultTitle} className="break-words font-semibold">{selected.filename}</h2>
          <p role="status" className="my-2 text-sm">Status: <strong>{selected.status}</strong></p>
          {selected.status === "ready" && <p className="mb-3 text-sm">Video validated. Recognition has not started.</p>}
          {selected.partial && <p className="mb-3 rounded bg-amber-50 p-2 text-sm text-amber-900">
            Partial results only. Completed sampled frames preserved. {active ? "Processing may add more results." : batch ? "Create a new batch to retry explicitly. Completed sources will not restart." : "Upload a new experiment to run again."}
          </p>}
          {selected.error && <p role="alert" className="mb-3 text-sm text-red-600">{selected.error}</p>}
          {selected.can_start && <ScanSettingsFields value={scanSettings} onChange={setScanSettings}
            disabled={busy || !isAdmin} video />}
          <div className="mb-4 flex flex-wrap gap-2">
            <Button disabled={busy || !isAdmin || !selected.can_start || !!scanSettingsError(scanSettings)} onClick={() => void action("start")}>Start experiment</Button>
            <Button disabled={busy || !selected.can_cancel || selected.status === "cancelling"} onClick={() => void action("cancel")}>
              {selected.status === "cancelling" ? "Cancelling at checkpoint\u2026" : "Cancel"}
            </Button>
            <Button disabled={busy || !selected.can_delete} onClick={() => void remove()}>Delete</Button>
            <Button onClick={() => void download()}>Download CSV</Button>
          </div>
          {batch && <p className="mb-2 text-sm">Batch: {selected.finished_videos} / {selected.total_videos} videos stopped; {selected.completed_videos} completed, {selected.failed_videos} failed.
            {" "}{selected.temporary_downloads_cleaned ? "Temporary downloads removed." : "Bounded temporary sources may be in use; each removed when it stops."} Original Drive files unchanged.</p>}
          {selected.waiting_reason && <p role="status">{selected.waiting_reason}</p>}
          <p className="text-sm">Progress: {selected.progress_percent.toFixed(1)}% {selected.progress_is_estimate && (batch ? "(estimate across download and decode phases)" : "(estimate against reported metadata)")} | {selected.decoded_frames} decoded frames
            {selected.verified_total_frames != null && " / " + selected.verified_total_frames + " verified total"}</p>
          {selected.decode_phase === "verifying_eof" && <p role="status" className="text-sm">Verifying readable EOF with independent decoder; recognition results preserved. Cancel remains available.</p>}
          {selected.decode_audit?.outcome === "normal_eof" && selected.media?.frame_count !== selected.verified_total_frames && <p className="text-sm text-amber-800">Clean EOF confirmed. Reported frame count differs from actual decoded frames; metadata does not prove missing or corrupt frames.</p>}
          <progress aria-label="Experiment processing progress" max={100} value={selected.progress_percent} className="my-2 block w-full" />
          <dl className="grid grid-cols-2 gap-2 text-sm">
            {selected.requested_provider && <><dt>Requested / actual provider</dt><dd>{selected.requested_provider} / {selected.actual_provider}</dd>
            <dt>Queue turn / active video workers</dt><dd>{selected.queue_position ?? "finished"} / {selected.active_workers ?? 0}</dd></>}
            {selected.stage_seconds && Object.entries(selected.stage_seconds).map(([stage, value]) => <div className="contents" key={stage}><dt>{stage.replaceAll("_", " ")}</dt><dd>{seconds(value)} (sum of worker time)</dd></div>)}
            <dt>Frame selection rule</dt><dd>{selected.can_start ? "Selected controls apply on Start; no scan has run" : selected.scan_settings ? "max(scan finish + wait, scan start + 1/target); latest virtual-camera frame" :
              selected.sampling_hz === null ? "Previous scan work + 400 ms; latest distinct frame" : selected.sampling_hz + " sample/s (earlier experiment)"}</dd>
            {selected.scan_settings && <>
              <dt>Camera FPS (virtual limit)</dt><dd>{selected.scan_settings.camera_fps}</dd>
              <dt>Wait after completed scan</dt><dd>{selected.scan_settings.post_scan_delay_seconds} s</dd>
              <dt>Target detections per second</dt><dd>{selected.scan_settings.target_detections_per_second} maximum intended starts/s</dd>
            </>}
            <dt>Maximum scan cadence</dt><dd>{selected.can_start ? "Calculated on Start" : selected.max_scan_hz} samples/s before scan work and source/virtual-FPS limits</dd>
            <dt>{batch ? "Aggregate cadence (sum of source intervals / spans)" : "Actual processed cadence (video time)"}</dt><dd>{selected.actual_video_detection_hz == null ? "Available after two samples in a source" : selected.actual_video_detection_hz.toFixed(3) + " samples/s"}</dd>
            <dt>Measured processing throughput (wall time)</dt><dd>{selected.processing_throughput_fps == null ? "\u2014" : selected.processing_throughput_fps.toFixed(3) + " sampled frames/s"}</dd>
            <dt>Decoded frames not inferred</dt><dd>{selected.decoded_frames_not_inferred}</dd>
            {!batch && <><dt>Video duration (nominal)</dt><dd>{seconds(selected.media?.duration_seconds)}</dd>
            <dt>Reported frame count (estimate)</dt><dd>{selected.media?.frame_count ?? "\u2014"}</dd>
            <dt>Verified readable frame total</dt><dd>{selected.verified_total_frames ?? "Not verified in this run"}</dd>
            <dt>Decoded source timestamp range</dt><dd>{seconds(selected.decoded_timestamp_first_seconds)} to {seconds(selected.decoded_timestamp_last_seconds)}</dd>
            <dt>Largest decoded timestamp gap</dt><dd>{seconds(selected.decoded_timestamp_max_gap_seconds)}</dd>
            <dt>Source FPS</dt><dd>{selected.media?.fps.toFixed(6) ?? "\u2014"}</dd></>}
            <dt>Frames sampled</dt><dd>{selected.sampled_frames}{!batch && <> / {selected.media?.expected_samples ?? selected.actual_max_samples_zero_work ?? selected.media?.max_samples ?? 0} {selected.media?.expected_samples != null ? "expected" : selected.actual_max_samples_zero_work != null ? "maximum on actual decoded frames with zero scan work" : "estimated maximum with zero scan work"}</>}</dd>
            <dt>Elapsed processing time</dt><dd>{seconds(elapsed)}</dd>
            <dt>Detected faces across sampled frames</dt><dd>{selected.detected_face_detections}</dd>
            <dt>Enrolled identities frozen at start</dt><dd>{selected.model?.enrolled_identities ?? "Captured when started"}</dd>
            <dt>Unique matched identities</dt><dd>{selected.unique_matched_people}</dd>
            <dt>Matched face detections</dt><dd>{selected.matched_face_detections}</dd>
            <dt>Unmatched face detections</dt><dd>{selected.unknown_detections}</dd>
            <dt>Sampled frames with unmatched faces</dt><dd>{selected.frames_with_unknown}</dd>
            <dt>Model / provider</dt><dd>{selected.model ? selected.model.name + " / " + selected.model.provider : "Captured when started"}</dd>
            <dt>Match threshold</dt><dd>{selected.model?.threshold ?? "\u2014"}</dd>
            <dt>Detector threshold / size</dt><dd>{selected.model ? selected.model.detection_threshold + " / " + selected.model.detection_size.join("x") : "\u2014"}</dd>
          </dl>
          <p className="my-3 text-xs text-gray-600">
            Rule: {selected.sampling_method}. Timestamp = frame index / source FPS (nominal).
            {selected.scan_settings
              ? " Start at frame 0. Next deadline is max(previous virtual capture + measured scan work + selected wait, previous virtual capture + 1/target). Pick the latest source frame at the latest virtual-camera tick before that deadline. If already inferred, wait for the next tick exposing a distinct frame. Decode sequentially; skip intervening frames; no scan backlog or real-time sleep."
              : selected.sampling_hz === null
              ? " Start at frame 0. After inference/matching finishes, advance virtual video time by measured scan work plus 0.400 s; pick floor(time x FPS), advancing to the next distinct frame at low FPS. Intervening frames decode sequentially without inference. No queue or real-time sleep."
              : " This saved experiment used the earlier fixed 1/s rule; its results and report retain that rule."}
          </p>
          {selected.historical_reference && <p className="mb-2 text-xs text-gray-600">
            Historical default reference: e4dbf60, CameraNode.runScanLoop/scanOnce, completion + 400 ms;
            requested capture up to 30 FPS is separate from detection cadence.
          </p>}
          <p className="mb-3 text-xs text-gray-600">
            {selected.timing_differences ?? "Earlier fixed-rate results: nominal FPS timestamps; no historical timing replay."}
            {" "}Actual video-time cadence = completed sample intervals / virtual capture-time span.
            Wall throughput = completed samples / elapsed processing time; offline decode and checkpoint time affect it.
            Reported frame count and nominal duration are metadata estimates. VFR or timestamp gaps do not imply corruption; v3 cadence still uses nominal index/FPS, so gaps are not replayed. Independent EOF validation distinguishes metadata mismatch from decoder damage. Truncated decode retains partial results.
          </p>
          {selected.model?.enrolled_identities === 0 && <p className="my-2 text-sm text-amber-800">No enrolled identities in snapshot. Detected faces are unmatched.</p>}
          {batch && <>
            <p className="my-3 text-sm">Combined counts sum source results once. They do not deduplicate the same person across cameras or measure passages. First/last timestamps below are minimum/maximum clip-relative timestamps; see each source for its exact range. Equal-score examples keep the earliest source (filename order), then earliest frame.</p>
            <VideoBatchSources sources={selected.videos ?? []} />
          </>}
          <h3 className="mb-2 font-semibold">{batch ? "Combined matched people" : "Matched people"}</h3>
          {!(selected.people?.length) && <p className="mb-3 text-sm text-gray-500">
            {selected.status === "ready" ? "No results yet." : selected.sampled_frames === 0
              ? "No sampled frame results yet." : selected.detected_face_detections === 0
              ? "No faces detected in sampled frames." : "Faces were detected, but no enrolled identity matched in sampled frames. See unmatched counts above."}
          </p>}
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead><tr><th className="p-2">Name</th><th className="p-2">Sampled frames detected</th><th className="p-2">First timestamp</th><th className="p-2">Last timestamp</th><th className="p-2">Example verdict</th><th className="p-2">Review</th></tr></thead>
              <tbody>{selected.people?.map(person => <tr key={person.identity_key} className="border-t border-gray-100">
                <td className="p-2">{person.name}</td><td className="p-2">{person.detection_count}</td>
                <td className="p-2">{seconds(person.first_timestamp_seconds)}</td><td className="p-2">{seconds(person.last_timestamp_seconds)}</td>
                <td className="p-2">{person.manual_verdict ?? "Not reviewed"}</td>
                <td className="p-2"><Button disabled={active} onClick={() => setReviewPerson(person)}>View match</Button>
                  {!person.preview_available && <p className="mt-1 text-xs text-gray-500">{person.preview_message ?? "Preview unavailable for this older run"}</p>}</td>
              </tr>)}</tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-gray-600">
            Person count: at most once per sampled frame, deduplicated by identity, even when multiple faces match that identity.
            Matched/unmatched face detections count individual detector results across sampled frames; repeated faces count again.
            Unknown faces have no persistent identity. Unique identities use enrolled identity keys, not names.
          </p>
          {!batch && <details className="mt-4">
            <summary className="cursor-pointer text-sm font-medium">Sampled timestamps and per-frame counts ({selected.sampled_frames})</summary>
            <div className="mt-2 max-h-64 overflow-auto">
              <table className="w-full text-left text-xs">
                <thead><tr><th>Frame index (zero-based)</th><th>Frame timestamp</th><th>Virtual capture time</th><th>Scan work</th><th>Faces</th><th>Matched identities</th><th>Unmatched faces</th></tr></thead>
                <tbody>{selected.samples?.map(sample => <tr key={sample.frame_index} className="border-t border-gray-100">
                  <td>{sample.frame_index}</td><td>{seconds(sample.timestamp_seconds)}</td><td>{seconds(sample.capture_time_seconds)}</td><td>{seconds(sample.scan_work_seconds)}</td><td>{sample.faces}</td>
                  <td>{sample.matched_people}</td><td>{sample.unknown_detections}</td>
                </tr>)}</tbody>
              </table>
            </div>
          </details>}
          <p className="mt-4 text-xs text-gray-600">
            CSV: UTF-8 with BOM. record_type=metadata uses key/value; record_type=person uses identity_key,
            name, detection_count, first_timestamp_seconds, last_timestamp_seconds, manual_verdict, review_meaning, preview_frame_number, preview_timestamp_seconds and preview_match_score.
            Verdict covers the shown example only, not every detection. Metadata includes status,
            partial flag, duration, source FPS, selected camera FPS, wait and target detections/s, selection rule, actual video-time cadence,
            wall throughput, historical reference, processing time, model/provider and thresholds.
            No accuracy metric without labeled ground truth.
            {batch && " Batch CSV appends source/statistics columns. metadata rows describe the batch; video rows contain per-source statistics; person rows contain combined counts and the representative source filename/verdict; video_person rows contain the breakdown. Do not sum person and video_person rows together. Clip timestamps have no shared camera clock."}
          </p>
        </Card> : <Card><p className="text-sm text-gray-500">Upload a video or select an existing video experiment.</p></Card>}
      </div>
    </div>
  );
}
