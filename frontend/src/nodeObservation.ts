// Live presence for one CCTV camera: forward what the Local Agent just saw
// (every face, matched or not, with its box) to Central, which keeps it in
// memory for a few seconds so "who is at this camera right now" can be
// answered. Attendance is NOT sent here - that still goes through
// /api/node/event exactly as before.

export interface AgentFace { bbox: number[]; participant_id: string | null; confidence: number | null }
export interface AgentScan {
  faces_total: number;
  matches: { participant_id: string; name: string; confidence: number }[];
  // Present only on agents new enough to report presence.
  image?: { w: number; h: number };
  faces?: AgentFace[];
}

const REPORTER_KEY = "reconize.presenceReporter";
let memoryReporter = "";

/** A random id for THIS browser tab. Central binds a camera's presence to the
 *  tab that is reporting it, so a second station that was given the same node
 *  name is refused instead of mixing its faces in. Kept in sessionStorage so
 *  a page refresh is still the same reporter. */
export function reporterId(): string {
  try {
    const saved = sessionStorage.getItem(REPORTER_KEY);
    if (saved) return saved;
  } catch { /* storage unavailable: fall through to the in-memory id */ }
  if (!memoryReporter) {
    memoryReporter = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Date.now().toString(36);
  }
  try { sessionStorage.setItem(REPORTER_KEY, memoryReporter); } catch { /* in-memory only */ }
  return memoryReporter;
}

/** The request body for POST /api/node/observation, or null when the agent
 *  is an older build that does not describe every face. */
export function observationBody(nodeId: string, scan: AgentScan, reporter: string = reporterId()) {
  if (!nodeId || !scan.image || !Array.isArray(scan.faces) || !(scan.image.w > 0) || !(scan.image.h > 0)) return null;
  return { node_id: nodeId, image_w: scan.image.w, image_h: scan.image.h, faces: scan.faces, reporter_id: reporter };
}

/** Fire-and-forget: never awaited by the scan loop, and a failure is ignored
 *  (presence is best-effort; the next scan reports again). */
export function reportObservation(post: (path: string, body: unknown) => Promise<unknown>, nodeId: string, scan: AgentScan): void {
  const body = observationBody(nodeId, scan);
  if (!body) return;
  try {
    void post("/api/node/observation", body).catch(() => undefined);
  } catch {
    /* ignored - see above */
  }
}
