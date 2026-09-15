import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet, apiPutJson, fileUrl, ApiError } from "../api/client";
import { Button, Spinner } from "./ui";
import {
  type Box,
  type Handle,
  type View,
  constrain,
  moveBox,
  resizeBox,
  roundBox,
  sameBox,
  zoomAt,
} from "../lib/boxGeometry";

/** Phase D1 — face bounding-box editor.
 *
 *  Corrects MASK GEOMETRY only. It never changes an identity, a consent value
 *  or a visibility decision, and it is not a review queue.
 *
 *  The image shown is the privacy-rendered MEDIA copy — never ORIGINAL, so a
 *  declined participant stays masked even here. It is drawn INSIDE an SVG whose
 *  viewBox is the original pixel size, so a box's SVG coordinates ARE original
 *  pixels: pointer positions go through getScreenCTM().inverse(), which inverts
 *  zoom, pan, letterboxing and CSS transforms exactly. Zoom and pan are view
 *  state only and can never change stored geometry.
 *
 *  Follow-up Task 3 — clicking (or keyboard-selecting) a box shows who it
 *  belongs to in a side panel (desktop) or bottom drawer (mobile). The image
 *  itself only ever carries an outline and a small number — never a name.
 *  Selecting never moves a box: a drag begins only after the pointer travels
 *  DRAG_THRESHOLD_PX.
 */

interface ParticipantSummary {
  person_id: string;
  participant_id: string;
  name: string;
}

interface FaceGeom {
  id: string;
  bbox: Box;
  detected_bbox: Box | null;
  mask_required: boolean;
  masked: boolean;
  number: number;
  identity: "matched" | "unknown" | "deleted";
  participant: ParticipantSummary | null;
  consent_status_at_processing: string | null;
  match_confidence: number | null;
}

interface FacesPayload {
  photo_id: string;
  filename: string;
  media_path: string | null;
  media_version: string | null;
  width: number;
  height: number;
  min_box: number;
  editable: boolean;
  blocked_reason: string | null;
  faces: FaceGeom[];
}

type Drag =
  | { kind: "move"; id: string; start: { x: number; y: number }; box: Box; client: { x: number; y: number }; active: boolean }
  | { kind: "resize"; id: string; handle: Handle; box: Box; client: { x: number; y: number }; active: boolean }
  | { kind: "pan"; start: { x: number; y: number }; view: View };

