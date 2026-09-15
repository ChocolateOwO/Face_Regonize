import { useCallback, useEffect, useRef, useState } from "react";
import { fileUrl } from "../api/client";
import { type View, zoomAt } from "../lib/boxGeometry";
import { Button } from "./ui";

export interface ViewerPhoto {
  id: string;
  filename: string;
  media_path: string | null;
}

const IDENTITY: View = { scale: 1, tx: 0, ty: 0 };
const MAX_SCALE = 16;

/** Phase J2 — full-resolution viewer. Shows the privacy-rendered MEDIA file
 *  only (never ORIGINAL), so a declined participant stays masked here too.
 *  Wheel / +/- zoom around the cursor, drag to pan, arrows for prev/next,
 *  0 to reset, Esc to close. */
export default function ImageViewer({
  photos,
  index,
  onIndexChange,
  onClose,
  onEdit,
}: {
  photos: ViewerPhoto[];
  index: number;
  onIndexChange: (index: number) => void;
  onClose: () => void;
  onEdit?: (photoId: string) => void;
}) {
  const photo = photos[index];
  const [view, setView] = useState<View>(IDENTITY);
  const [drag, setDrag] = useState<{ x: number; y: number; view: View } | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);

  useEffect(() => setView(IDENTITY), [index]);

  const go = useCallback(
    (delta: number) => {
      const next = index + delta;
      if (next >= 0 && next < photos.length) onIndexChange(next);
    },
    [index, photos.length, onIndexChange],
  );

  const zoomCentre = useCallback((factor: number) => {
    const el = stageRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setView((v) => zoomAt(v, { x: r.width / 2, y: r.height / 2 }, factor, 1, MAX_SCALE));
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight") go(1);
      else if (e.key === "ArrowLeft") go(-1);
      else if (e.key === "+" || e.key === "=") zoomCentre(1.25);
      else if (e.key === "-") zoomCentre(0.8);
      else if (e.key === "0") setView(IDENTITY);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, onClose, zoomCentre]);

  // Non-passive, so zooming never scrolls the page underneath the overlay.
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    function onWheel(e: WheelEvent) {
      e.preventDefault();
      const r = el!.getBoundingClientRect();
      const p = { x: e.clientX - r.left, y: e.clientY - r.top };
      setView((v) => zoomAt(v, p, e.deltaY < 0 ? 1.2 : 1 / 1.2, 1, MAX_SCALE));
    }
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  if (!photo) return null;

  return (
    <div className="fixed inset-0 bg-black/90 z-50 flex flex-col" role="dialog" aria-label="Photo viewer">
      <div className="flex items-center justify-between gap-3 px-4 py-2 text-white flex-wrap">
        <div className="min-w-0">
          <div className="text-sm font-medium truncate">{photo.filename}</div>
          <div className="text-xs text-gray-400">
            {index + 1} of {photos.length} on this page · {Math.round(view.scale * 100)}%
          </div>
        </div>
        <div className="flex gap-2 items-center">
          <Button variant="secondary" onClick={() => go(-1)} disabled={index === 0}>
            ← Prev
          </Button>
          <Button variant="secondary" onClick={() => go(1)} disabled={index >= photos.length - 1}>
            Next →
          </Button>
          <Button variant="secondary" onClick={() => setView(IDENTITY)}>
            Reset
          </Button>
          {onEdit && photo.media_path && (
            <Button variant="secondary" onClick={() => onEdit(photo.id)}>
              Adjust face boxes
            </Button>
          )}
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
      <div
        ref={stageRef}
        data-testid="viewer-stage"
        className={`relative flex-1 min-h-0 overflow-hidden select-none touch-none ${view.scale > 1 ? "cursor-grab" : ""}`}
        onPointerDown={(e) => {
          if (view.scale <= 1) return;
          (e.target as Element).setPointerCapture?.(e.pointerId);
          setDrag({ x: e.clientX, y: e.clientY, view });
        }}
        onPointerMove={(e) => {
          if (!drag) return;
          setView({ ...drag.view, tx: drag.view.tx + (e.clientX - drag.x), ty: drag.view.ty + (e.clientY - drag.y) });
        }}
        onPointerUp={() => setDrag(null)}
        onPointerLeave={() => setDrag(null)}
        onDoubleClick={() => setView(IDENTITY)}
      >
        {photo.media_path ? (
          <div
            className="absolute inset-0"
            style={{ transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`, transformOrigin: "0 0" }}
          >
            <img
              src={fileUrl(photo.media_path)}
              alt={photo.filename}
              data-testid="viewer-image"
              draggable={false}
              className="w-full h-full object-contain"
            />
          </div>
        ) : (
          <p className="text-sm text-gray-300 p-6">This photo has not finished processing.</p>
        )}
      </div>
    </div>
  );
}
