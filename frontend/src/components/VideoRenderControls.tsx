import { useState } from "react";
import { API_BASE, ApiError, apiDelete, apiPostJson } from "../api/client";
import { Button } from "./ui";

const API = "/api/local-video-experiment";

/** Optional tagged-video output, chosen before Start and frozen with the run. */
export type RenderMode = "auto" | "gpu" | "cpu";
export interface RenderChoice { enabled: boolean; keep_audio: boolean; detector: "plain" | "tiled"; decoder: RenderMode; encoder: RenderMode }
export const DEFAULT_RENDER_CHOICE: RenderChoice = { enabled: false, keep_audio: true, detector: "plain", decoder: "auto", encoder: "auto" };

/** What the backend records for one source's tagged video. */
export interface RenderRecord {
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted" | "not_rendered" | "deleted";
  detector: "plain" | "tiled"; keep_audio: boolean; error?: string | null;
  decoder_mode?: RenderMode; encoder_mode?: RenderMode;
  decoder?: string; decoder_note?: string | null; encoder?: string; encoder_label?: string; encoder_note?: string | null;
  audio?: string; audio_source_codec?: string | null; font?: string;
  width?: number; height?: number; padded_pixels?: number[];
  frames_written?: number; frames_expected?: number | null; progress_percent?: number | null;
  output_bytes?: number | null; duration_seconds?: number | null; seconds?: number;
  timing?: string; source_vfr?: boolean; timestamp_repairs?: number; people_shown?: number;
}

export const RENDER_ACTIVE = ["queued", "running"];
export const renderIsActive = (record?: RenderRecord | null) => !!record && RENDER_ACTIVE.includes(record.status);
export const renderBody = (choice: RenderChoice) => (choice.enabled ? { render: { ...choice } } : {});

const DETECTOR_LABEL: Record<string, string> = {
  plain: "Plain (default) — same full-frame detector as the experiment",
  tiled: "Tiled — finds small or distant faces; several times more GPU time",
};
// Decoding and encoding are chosen separately. GPU means GPU: if it cannot be
// used for a video the render stops and says why; it is never swapped for CPU.
const DECODER_LABEL: Record<RenderMode, string> = {
  auto: "Auto — CPU (measured no slower than the GPU here)", gpu: "GPU — NVDEC", cpu: "CPU — OpenCV FFmpeg",
};
const ENCODER_LABEL: Record<RenderMode, string> = {
  auto: "Auto — the faster, smaller path measured on this PC", gpu: "GPU — h264_nvenc", cpu: "CPU — libx264",
};
const MODE_WORD: Record<RenderMode, string> = { auto: "Auto", gpu: "GPU", cpu: "CPU" };

/** The two mode selectors, shared by the before-Start options and the later render. */
function ModeFields({ decoder, encoder, onChange, disabled }: {
  decoder: RenderMode; encoder: RenderMode; onChange: (patch: { decoder?: RenderMode; encoder?: RenderMode }) => void; disabled: boolean;
}) {
  return <>
    <label className="block">Decoding
      <select aria-label="Tagged video decoder" className="ml-2 rounded border p-1" value={decoder} disabled={disabled}
        onChange={event => onChange({ decoder: event.target.value as RenderMode })}>
        <option value="auto">{DECODER_LABEL.auto}</option>
        <option value="gpu">{DECODER_LABEL.gpu}</option>
        <option value="cpu">{DECODER_LABEL.cpu}</option>
      </select>
    </label>
    <label className="block">Encoding
      <select aria-label="Tagged video encoder" className="ml-2 rounded border p-1" value={encoder} disabled={disabled}
        onChange={event => onChange({ encoder: event.target.value as RenderMode })}>
        <option value="auto">{ENCODER_LABEL.auto}</option>
        <option value="gpu">{ENCODER_LABEL.gpu}</option>
        <option value="cpu">{ENCODER_LABEL.cpu}</option>
      </select>
    </label>
    {(decoder === "gpu" || encoder === "gpu") && <p role="note" className="text-xs text-gray-600">
      GPU is used exactly as chosen: if this computer cannot {decoder === "gpu" && encoder === "gpu" ? "decode or encode" : decoder === "gpu" ? "decode" : "encode"} a
      video on the GPU, that tagged video fails with the reason instead of quietly using the CPU.
      {encoder === "gpu" && " A 4K source is wider than the GPU's H.264 encoder accepts once the names panel is added."}
    </p>}
  </>;
}

const AUDIO_LABEL: Record<string, string> = {
  copied: "source audio copied", transcoded: "source audio converted to AAC", none: "source has no audio",
  not_requested: "audio not requested", incompatible: "source audio could not be used; video has no audio",
};
const STATUS_LABEL: Record<string, string> = {
  queued: "queued", running: "rendering", completed: "done", failed: "failed", cancelled: "cancelled",
  interrupted: "interrupted", not_rendered: "not rendered", deleted: "deleted",
};
const mib = (bytes: number) => (bytes >= 1024 ** 3 ? (bytes / 1024 ** 3).toFixed(2) + " GiB" : (bytes / 1024 ** 2).toFixed(1) + " MiB");
const message = (err: unknown) => (err instanceof Error ? err.message : "Request failed.");