const HANDLES: Handle[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const DRAG_THRESHOLD_PX = 4;

const CONSENT_LABEL: Record<string, string> = {
  consented: "Consented",
  declined: "Declined",
  pending: "Pending (no answer yet)",
  no_match: "Not applicable — no participant match",
};

function faceTitle(f: FaceGeom): string {
  if (f.identity === "matched" && f.participant) return f.participant.name || f.participant.participant_id;
  if (f.identity === "deleted") return "Participant no longer exists";
  return "Unknown";
}

function FaceDetails({ face, onClose }: { face: FaceGeom; onClose: () => void }) {
  return (
    <div data-testid="face-details" className="space-y-3 text-sm">
      <div className="flex items-center justify-between">
        <div className="font-semibold text-gray-900">Face {face.number}</div>
        <button type="button" onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xs" aria-label="Close face details">
          Close ✕
        </button>
      </div>
      {face.identity === "matched" && face.participant ? (
        <div>
          <div className="text-xs text-gray-500">Participant</div>
          <div className="font-medium text-gray-900" data-testid="face-participant-name">{face.participant.name || "—"}</div>
          <div className="text-xs text-gray-500" data-testid="face-participant-id">ID {face.participant.participant_id}</div>
        </div>
      ) : face.identity === "deleted" ? (
        <p data-testid="face-participant-deleted" className="text-amber-700 font-medium">Participant no longer exists</p>
      ) : (
        <p data-testid="face-unknown" className="text-gray-700 font-medium">Unknown / no participant match</p>
      )}
      {face.identity !== "unknown" && (
        <div>
          <div className="text-xs text-gray-500">Consent at processing time</div>
          <div data-testid="face-consent-snapshot">
            {CONSENT_LABEL[face.consent_status_at_processing ?? ""] ?? face.consent_status_at_processing ?? "—"}
          </div>
          <div className="text-[11px] text-gray-400">
            Snapshot recorded when this photo was processed. The current consent is on the participant page.
          </div>
        </div>
      )}
      <div>
        <div className="text-xs text-gray-500">Masking</div>
        <div data-testid="face-mask">{face.mask_required ? "Required — this face is masked" : "Not required — face stays visible"}</div>
      </div>
      {face.identity === "matched" && face.match_confidence !== null && (
        <div>
          <div className="text-xs text-gray-500">Match score (stored at processing)</div>
          <div>{face.match_confidence.toFixed(2)}</div>
        </div>
      )}
      {face.identity === "matched" && face.participant && (
        <Link
          to={`/people/${face.participant.person_id}`}
          data-testid="view-participant"
          className="inline-block px-3 py-1.5 rounded-lg bg-indigo-600 text-white text-xs font-medium hover:bg-indigo-700"
        >
          View participant
        </Link>
      )}
    </div>
  );
}

export default function BoxEditor({
  batchId,
  photoId,
  onClose,
  onSaved,
}: {
  batchId: string;
  photoId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [data, setData] = useState<FacesPayload | null>(null);
  const [boxes, setBoxes] = useState<Record<string, Box>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<View>({ scale: 1, tx: 0, ty: 0 });
  const [drag, setDrag] = useState<Drag | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const svgRef = useRef<SVGSVGElement>(null);
  const layerRef = useRef<SVGGElement>(null);

  const load = useCallback(() => {
    apiGet(`/api/photo-batches/${batchId}/photos/${photoId}/faces`)
      .then((d: FacesPayload) => {
        setData(d);
        setBoxes(Object.fromEntries(d.faces.map((f) => [f.id, f.bbox])));
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load this photo's faces."));
  }, [batchId, photoId]);

  useEffect(load, [load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (selected) setSelected(null);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, selected]);

  const faceById = useMemo(() => Object.fromEntries((data?.faces ?? []).map((f) => [f.id, f])), [data]);
  const selectedFace = selected ? faceById[selected] ?? null : null;

  const constraintsFor = useCallback(
    (id: string) => ({
      width: data?.width ?? 1,
      height: data?.height ?? 1,
      minSize: data?.min_box ?? 8,
      // Fail-closed: a masked face must keep covering what the detector found.
      mustContain: faceById[id]?.mask_required ? faceById[id].detected_bbox ?? faceById[id].bbox : null,
    }),
    [data, faceById],
  );

  /** Screen point -> a space, via the inverse screen CTM of `el`. */
  function toSpace(el: SVGGraphicsElement | null, clientX: number, clientY: number) {
    const svg = svgRef.current;
    const m = el?.getScreenCTM();
    if (!svg || !m) return { x: 0, y: 0 };
    const pt = svg.createSVGPoint();
    pt.x = clientX;
    pt.y = clientY;
    const r = pt.matrixTransform(m.inverse());
    return { x: r.x, y: r.y };
  }
  const toOriginal = (x: number, y: number) => toSpace(layerRef.current, x, y); // image pixels
  const toRoot = (x: number, y: number) => toSpace(svgRef.current, x, y); // viewBox, before zoom/pan

  function onPointerMove(e: React.PointerEvent) {
    if (!drag) return;
    if (drag.kind === "pan") {
      const p = toRoot(e.clientX, e.clientY);
      setView({ ...drag.view, tx: drag.view.tx + (p.x - drag.start.x), ty: drag.view.ty + (p.y - drag.start.y) });
      return;
    }
    // A click that selects a box must never move it: nothing changes until
    // the pointer has clearly travelled.
    if (!drag.active) {
      if (Math.hypot(e.clientX - drag.client.x, e.clientY - drag.client.y) < DRAG_THRESHOLD_PX) return;
      setDrag({ ...drag, active: true });
    }
    const p = toOriginal(e.clientX, e.clientY);
    const c = constraintsFor(drag.id);
    const next =
      drag.kind === "move"
        ? moveBox(drag.box, p.x - drag.start.x, p.y - drag.start.y, c)
        : resizeBox(drag.box, drag.handle, p, c);
    setBoxes((b) => ({ ...b, [drag.id]: next }));
  }

  function onWheel(e: React.WheelEvent) {
    const p = toRoot(e.clientX, e.clientY);
    setView((v) => zoomAt(v, p, e.deltaY < 0 ? 1.2 : 1 / 1.2));
  }

  const changed = useMemo(
    () => (data?.faces ?? []).filter((f) => boxes[f.id] && !sameBox(boxes[f.id], f.bbox)),
    [data, boxes],
  );

  async function save() {
    if (!data) return;
    setSaving(true);
    setError("");
    try {
      // Saved in ORIGINAL-image pixels, rounded to the backend's 0.1 px format.
      for (const f of changed) {
        const box = roundBox(constrain(boxes[f.id], constraintsFor(f.id)));
        await apiPutJson(`/api/photo-batches/${batchId}/photos/${photoId}/faces/${f.id}`, { bbox: box });
      }
      onSaved();
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not save. Nothing was changed.");
      load();
    } finally {
      setSaving(false);
    }
  }

  // Handles and labels keep a constant on-screen size whatever the zoom level.
  const pxPerUnit = (svgRef.current?.getScreenCTM()?.a ?? 1) * view.scale;
  const handle = 10 / pxPerUnit;
  const stroke = 2 / pxPerUnit;
  const labelSize = 12 / pxPerUnit;

  return (
    <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4" role="dialog" aria-label="Adjust face boxes">
      <div className="bg-white rounded-xl w-full max-w-6xl max-h-[95vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-gray-100 flex-wrap">
          <div>
            <h2 className="font-semibold text-gray-900">Adjust face boxes</h2>
            <p className="text-xs text-gray-500">
              {data?.filename} — click a box to see who it is; drag it to move it, drag its handles to resize, scroll to
              zoom, drag the image to pan. Masked faces can only be enlarged.
            </p>
          </div>
          <div className="flex gap-2 items-center">
            <Button variant="secondary" onClick={() => setView({ scale: 1, tx: 0, ty: 0 })}>Reset view</Button>
            <Button variant="secondary" onClick={onClose}>Close</Button>
            <Button onClick={save} disabled={!data?.editable || saving || changed.length === 0}>
              {saving ? "Saving..." : `Save${changed.length ? ` (${changed.length})` : ""}`}
            </Button>
          </div>
        </div>

        {error && <div className="mx-4 mt-3 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</div>}
        {data && !data.editable && (
          <div className="mx-4 mt-3 text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
            {data.blocked_reason}
          </div>
        )}

        <div className="flex-1 min-h-0 p-4 flex gap-4">
          <div className="flex-1 min-w-0">
            {!data ? (
              <Spinner label="Loading photo..." />
            ) : !data.media_path ? (
              <p className="text-sm text-gray-500">This photo has not finished processing.</p>
            ) : (
              <svg
                ref={svgRef}
                data-testid="box-editor-svg"
                viewBox={`0 0 ${data.width} ${data.height}`}
                preserveAspectRatio="xMidYMid meet"
                className="w-full h-[70vh] bg-gray-900 rounded-lg touch-none select-none"
                onWheel={onWheel}
                onPointerMove={onPointerMove}
                onPointerUp={() => setDrag(null)}
                onPointerLeave={() => setDrag(null)}
              >
                <g ref={layerRef} data-testid="box-editor-layer" transform={`translate(${view.tx} ${view.ty}) scale(${view.scale})`}>
                  <image
                    href={fileUrl(data.media_path, data.media_version ?? undefined)}
                    x={0}
                    y={0}
                    width={data.width}
                    height={data.height}
                    onPointerDown={(e) => {
                      (e.target as Element).setPointerCapture?.(e.pointerId);
                      setSelected(null);
                      setDrag({ kind: "pan", start: toRoot(e.clientX, e.clientY), view });
                    }}
                  />
                  {data.faces.map((f) => {
                    const [x1, y1, x2, y2] = boxes[f.id] ?? f.bbox;
                    const isSel = selected === f.id;
                    const color = f.mask_required ? "#ef4444" : "#6366f1";
                    const guard = f.mask_required ? f.detected_bbox ?? f.bbox : null;
                    const labelY = y1 > labelSize * 1.2 ? y1 - labelSize * 0.25 : y1 + labelSize;
                    return (
                      <g key={f.id} data-face-id={f.id}>
                        {guard && (
                          <rect x={guard[0]} y={guard[1]} width={guard[2] - guard[0]} height={guard[3] - guard[1]}
                                fill="none" stroke={color} strokeDasharray={`${4 * stroke} ${3 * stroke}`} strokeWidth={stroke} opacity={0.6} pointerEvents="none" />
                        )}
                        <rect
                          data-testid={`box-${f.id}`}
                          role="button"
                          tabIndex={0}
                          aria-label={`Face ${f.number}`}
                          aria-pressed={isSel}
                          x={x1} y={y1} width={x2 - x1} height={y2 - y1}
                          fill={isSel ? "rgba(99,102,241,0.12)" : "transparent"}
                          stroke={isSel ? "#facc15" : color} strokeWidth={isSel ? stroke * 1.8 : stroke}
                          style={{ cursor: data.editable ? "move" : "pointer", outline: "none" }}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              setSelected(f.id);
                            }
                          }}
                          onPointerDown={(e) => {
                            e.stopPropagation();
                            setSelected(f.id);
                            if (!data.editable) return;
                            (e.target as Element).setPointerCapture?.(e.pointerId);
                            setDrag({
                              kind: "move", id: f.id, start: toOriginal(e.clientX, e.clientY), box: boxes[f.id] ?? f.bbox,
                              client: { x: e.clientX, y: e.clientY }, active: false,
                            });
                          }}
                        />
                        {/* A small, non-identifying number — never a name. */}
                        <text
                          data-testid={`box-number-${f.id}`}
                          x={x1} y={labelY} fontSize={labelSize} fontWeight={700}
                          fill="white" stroke="black" strokeWidth={labelSize * 0.12} paintOrder="stroke"
                          pointerEvents="none"
                        >
                          {f.number}
                        </text>
                        {isSel && data.editable &&
                          HANDLES.map((h) => {
                            const hx = h.includes("w") ? x1 : h.includes("e") ? x2 : (x1 + x2) / 2;
                            const hy = h.includes("n") ? y1 : h.includes("s") ? y2 : (y1 + y2) / 2;
                            return (
                              <rect
                                key={h}
                                data-testid={`handle-${f.id}-${h}`}
                                x={hx - handle / 2} y={hy - handle / 2} width={handle} height={handle}
                                fill="white" stroke={color} strokeWidth={stroke}
                                style={{ cursor: `${h}-resize` }}
                                onPointerDown={(e) => {
                                  e.stopPropagation();
                                  (e.target as Element).setPointerCapture?.(e.pointerId);
                                  setDrag({
                                    kind: "resize", id: f.id, handle: h, box: boxes[f.id] ?? f.bbox,
                                    client: { x: e.clientX, y: e.clientY }, active: false,
                                  });
                                }}
                              />
                            );
                          })}
                      </g>
                    );
                  })}
                </g>
              </svg>
            )}
          </div>

          {/* Desktop: who the selected box belongs to, or the list of faces. */}
          {data && data.media_path && (
            <aside data-testid="face-panel" className="hidden md:block w-72 shrink-0 overflow-y-auto border-l border-gray-100 pl-4">
              {selectedFace ? (
                <FaceDetails face={selectedFace} onClose={() => setSelected(null)} />
              ) : (
                <div className="text-sm">
                  <div className="font-medium text-gray-900 mb-2">Faces in this photo</div>
                  {data.faces.length === 0 ? (
                    <p className="text-gray-500 text-xs">No faces were detected.</p>
                  ) : (
                    <ul className="space-y-1">
                      {data.faces.map((f) => (
                        <li key={f.id}>
                          <button
                            type="button"
                            data-testid={`face-list-${f.id}`}
                            onClick={() => setSelected(f.id)}
                            className="w-full text-left px-2 py-1 rounded hover:bg-gray-50 text-xs"
                          >
                            <span className="font-semibold">Face {f.number}</span> — {faceTitle(f)}
                            {f.mask_required && <span className="text-red-600"> · masked</span>}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                  <p className="text-[11px] text-gray-400 mt-3">Click a box, or Tab to it and press Enter, to see its details.</p>
                </div>
              )}
            </aside>
          )}
        </div>
        <div className="px-4 pb-3 text-xs text-gray-400 flex gap-4 flex-wrap">
          <span><span className="inline-block w-3 h-3 border-2 border-red-500 align-middle mr-1" />masked (declined) — dashed outline is the detected face it must keep covering</span>
          <span><span className="inline-block w-3 h-3 border-2 border-indigo-500 align-middle mr-1" />visible</span>
          <span>Zoom {Math.round(view.scale * 100)}%</span>
        </div>
      </div>

      {/* Mobile: the same details in a bottom drawer. */}
      {selectedFace && (
        <div data-testid="face-drawer" className="md:hidden fixed inset-x-0 bottom-0 z-[60] bg-white rounded-t-xl shadow-2xl p-4 max-h-[55vh] overflow-y-auto">
          <FaceDetails face={selectedFace} onClose={() => setSelected(null)} />
        </div>
      )}
    </div>
  );
}
