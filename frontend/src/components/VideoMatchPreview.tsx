import { useEffect, useRef, useState } from "react";
import { apiGet, apiPostJson } from "../api/client";
import { Button } from "./ui";

export type ExampleVerdict = "Not reviewed" | "Correct" | "Incorrect" | "Unsure";
export interface MatchExample {
  frame_index: number; frame_number: number; timestamp_seconds: number;
  source_timestamp_seconds?: number; match_score: number | null;
  score_meaning: string; selection_rule: string; width: number; height: number;
  bbox: number[]; example_key: string;
}
export interface MatchedPerson {
  identity_key: string; name: string; detection_count: number;
  first_timestamp_seconds: number; last_timestamp_seconds: number;
  preview?: MatchExample; preview_available?: boolean; preview_message?: string; manual_verdict?: ExampleVerdict;
}

export default function VideoMatchPreview({ jobId, person, active, onClose, onReviewed }: {
  jobId: string; person: MatchedPerson; active: boolean; onClose: () => void;
  onReviewed: (state: unknown) => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [imageReady, setImageReady] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [verdict, setVerdict] = useState<ExampleVerdict>(person.manual_verdict ?? "Not reviewed");
  const [saving, setSaving] = useState(false);
  const closeButton = useRef<HTMLButtonElement>(null);
  const example = person.preview;
  const endpoint = "/api/local-video-experiment/" + jobId + "/people/" + person.identity_key;
  useEffect(() => {
    const controller = new AbortController();
    let disposed = false, objectUrl: string | null = null;
    setUrl(null); setImageReady(false); setError(""); setZoom(1); setVerdict(person.manual_verdict ?? "Not reviewed");
    if (person.preview_available && example) {
      void apiGet(endpoint + "/preview?example_key=" + example.example_key, controller.signal)
        .then((blob: Blob) => {
          if (disposed) return;
          objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
        }).catch(err => { if (!disposed) setError(err instanceof Error ? err.message : "Preview could not be loaded."); });
    }
    return () => { disposed = true; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [endpoint, example?.example_key, person.preview_available, person.manual_verdict]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeButton.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "Tab") {
        const modal = closeButton.current?.closest('[role="dialog"]');
        const buttons = modal?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled)');
        if (buttons?.length) {
          const first = buttons[0], last = buttons[buttons.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
      }
    };
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("keydown", keydown); previous?.focus(); };
  }, [onClose]);
  async function save() {
    if (!example || active || saving || !imageReady) return;
    setSaving(true); setError("");
    try { onReviewed(await apiPostJson(endpoint + "/review", { verdict, example_key: example.example_key })); }
    catch (err) { setError(err instanceof Error ? err.message : "Review could not be saved."); }
    finally { setSaving(false); }
  }
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
    <section role="dialog" aria-modal="true" aria-labelledby="video-match-title" className="flex max-h-[95vh] w-full max-w-6xl flex-col rounded-xl bg-white p-5 shadow-xl">
      <div className="mb-2 flex items-start justify-between gap-4">
        <div><h2 id="video-match-title" className="text-lg font-semibold">Predicted match: {person.name}</h2>
          <p className="text-sm text-gray-600">{person.detection_count} sampled frames detected. This review covers one example only.</p></div>
        <button ref={closeButton} type="button" onClick={onClose} className="rounded border px-3 py-2 text-sm">Close</button>
      </div>
      {!person.preview_available || !example ? <p className="py-8 text-sm text-gray-600">{person.preview_message ?? "Preview unavailable for this older run"}. No video is reprocessed.</p> : <>
        <p className="mb-2 text-sm">Video timestamp: {(example.source_timestamp_seconds ?? example.timestamp_seconds).toFixed(3)} s
          {example.source_timestamp_seconds != null ? " (source PTS)" : " (nominal)"}. Frame {example.frame_number} (index {example.frame_index}, zero-based).
          {example.match_score == null ? " Match score unavailable." : " Cosine similarity: " + example.match_score.toFixed(4) + " (higher is stronger; not a probability)."}</p>
        <p className="mb-2 text-xs text-gray-600">{example.selection_rule}. Only the selected matched face is boxed. Full frame preserved; no face crop.</p>
        <label className="mb-2 flex items-center gap-3 text-sm">Zoom {zoom.toFixed(1)}x
          <input aria-label="Match preview zoom" type="range" min="1" max="4" step="0.25" value={zoom} onChange={e => setZoom(Number(e.target.value))} /></label>
        <div className="min-h-32 flex-1 overflow-auto rounded border bg-gray-950" style={{ maxHeight: "60vh" }}>
          {url ? <div className="relative" style={{ width: (zoom * 100) + "%" }}>
            <img src={url} onLoad={() => setImageReady(true)} onError={() => { setImageReady(false); setUrl(null); setError("Representative frame could not be displayed. No review saved."); }} alt={"Full representative video frame for predicted " + person.name} className="block h-auto w-full" />
            <svg aria-label={"Bounding box for " + person.name + " only"} className="pointer-events-none absolute inset-0 h-full w-full" viewBox={"0 0 " + example.width + " " + example.height} preserveAspectRatio="none">
              <rect x={example.bbox[0]} y={example.bbox[1]} width={example.bbox[2] - example.bbox[0]} height={example.bbox[3] - example.bbox[1]} fill="none" stroke="black" strokeWidth="7" vectorEffect="non-scaling-stroke" />
              <rect x={example.bbox[0]} y={example.bbox[1]} width={example.bbox[2] - example.bbox[0]} height={example.bbox[3] - example.bbox[1]} fill="none" stroke="#a3ff12" strokeWidth="3" vectorEffect="non-scaling-stroke" />
            </svg>
          </div> : <p className="p-6 text-sm text-white">{error ? "Preview could not be loaded." : "Loading representative frame..."}</p>}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className="text-sm">Verdict for shown example only <select aria-label="Verdict for shown example only" className="ml-2 rounded border px-2 py-1" value={verdict} disabled={active || saving || !url || !imageReady} onChange={e => setVerdict(e.target.value as ExampleVerdict)}>
            {["Not reviewed", "Correct", "Incorrect", "Unsure"].map(value => <option key={value}>{value}</option>)}
          </select></label>
          <Button disabled={active || saving || !url || !imageReady} onClick={() => void save()}>{saving ? "Saving..." : "Save example verdict"}</Button>
          {active && <p className="text-xs text-amber-800">Wait for processing to stop; strongest example can still change.</p>}
        </div>
      </>}
      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
      <p className="mt-2 text-xs text-gray-600">A verdict never changes detection counts and does not verify every appearance or calculate recognition accuracy.</p>
    </section>
  </div>;
}
