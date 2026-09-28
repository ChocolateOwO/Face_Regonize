import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPostJson, apiDelete, downloadFile, ApiError } from "../api/client";
import { Button, Card, PageHeader } from "../components/ui";

import VideoMatchPreview, { type MatchedPerson } from "../components/VideoMatchPreview";
import ScanSettingsFields from "../components/ScanSettingsFields";
import { DEFAULT_SCAN_SETTINGS, scanSettingsError, type ScanSettings } from "../api/scanSettings";

const API = "/api/local-video-experiment";
const ACTIVE = ["running", "cancelling"];
type Person = MatchedPerson;
interface Sample {
  frame_index: number; timestamp_seconds: number; faces: number;
  matched_people: number; unknown_detections: number;
  capture_time_seconds?: number; scan_work_seconds?: number;
}
interface Experiment {
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
  const [scanSettings, setScanSettings] = useState<ScanSettings>({ ...DEFAULT_SCAN_SETTINGS });
  const [isAdmin, setIsAdmin] = useState(false);
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
  selectedId.current = selected?.id ?? null;

  const loadList = useCallback(async () => {
    const data = await apiGet(API);
    setList(data.experiments as Experiment[]); setIsAdmin(true);
  }, []);
  useEffect(() => { void loadList().catch(err => setError(message(err))); }, [loadList]);
  const active = selected != null && ACTIVE.includes(selected.status);
  useEffect(() => {
    if (!active || !selected) return;
    const id = selected.id;
    let disposed = false;
    let timer: number;
    async function poll() {
      try {
        const fresh = await apiGet(API + "/" + id) as Experiment;
        if (disposed || selectedId.current !== id) return;
        setSelected(fresh); setClock(Date.now());
        if (!ACTIVE.includes(fresh.status)) { await loadList(); return; }
      } catch (err) { if (!disposed) setError(message(err)); }
      if (!disposed) timer = window.setTimeout(poll, 1000);
    }
    timer = window.setTimeout(poll, 500);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [active, selected?.id, loadList]);

  async function upload() {
    if (!file || busy) return;
    if (file.size > 512 * 1024 * 1024) { setError("Video exceeds 512 MiB upload limit."); return; }
    setBusy(true); setUploading(true); setError(""); setUploadPercent(0);
    try {
      const fresh = await uploadVideo(file, setUploadPercent);
      setSelected(fresh); setFile(null);
      if (input.current) input.current.value = "";
      await loadList();
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); setUploading(false); }
  }
  async function select(id: string) {
    setError("");
    try { setSelected(await apiGet(API + "/" + id) as Experiment); }
    catch (err) { setError(message(err)); }
  }
  async function action(kind: "start" | "cancel") {
    if (!selected || busy) return;
    if (kind === "start") {
      const problem = scanSettingsError(scanSettings);
      if (problem) { setError(problem); return; }
    }
    setBusy(true); setError("");
    try {
      setSelected(await apiPostJson(API + "/" + selected.id + "/" + kind, kind === "start" ? scanSettings : {}) as Experiment);
      setClock(Date.now()); await loadList();
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!selected || !window.confirm("Delete this experiment's uploaded video and local results? Other experiments and application data are preserved.")) return;
    setBusy(true); setError("");
    try { await apiDelete(API + "/" + selected.id); setSelected(null); await loadList(); }
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
  return (
    <div>
      {reviewPerson && selected && <VideoMatchPreview jobId={selected.id} person={reviewPerson} active={active}
        onClose={() => setReviewPerson(null)} onReviewed={state => {
          const fresh = state as Experiment; setSelected(fresh);
          setReviewPerson(fresh.people?.find(p => p.identity_key === reviewPerson.identity_key) ?? null);
        }} />}
      <PageHeader title="Local Video Experiment" subtitle="Count sampled frames containing enrolled identities" />
      <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        EXPERIMENT ONLY. Local video and results stay on this server. No Google Drive upload.
        Counts measure sampled-frame detections, not camera passages or recognition accuracy.
      </div>
      {error && <p role="alert" className="mb-3 text-sm text-red-600">{error}</p>}
      <Card className="mb-4">
        <label htmlFor="local-video-file" className="mb-2 block text-sm font-medium">Local video file</label>
        <input ref={input} id="local-video-file" type="file" accept=".mp4,.mov,.avi,.mkv,.webm,.m4v"
          disabled={busy} onChange={e => { setFile(e.target.files?.[0] || null); setError(""); }}
          className="block w-full text-sm" />
        <p className="my-2 text-xs text-gray-600">
          MP4, MOV, AVI, MKV, WebM, M4V; codec must decode with OpenCV/FFmpeg.
          Maximum 512 MiB, 30 minutes, 4K pixels, 1-120 FPS.
          File selection does nothing. Upload validates media; Start experiment runs recognition.
        </p>
        <Button disabled={!file || busy} onClick={upload}>Upload video</Button>
        {uploading && <div className="mt-2 text-sm" role="status">
          Upload {uploadPercent}% {uploadPercent === 100 && "\u2014 validating video; recognition has not started"}
          <progress aria-label="Video upload progress" value={uploadPercent} max={100} className="block w-full" />
        </div>}
      </Card>
      <div className="grid gap-4 md:grid-cols-[260px_1fr]">
        <Card>
          <h2 className="mb-2 font-semibold">Video experiments</h2>
          {!list.length && <p className="text-sm text-gray-500">No video experiments yet.</p>}
          <ul className="space-y-1">
            {list.map(exp => <li key={exp.id}><button disabled={busy} onClick={() => void select(exp.id)}
              className={"w-full break-words rounded-lg px-3 py-2 text-left text-sm " + (selected?.id === exp.id ? "bg-indigo-50 text-indigo-700" : "hover:bg-gray-50")}>
              <div className="font-medium">{exp.filename}</div>
              <div className="text-xs text-gray-500">{exp.status} | {exp.sampled_frames} sampled frames</div>
            </button></li>)}
          </ul>
          <Button className="mt-3" disabled={busy} onClick={() => void loadList().catch(err => setError(message(err)))}>Refresh list</Button>
        </Card>
        {selected ? <Card>
          <h2 className="break-words font-semibold">{selected.filename}</h2>
          <p role="status" className="my-2 text-sm">Status: <strong>{selected.status}</strong></p>
          {selected.status === "ready" && <p className="mb-3 text-sm">Video validated. Recognition has not started.</p>}
          {selected.partial && <p className="mb-3 rounded bg-amber-50 p-2 text-sm text-amber-900">
            Partial results only. Completed sampled frames preserved. {active ? "Processing may add more results." : "Upload a new experiment to run again."}
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
          <p className="text-sm">Progress: {selected.progress_percent.toFixed(1)}% {selected.progress_is_estimate && "(estimate against reported metadata)"} | {selected.decoded_frames} decoded frames
            {selected.verified_total_frames != null && " / " + selected.verified_total_frames + " verified total"}</p>
          {selected.decode_phase === "verifying_eof" && <p role="status" className="text-sm">Verifying readable EOF with independent decoder; recognition results preserved. Cancel remains available.</p>}
          {selected.decode_audit?.outcome === "normal_eof" && selected.media?.frame_count !== selected.verified_total_frames && <p className="text-sm text-amber-800">Clean EOF confirmed. Reported frame count differs from actual decoded frames; metadata does not prove missing or corrupt frames.</p>}
          <progress aria-label="Experiment processing progress" max={100} value={selected.progress_percent} className="my-2 block w-full" />
          <dl className="grid grid-cols-2 gap-2 text-sm">
            <dt>Frame selection rule</dt><dd>{selected.can_start ? "Selected controls apply on Start; no scan has run" : selected.scan_settings ? "max(scan finish + wait, scan start + 1/target); latest virtual-camera frame" :
              selected.sampling_hz === null ? "Previous scan work + 400 ms; latest distinct frame" : selected.sampling_hz + " sample/s (earlier experiment)"}</dd>
            {selected.scan_settings && <>
              <dt>Camera FPS (virtual limit)</dt><dd>{selected.scan_settings.camera_fps}</dd>
              <dt>Wait after completed scan</dt><dd>{selected.scan_settings.post_scan_delay_seconds} s</dd>
              <dt>Target detections per second</dt><dd>{selected.scan_settings.target_detections_per_second} maximum intended starts/s</dd>
            </>}
            <dt>Maximum scan cadence</dt><dd>{selected.can_start ? "Calculated on Start" : selected.max_scan_hz} samples/s before scan work and source/virtual-FPS limits</dd>
            <dt>Actual processed cadence (video time)</dt><dd>{selected.actual_video_detection_hz == null ? "Available after two samples" : selected.actual_video_detection_hz.toFixed(3) + " samples/s"}</dd>
            <dt>Measured processing throughput (wall time)</dt><dd>{selected.processing_throughput_fps == null ? "\u2014" : selected.processing_throughput_fps.toFixed(3) + " sampled frames/s"}</dd>
            <dt>Decoded frames not inferred</dt><dd>{selected.decoded_frames_not_inferred}</dd>
            <dt>Video duration (nominal)</dt><dd>{seconds(selected.media?.duration_seconds)}</dd>
            <dt>Reported frame count (estimate)</dt><dd>{selected.media?.frame_count ?? "\u2014"}</dd>
            <dt>Verified readable frame total</dt><dd>{selected.verified_total_frames ?? "Not verified in this run"}</dd>
            <dt>Decoded source timestamp range</dt><dd>{seconds(selected.decoded_timestamp_first_seconds)} to {seconds(selected.decoded_timestamp_last_seconds)}</dd>
            <dt>Largest decoded timestamp gap</dt><dd>{seconds(selected.decoded_timestamp_max_gap_seconds)}</dd>
            <dt>Source FPS</dt><dd>{selected.media?.fps.toFixed(6) ?? "\u2014"}</dd>
            <dt>Frames sampled</dt><dd>{selected.sampled_frames} / {selected.media?.expected_samples ?? selected.actual_max_samples_zero_work ?? selected.media?.max_samples ?? 0} {selected.media?.expected_samples != null ? "expected" : selected.actual_max_samples_zero_work != null ? "maximum on actual decoded frames with zero scan work" : "estimated maximum with zero scan work"}</dd>
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
          <h3 className="mb-2 font-semibold">Matched people</h3>
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
          <details className="mt-4">
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
          </details>
          <p className="mt-4 text-xs text-gray-600">
            CSV: UTF-8 with BOM. record_type=metadata uses key/value; record_type=person uses identity_key,
            name, detection_count, first_timestamp_seconds, last_timestamp_seconds, manual_verdict, review_meaning, preview_frame_number, preview_timestamp_seconds and preview_match_score.
            Verdict covers the shown example only, not every detection. Metadata includes status,
            partial flag, duration, source FPS, selected camera FPS, wait and target detections/s, selection rule, actual video-time cadence,
            wall throughput, historical reference, processing time, model/provider and thresholds.
            No accuracy metric without labeled ground truth.
          </p>
        </Card> : <Card><p className="text-sm text-gray-500">Upload a video or select an existing video experiment.</p></Card>}
      </div>
    </div>
  );
}
