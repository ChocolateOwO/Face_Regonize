/**
 * Phase D1 — pure geometry for the bounding-box editor.
 *
 * Every box handled here is in ORIGINAL-image pixel coordinates, the only
 * space the backend accepts. The editor draws the image inside an SVG whose
 * viewBox IS the original pixel size, so screen → original is a single
 * `getScreenCTM().inverse()` — which already accounts for zoom, pan,
 * letterboxing (`preserveAspectRatio`) and any CSS transform. The functions
 * below never see a display coordinate, which is why zooming or panning can
 * never alter stored geometry.
 */

export type Box = [number, number, number, number]; // x1, y1, x2, y2
export type Handle = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
export interface View { scale: number; tx: number; ty: number } // local = (root - t) / scale

export interface Constraints {
  width: number;
  height: number;
  minSize: number;
  /** For a privacy-masked face: the box must keep covering this region. */
  mustContain?: Box | null;
}

const EPS = 1e-9;

export function normalize(b: Box): Box {
  return [Math.min(b[0], b[2]), Math.min(b[1], b[3]), Math.max(b[0], b[2]), Math.max(b[1], b[3])];
}

/** The backend stores boxes rounded to 0.1 px; do the same before comparing/saving. */
export function roundBox(b: Box): Box {
  return b.map((v) => Math.round(v * 10) / 10) as Box;
}

export function sameBox(a: Box, b: Box): boolean {
  const ra = roundBox(a), rb = roundBox(b);
  return ra.every((v, i) => Math.abs(v - rb[i]) < EPS);
}

export function containsBox(outer: Box, inner: Box, tol = 0.5): boolean {
  return outer[0] <= inner[0] + tol && outer[1] <= inner[1] + tol &&
         outer[2] >= inner[2] - tol && outer[3] >= inner[3] - tol;
}

/** Clamp every edge so the box lies inside the image, is at least minSize,
 *  and — for a masked face — still contains the protected region. The
 *  containment rule wins over everything but the image bounds. */
export function constrain(box: Box, c: Constraints): Box {
  let [x1, y1, x2, y2] = normalize(box);
  x1 = Math.max(0, x1); y1 = Math.max(0, y1);
  x2 = Math.min(c.width, x2); y2 = Math.min(c.height, y2);
  if (c.mustContain) {
    const [ix1, iy1, ix2, iy2] = c.mustContain;
    x1 = Math.min(x1, ix1); y1 = Math.min(y1, iy1);
    x2 = Math.max(x2, ix2); y2 = Math.max(y2, iy2);
  }
  if (x2 - x1 < c.minSize) {
    x2 = Math.min(c.width, x1 + c.minSize);
    x1 = Math.max(0, x2 - c.minSize);
  }
  if (y2 - y1 < c.minSize) {
    y2 = Math.min(c.height, y1 + c.minSize);
    y1 = Math.max(0, y2 - c.minSize);
  }
  return [x1, y1, x2, y2];
}

/** Translate a box, keeping its size, then keep it inside the image and (for
 *  a masked face) still covering the protected region. */
export function moveBox(box: Box, dx: number, dy: number, c: Constraints): Box {
  const [x1, y1, x2, y2] = normalize(box);
  const w = x2 - x1, h = y2 - y1;
  let nx = x1 + dx, ny = y1 + dy;
  let minX = 0, maxX = c.width - w, minY = 0, maxY = c.height - h;
  if (c.mustContain) {
    const [ix1, iy1, ix2, iy2] = c.mustContain;
    minX = Math.max(minX, ix2 - w); maxX = Math.min(maxX, ix1);
    minY = Math.max(minY, iy2 - h); maxY = Math.min(maxY, iy1);
  }
  nx = Math.min(Math.max(nx, minX), Math.max(minX, maxX));
  ny = Math.min(Math.max(ny, minY), Math.max(minY, maxY));
  return constrain([nx, ny, nx + w, ny + h], c);
}

/** Drag one handle to point p (original coords). */
export function resizeBox(box: Box, handle: Handle, p: { x: number; y: number }, c: Constraints): Box {
  let [x1, y1, x2, y2] = normalize(box);
  if (handle.includes("w")) x1 = Math.min(p.x, x2 - c.minSize);
  if (handle.includes("e")) x2 = Math.max(p.x, x1 + c.minSize);
  if (handle.includes("n")) y1 = Math.min(p.y, y2 - c.minSize);
  if (handle.includes("s")) y2 = Math.max(p.y, y1 + c.minSize);
  return constrain([x1, y1, x2, y2], c);
}

/** Inverse of the view transform `translate(tx ty) scale(s)`: a point in the
 *  SVG root's viewBox space → original-image space. */
export function rootToLocal(p: { x: number; y: number }, v: View): { x: number; y: number } {
  return { x: (p.x - v.tx) / v.scale, y: (p.y - v.ty) / v.scale };
}

/** Zoom by `factor` keeping root-space point `p` fixed under the cursor. */
export function zoomAt(v: View, p: { x: number; y: number }, factor: number, min = 1, max = 16): View {
  const scale = Math.min(max, Math.max(min, v.scale * factor));
  const k = scale / v.scale;
  return { scale, tx: p.x - (p.x - v.tx) * k, ty: p.y - (p.y - v.ty) * k };
}

export function parseBbox(text: string): Box {
  const [a, b, c, d] = text.split(",").map(Number);
  return [a, b, c, d];
}