export function RenderOptions({ value, onChange, disabled, drive = false }: {
  value: RenderChoice; onChange: (value: RenderChoice) => void; disabled: boolean; drive?: boolean;
}) {
  return <fieldset className="my-3 rounded border p-3 text-sm" aria-label="Tagged video options">
    <label className="flex items-center gap-2 font-medium">
      <input type="checkbox" aria-label="Render tagged video" checked={value.enabled} disabled={disabled}
        onChange={event => onChange({ ...value, enabled: event.target.checked })} />
      Render tagged video
    </label>
    <p className="mt-1 text-xs text-gray-600">
      An extra pass that writes a copy of {drive ? "each selected video" : "the video"} with every face boxed and named and a names panel on the right.
      It detects on every frame, so it adds GPU time; recognition counts, previews and the CSV are not changed by it.
      {drive && " Each Drive video is rendered while its temporary download still exists, then the download is deleted as usual; a Drive video cannot be rendered later."}
    </p>
    {value.enabled && <div className="mt-2 space-y-2">
      <label className="block">Face detection for the tagged video
        <select aria-label="Tagged video detector" className="ml-2 rounded border p-1" value={value.detector} disabled={disabled}
          onChange={event => onChange({ ...value, detector: event.target.value as RenderChoice["detector"] })}>
          <option value="plain">{DETECTOR_LABEL.plain}</option>
          <option value="tiled">{DETECTOR_LABEL.tiled}</option>
        </select>
      </label>
      {value.detector === "tiled" && <p role="note" className="rounded bg-amber-50 p-2 text-xs text-amber-900">
        Tiled scans the whole frame plus overlapping 640 px tiles on every frame: about 9 detector calls per 1080p frame and about 46 per 4K frame,
        instead of 1. Expect the render to take several times longer and live recognition to respond more slowly while it runs.
      </p>}
      <ModeFields decoder={value.decoder} encoder={value.encoder} disabled={disabled} onChange={patch => onChange({ ...value, ...patch })} />
      <label className="flex items-center gap-2">
        <input type="checkbox" aria-label="Keep source audio" checked={value.keep_audio} disabled={disabled}
          onChange={event => onChange({ ...value, keep_audio: event.target.checked })} />
        Keep the source audio
      </label>
    </div>}
  </fieldset>;
}

type FileWriter = { write: (chunk: Uint8Array) => Promise<void>; close: () => Promise<void>; abort: () => Promise<void> };
type SavePicker = (options: { suggestedName: string }) => Promise<{ createWritable: () => Promise<FileWriter> }>;

/** Tagged videos can be large: stream to a chosen file where the browser
 *  allows it (Chromium on localhost/HTTPS), otherwise fall back to a Blob. */
