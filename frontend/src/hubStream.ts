/**
 * Mice Hub cameras as CCTV camera sources.
 *
 * When the Mice Hub on this PC already holds a camera, the station must not
 * open it again (Windows lets one program hold a camera). The Hub shares it
 * as MJPEG; Reconize's backend relays that stream on its own origin
 * (GET /api/hub/cam.stream?dev=...), and this module turns it into an
 * ordinary MediaStream (canvas.captureStream), so preview, scanning and
 * recording in CameraNode.tsx work exactly as for a local camera - one
 * stream per camera, never reopened by a scan.
 *
 * Bounded: one frame is decoded at a time and only the newest waiting frame
 * is kept; the parser drops oversize or non-JPEG parts. A dropped or busy
 * stream reconnects with backoff (1 s doubling to 15 s, or the Hub's
 * Retry-After). fresh() tells the scan loop whether the picture is live, so a
 * frozen last frame is never scanned again while reconnecting.
 */

export const HUB_PREFIX = "hub:";
const SOI0 = 0xff, SOI1 = 0xd8, EOI1 = 0xd9;
export const MAX_FRAME_BYTES = 4 * 1024 * 1024;
export const MAX_BUFFER_BYTES = 2 * MAX_FRAME_BYTES;
export const FRESH_MS = 2000;
const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];
const FIRST_FRAME_TIMEOUT_MS = 10000;

export function isHubDevice(deviceId: string): boolean {
  return deviceId.startsWith(HUB_PREFIX);
}
export function hubDeviceId(dev: string): string {
  return HUB_PREFIX + dev;
}
export function hubDevOf(deviceId: string): string {
  return deviceId.slice(HUB_PREFIX.length);
}

function indexOf(buf: Uint8Array, a: number, b: number, from: number): number {
  for (let i = Math.max(from, 0); i + 1 < buf.length; i++) if (buf[i] === a && buf[i + 1] === b) return i;
  return -1;
}
function indexOfText(buf: Uint8Array, text: string, from = 0): number {
  const codes = [...text].map((c) => c.charCodeAt(0));
  outer: for (let i = from; i + codes.length <= buf.length; i++) {
    for (let k = 0; k < codes.length; k++) if (buf[i + k] !== codes[k]) continue outer;
    return i;
  }
  return -1;
}
function isJpeg(f: Uint8Array): boolean {
  let end = f.length;
  while (end > 0 && (f[end - 1] === 0x0d || f[end - 1] === 0x0a)) end--;
  return f.length >= 4 && f[0] === SOI0 && f[1] === SOI1 && f[end - 2] === SOI0 && f[end - 1] === EOI1;
}

/** Bytes in, complete JPEG frames out (same rules as the backend parser). */
export class MjpegParser {
  buf = new Uint8Array(0);
  invalid = 0;
  frames = 0;
  private maxFrame: number;
  private maxBuffer: number;
  constructor(maxFrame = MAX_FRAME_BYTES, maxBuffer = MAX_BUFFER_BYTES) {
    this.maxFrame = maxFrame;
    this.maxBuffer = maxBuffer;
  }

  push(chunk: Uint8Array): Uint8Array[] {
    const next = new Uint8Array(this.buf.length + chunk.length);
    next.set(this.buf);
    next.set(chunk, this.buf.length);
    this.buf = next;
    const out: Uint8Array[] = [];
    for (;;) {
      const f = this.next();
      if (!f) break;
      out.push(f);
    }
    if (this.buf.length > this.maxBuffer) {
      this.invalid++;
      this.buf = new Uint8Array(0);
    }
    return out;
  }

  private drop(n: number) {
    this.buf = this.buf.slice(n);
  }

  private next(): Uint8Array | null {
    for (;;) {
      const buf = this.buf;
      const start = indexOf(buf, SOI0, SOI1, 0);
      const headerEnd = indexOfText(buf, "\r\n\r\n");
      if (headerEnd !== -1 && (start === -1 || headerEnd < start)) {
        const header = new TextDecoder().decode(buf.slice(0, headerEnd)).toLowerCase();
        const m = /content-length:\s*(\d+)/.exec(header);
        if (m) {
          const length = Number(m[1]);
          const bodyAt = headerEnd + 4;
          if (length > this.maxFrame) {
            this.invalid++;
            this.drop(bodyAt);
            continue;
          }
          if (buf.length < bodyAt + length) return null;
          const frame = buf.slice(bodyAt, bodyAt + length);
          this.drop(bodyAt + length);
          if (isJpeg(frame)) {
            this.frames++;
            return frame;
          }
          this.invalid++;
          continue;
        }
        if (start === -1) {
          this.drop(headerEnd + 4);
          continue;
        }
      }
      if (start === -1) {
        if (buf.length > 1) this.drop(buf.length - 1);
        return null;
      }
      const end = indexOf(buf, SOI0, EOI1, start + 2);
      if (end === -1) {
        if (buf.length - start > this.maxFrame) {
          this.invalid++;
          this.drop(start + 2);
        }
        return null;
      }
      const frame = buf.slice(start, end + 2);
      this.drop(end + 2);
      if (frame.length > this.maxFrame) {
        this.invalid++;
        continue;
      }
      this.frames++;
      return frame;
    }
  }
}

export interface HubStreamHandle {
  stream: MediaStream;
  /** True while frames arrived in the last FRESH_MS. */
  fresh(): boolean;
  stop(): void;
}

export interface HubStreamDeps {
  fetchImpl?: typeof fetch;
  token?: () => string | null;
  decode?: (frame: Uint8Array) => Promise<{ width: number; height: number; close?: () => void } & CanvasImageSource>;
  makeCanvas?: () => HTMLCanvasElement;
  now?: () => number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  onState?: (state: "live" | "reconnecting" | "stopped", detail: string) => void;
  fps?: number;
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });
}

/** Opens the relayed Hub stream for `dev`. Resolves once the first frame is
 * drawn; rejects (and stops) if none arrives within 10 s. */
export function openHubCamera(dev: string, deps: HubStreamDeps = {}): Promise<HubStreamHandle> {
  const fetchImpl = deps.fetchImpl ?? fetch.bind(globalThis);
  const token = deps.token ?? (() => { try { return localStorage.getItem("token"); } catch { return null; } });
  const decode = deps.decode ?? ((f: Uint8Array) => createImageBitmap(new Blob([f as BlobPart], { type: "image/jpeg" })));
  const now = deps.now ?? (() => performance.now());
  const sleep = deps.sleep ?? defaultSleep;
  const onState = deps.onState ?? (() => {});
  const canvas = (deps.makeCanvas ?? (() => document.createElement("canvas")))();
  canvas.width = 640;
  canvas.height = 480;
  const ctx = canvas.getContext("2d");
  const stream = (canvas as HTMLCanvasElement & { captureStream(fps?: number): MediaStream }).captureStream(deps.fps ?? 15);
  const abort = new AbortController();
  let lastFrameAt = -Infinity;
  let pending: Uint8Array | null = null;
  let decoding = false;
  let stopped = false;
  let firstFrame: (() => void) | null = null;
  let failFirst: ((reason: string) => void) | null = null;

  async function drain() {
    if (decoding) return;
    decoding = true;
    try {
      while (pending && !stopped) {
        const frame = pending;
        pending = null;
        try {
          const img = await decode(frame);
          if (stopped) { img.close?.(); break; }
          if (canvas.width !== img.width || canvas.height !== img.height) {
            canvas.width = img.width;
            canvas.height = img.height;
          }
          ctx?.drawImage(img, 0, 0);
          img.close?.();
          lastFrameAt = now();
          if (firstFrame) { firstFrame(); firstFrame = null; }
        } catch {
          /* a frame the browser cannot decode is skipped */
        }
      }
    } finally {
      decoding = false;
    }
  }

  async function run() {
    let attempt = 0;
    while (!stopped) {
      let waitMs = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      let detail = "";
      try {
        const t = token();
        const res = await fetchImpl(`/api/hub/cam.stream?dev=${encodeURIComponent(dev)}`, {
          headers: t ? { Authorization: `Bearer ${t}` } : {}, signal: abort.signal, cache: "no-store",
        });
        if (res.status === 401 || res.status === 403) {
          detail = "Not allowed to read Hub cameras with this sign-in.";
          stopped = true;
          abort.abort();
          onState("stopped", detail);
          failFirst?.(detail);
          break;
        }
        if (!res.ok || !res.body) {
          const retry = Number(res.headers?.get?.("Retry-After"));
          if (retry > 0) waitMs = Math.min(retry * 1000, 30000);
          detail = res.status === 404 ? "The Mice Hub is not configured on this PC." : `Hub camera unavailable (HTTP ${res.status}).`;
        } else {
          const parser = new MjpegParser();
          const reader = res.body.getReader();
          for (;;) {
            const { value, done } = await reader.read();
            if (done || stopped) break;
            const frames = parser.push(value);
            if (frames.length) {
              pending = frames[frames.length - 1]; // newest wins: bounded, never a backlog
              attempt = 0;
              onState("live", "");
              void drain();
            }
          }
          detail = "The Hub stream ended.";
        }
      } catch (err) {
        if (stopped) break;
        detail = err instanceof Error ? err.message : "The Hub stream failed.";
      }
      if (stopped) break;
      onState("reconnecting", detail);
      attempt++;
      await sleep(waitMs, abort.signal);
    }
  }

  const handle: HubStreamHandle = {
    stream,
    fresh: () => now() - lastFrameAt < FRESH_MS,
    stop: () => {
      if (stopped && abort.signal.aborted) return;
      stopped = true;
      abort.abort();
      onState("stopped", "");
    },
  };
  // CameraNode stops a camera by stopping its tracks; a canvas track does not
  // tell anyone, so stopping it also stops the Hub connection.
  for (const track of stream.getTracks()) {
    const original = track.stop.bind(track);
    track.stop = () => { handle.stop(); original(); };
  }

  return new Promise<HubStreamHandle>((resolve, reject) => {
    const timer = setTimeout(() => failFirst?.("No picture from the Hub camera within 10 seconds."), FIRST_FRAME_TIMEOUT_MS);
    firstFrame = () => { clearTimeout(timer); failFirst = null; resolve(handle); };
    failFirst = (reason: string) => {
      clearTimeout(timer);
      firstFrame = null;
      failFirst = null;
      handle.stop();
      reject(new Error(reason));
    };
    void run();
  });
}

/** The Hub's cameras as CCTV devices ("Hub · <name>"), or [] without a Hub. */
export async function listHubCameras(get: (path: string) => Promise<{ enabled: boolean; cameras: { dev: string; name: string }[] }>):
  Promise<{ deviceId: string; label: string }[]> {
  try {
    const data = await get("/api/hub/cameras");
    if (!data?.enabled) return [];
    return data.cameras.map((c) => ({ deviceId: hubDeviceId(c.dev), label: `Hub · ${c.name}` }));
  } catch {
    return [];
  }
}