async function saveVideo(path: string, filename: string) {
  const picker = (window as unknown as { showSaveFilePicker?: SavePicker }).showSaveFilePicker;
  const target = picker ? await picker.call(window, { suggestedName: filename }) : undefined;
  const token = localStorage.getItem("token");
  const response = await fetch(API_BASE + path, { headers: token ? { Authorization: "Bearer " + token } : {} });
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("auth:unauthorized"));
    const data = await response.json().catch(() => null);
    throw new ApiError(response.status, data?.detail || response.statusText);
  }
  if (target && response.body) {
    const writer = await target.createWritable();
    const reader = response.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        await writer.write(value);
      }
      await writer.close();
    } catch (err) {
      await writer.abort().catch(() => undefined);
      throw err;
    }
    return;
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url; link.download = filename;
  document.body.appendChild(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** One source's tagged video: what is happening, what was actually used, and
 *  Download / Delete / Cancel. `onChanged` receives the refreshed experiment. */
export function RenderStatus({ jobId, sourceKey, filename, record, onChanged, disabled = false }: {
  jobId: string; sourceKey: string; filename: string; record?: RenderRecord | null;
  onChanged: (state: unknown) => void; disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!record) return null;
  const base = API + "/" + jobId + "/sources/" + sourceKey + "/render";
  async function run(work: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { const state = await work(); if (state) onChanged(state); }
    catch (err) { if (!(err instanceof DOMException && err.name === "AbortError")) setError(message(err)); }
    finally { setBusy(false); }
  }
  const stem = filename.replace(/\.[^.]+$/, "") || sourceKey;
  const active = renderIsActive(record);
  return <div className="text-xs" aria-label={"Tagged video for " + filename}>
    <span role="status"><strong>Tagged video: {STATUS_LABEL[record.status] ?? record.status}</strong>
      {record.status === "running" && record.progress_percent != null && <> {record.progress_percent.toFixed(1)}%</>}
      {record.status === "running" && record.frames_written != null && <> ({record.frames_written}{record.frames_expected ? " / ~" + record.frames_expected : ""} frames)</>}
      {record.status === "completed" && record.output_bytes != null && <> — {mib(record.output_bytes)}{record.width ? ", " + record.width + "×" + record.height : ""}</>}
    </span>
    <span className="block">Detector: {record.detector === "tiled" ? "Tiled (higher GPU cost)" : "Plain"}
      {record.decoder ? <> | Decoder: {record.decoder} ({MODE_WORD[record.decoder_mode ?? "auto"]})</>
        : record.decoder_mode && record.decoder_mode !== "auto" && <> | Decoder: {MODE_WORD[record.decoder_mode]} requested</>}
      {record.encoder ? <> | Encoder: {record.encoder_label ?? record.encoder} ({MODE_WORD[record.encoder_mode ?? "auto"]})</>
        : record.encoder_mode && record.encoder_mode !== "auto" && <> | Encoder: {MODE_WORD[record.encoder_mode]} requested</>}
      {record.audio && <> | Audio: {AUDIO_LABEL[record.audio] ?? record.audio}</>}</span>
    {record.encoder_note && <span className="block text-amber-800">Encoder fallback: {record.encoder_note}</span>}
    {record.decoder_note && <span className="block text-amber-800">{record.decoder_note}</span>}
    {record.status === "completed" && <span className="block">Timing: {record.timing ?? "source timestamps"}{record.source_vfr ? " (variable frame rate source)" : ""}
      {record.timestamp_repairs ? "; " + record.timestamp_repairs + " missing source timestamps replaced by the nominal frame interval" : ""}
      {record.padded_pixels && (record.padded_pixels[0] > 0 || record.padded_pixels[1] > 0) ? "; padded to even dimensions" : ""}
      {record.font?.startsWith("fallback") ? "; " + record.font : ""}</span>}
    {record.error && <span role="alert" className={"block " + (record.status === "failed" ? "text-red-700" : "text-gray-700")}>{record.error}</span>}
    <span className="mt-1 flex flex-wrap gap-2">
      {record.status === "completed" && <Button disabled={busy || disabled} onClick={() => void run(async () => { await saveVideo(base + ".mp4", stem + "_tagged.mp4"); return null; })}>
        {busy ? "Working…" : "Download tagged video"}</Button>}
      {active && <Button disabled={busy || disabled} onClick={() => void run(() => apiPostJson(base + "/cancel", {}))}>Cancel render</Button>}
      {!active && record.status !== "deleted" && record.status !== "not_rendered" && <Button disabled={busy || disabled} onClick={() => {
        if (record.status !== "completed" || window.confirm("Delete this tagged video? Recognition results and other videos are kept."))
          void run(() => apiDelete(base));
      }}>{record.status === "completed" ? "Delete tagged video" : "Clear"}</Button>}
    </span>
    {error && <span role="alert" className="block text-red-700">{error}</span>}
  </div>;
}

/** A retained local upload whose run completed can be rendered afterwards. */
export function RenderLater({ jobId, onChanged, disabled }: { jobId: string; onChanged: (state: unknown) => void; disabled: boolean }) {
  const [choice, setChoice] = useState<RenderChoice>({ ...DEFAULT_RENDER_CHOICE, enabled: true });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function start() {
    setBusy(true); setError("");
    try {
      onChanged(await apiPostJson(API + "/" + jobId + "/render",
        { keep_audio: choice.keep_audio, detector: choice.detector, decoder: choice.decoder, encoder: choice.encoder }));
    }
    catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }
  return <div className="my-3 rounded border p-3 text-sm" aria-label="Render tagged video later">
    <p className="font-medium">Tagged video</p>
    <p className="text-xs text-gray-600">This upload is still stored, so its tagged video can be made now as a queued job. Recognition results are not changed.</p>
    <label className="mt-2 block">Face detection
      <select aria-label="Tagged video detector" className="ml-2 rounded border p-1" value={choice.detector} disabled={busy || disabled}
        onChange={event => setChoice({ ...choice, detector: event.target.value as RenderChoice["detector"] })}>
        <option value="plain">{DETECTOR_LABEL.plain}</option>
        <option value="tiled">{DETECTOR_LABEL.tiled}</option>
      </select></label>
    <div className="mt-2 space-y-2">
      <ModeFields decoder={choice.decoder} encoder={choice.encoder} disabled={busy || disabled} onChange={patch => setChoice({ ...choice, ...patch })} />
    </div>
    <label className="mt-2 flex items-center gap-2"><input type="checkbox" aria-label="Keep source audio" checked={choice.keep_audio} disabled={busy || disabled}
      onChange={event => setChoice({ ...choice, keep_audio: event.target.checked })} />Keep the source audio</label>
    <Button className="mt-2" disabled={busy || disabled} onClick={() => void start()}>{busy ? "Queuing…" : "Render tagged video"}</Button>
    {error && <p role="alert" className="mt-1 text-xs text-red-700">{error}</p>}
  </div>;
}
