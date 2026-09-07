import { memo, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { apiGet, apiPostForm, apiPostJson } from "../api/client";
import {
  cameraUnavailableReason,
  listCameras,
  listCamerasWithPermission,
  videoConstraints,
  type CameraDevice,
} from "../api/cameras";
import { Badge, Button, Card, Input, PageHeader } from "../components/ui";

/**
 * A camera station. Two genuinely different shapes live on this one page:
 *
 *  - CENTRAL inference (phone/iPad, no Local Agent): exactly one physical
 *    camera, because a phone has exactly one. This is the ORIGINAL
 *    single-stream flow, unmodified by the multi-camera work below - see
 *    startCamera()/stopEverything() and everything that touches streamRef.
 *
 *  - LOCAL inference (a Windows PC with a Local Node Agent): this PC may
 *    have several physical cameras plugged in at once, and this task is
 *    about running face recognition on all of them simultaneously. See
 *    "MULTI-CAMERA (local inference only)" below - it manages an array of
 *    independent Camera Instances, each with its own getUserMedia stream,
 *    its own scan loop, its own busy-guard and its own Recent Detections,
 *    registered with Central as its own node_id (e.g. "CAM-Test-01"). The
 *    ONE Local Agent underneath is unaffected by how many cameras call it -
 *    it already treats /recognize as camera-agnostic (see node_agent.py).
 *
 * CCTV publishing (WebRTC) stays exactly as it was, and only for the
 * CENTRAL/single-camera path - it is explicitly out of scope for the new
 * multi-camera cards in this task.
 */

type InferenceMode = "local" | "central";
type NodeMode = "tap" | "always";

interface PersonResult {
  person_id: string;
  participant_id?: string;
  name: string;
  checkin: "new" | "already";
}
interface FlashEntry extends PersonResult {
  key: number;
  leaving: boolean;
}
type FlashTimer = { key: number; expireId: number; removeId: number };

const FLASH_MS = 5000;
const FLASH_FADE_MS = 400;
const FLASH_MAX = 7;
const SCAN_DELAY_MS = 400; // a station is unattended and long-running — gentler than the kiosk's 200ms
const HEARTBEAT_MS = 3000;
const AGENT_HEALTH_MS = 4000;

const CONFIG_KEY = "reconize_node_config";

interface NodeConfig {
  node_id: string;
  display_name: string;
  activity_id: string;
  camera_label: string;
  mode: NodeMode;
  inference_mode: InferenceMode;
  agent_port: string;
}

function loadSavedConfig(): NodeConfig | null {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    return raw ? (JSON.parse(raw) as NodeConfig) : null;
  } catch {
    return null;
  }
}
function saveConfig(cfg: NodeConfig): void {
  try {
    localStorage.setItem(CONFIG_KEY, JSON.stringify(cfg));
  } catch {
    /* private mode / storage disabled — the station still works this session */
  }
}

function wsBase(): string {
  return (window.location.protocol === "https:" ? "wss://" : "ws://") + window.location.host;
}

function describeCameraError(err: unknown): string {
  const name = err instanceof DOMException ? err.name : "";
  switch (name) {
    case "NotAllowedError":
      return "CAMERA PERMISSION DENIED. Please allow camera access in your browser settings and try again.";
    case "NotFoundError":
      return "CAMERA UNAVAILABLE — no camera was found on this device.";
    case "NotReadableError":
      return "CAMERA UNAVAILABLE — it may already be in use by another application.";
    case "OverconstrainedError":
      return "The selected camera is no longer available. Choose another one.";
    default:
      return err instanceof Error ? err.message : "Could not access the camera.";
  }
}

// A shared, low-load resolution for every camera this page opens, whether
// it is the single central-inference stream or one of several local-mode
// instances — an unconstrained request lets the browser pick the camera's
// native resolution (often 1080p+), and on a GPU that is also running
// InsightFace for two or three OTHER cameras at once, that is what turns
// "live video" into video running seconds behind.
const CAMERA_CONSTRAINTS = {
  width: { ideal: 1280, max: 1280 },
  height: { ideal: 720, max: 720 },
  frameRate: { ideal: 30, max: 30 },
};

export default function CameraNode() {
  const [searchParams] = useSearchParams();
  const [activities, setActivities] = useState<{ id: string; name: string }[]>([]);
  const [cameras, setCameras] = useState<CameraDevice[]>([]);
  const [config, setConfig] = useState<NodeConfig | null>(null);
  const [setupError, setSetupError] = useState("");

  // ---- setup form state (only used before the station is configured) ----
  const [formNodeId, setFormNodeId] = useState(searchParams.get("node") ?? "");
  const [formDisplayName, setFormDisplayName] = useState("");
  const [formActivity, setFormActivity] = useState(searchParams.get("activity") ?? "");
  const [formCameraLabel, setFormCameraLabel] = useState("");
  // Distributed stations are unattended, so there is no tap-to-scan/consent
  // screen here — that flow belongs to the guest-facing kiosk (Recognition.tsx)
  // and is out of this task's scope. Every node runs continuously, matching
  // the kiosk's existing "always" mode.
  const formMode: NodeMode = "always";
  const [formInference, setFormInference] = useState<InferenceMode>(
    (searchParams.get("inference") as InferenceMode | null) ?? "local",
  );
  const [formAgentPort, setFormAgentPort] = useState(searchParams.get("agentPort") ?? "8101");

  // ---- shared live-station state (both branches) ----
  const [centralOnline, setCentralOnline] = useState(true);
  const [agentStatus, setAgentStatus] = useState<"unknown" | "ready" | "offline">("unknown");
  const [agentIndexInfo, setAgentIndexInfo] = useState("");
  const [agentError, setAgentError] = useState("");
  const agentHealthTimerRef = useRef<number | null>(null);

  // ---- load existing config / activities / cameras ----
  useEffect(() => {
    apiGet("/api/activities").then((d) => setActivities(d.activities)).catch(() => {});
    listCameras().then(setCameras).catch(() => {});

    const urlNode = searchParams.get("node");
    const saved = loadSavedConfig();
    if (urlNode && saved && saved.node_id === urlNode) {
      setConfig(saved);
    } else if (urlNode) {
      apiGet(`/api/node/config/${encodeURIComponent(urlNode)}`)
        .then((c) =>
          setConfig({
            node_id: c.node_id, display_name: c.display_name, activity_id: c.activity_id ?? "",
            camera_label: c.camera_label, mode: c.mode, inference_mode: c.inference_mode,
            agent_port: searchParams.get("agentPort") ?? "8101",
          }),
        )
        .catch(() => {
          setFormNodeId(urlNode);
        });
    } else if (saved) {
      setConfig(saved);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submitSetup(e: React.FormEvent) {
    e.preventDefault();
    setSetupError("");
    const node_id = formNodeId.trim();
    if (!node_id) {
      setSetupError("Give this station a name, e.g. CAM-02.");
      return;
    }
    const cfg: NodeConfig = {
      node_id, display_name: formDisplayName.trim() || node_id,
      activity_id: formActivity, camera_label: formCameraLabel,
      mode: formMode, inference_mode: formInference, agent_port: formAgentPort.trim(),
    };
    try {
      await apiPostJson("/api/node/register", {
        node_id: cfg.node_id, display_name: cfg.display_name, activity_id: cfg.activity_id || null,
        camera_label: cfg.camera_label, mode: cfg.mode, inference_mode: cfg.inference_mode,
      });
    } catch (err) {
      setSetupError(err instanceof Error ? err.message : "Could not register this station with Central.");
      return;
    }
    saveConfig(cfg);
    setConfig(cfg);
  }

  function activityName(id: string): string {
    return activities.find((a) => a.id === id)?.name ?? "";
  }

  function startAgentHealthPoll(port: string) {
    const buildAgentCommand = () =>
      `python node_agent.py --node-id ${config?.node_id || "CAM-02"} --port ${port} --central ${window.location.origin}`;

    const tick = () => {
      fetch(`http://127.0.0.1:${port}/health`)
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((d) => {
          setAgentStatus("ready");
          setAgentIndexInfo(`${d.participants_cached} cached · Central ${d.central_reachable ? "reachable" : "UNREACHABLE from agent"}`);
          setAgentError("");
        })
        .catch(() => {
          // Direct health check failed. Try Central's /api/node/status as
          // fallback — the agent self-reports local_agent_ok=true independently
          // (node_agent.py heartbeat_loop), so this can distinguish "agent is
          // running but browser blocks the HTTP call (HTTPS mixed content)"
          // from "agent process is genuinely not running".
          apiGet("/api/node/status")
            .then((status) => {
              const ourNode = status.nodes.find((n: any) => n.node_id === config?.node_id);
              if (ourNode?.local_agent_ok === true) {
                setAgentStatus("ready");
                setAgentIndexInfo("Agent running (via Central) — but direct /recognize calls may be blocked by mixed content");
                setAgentError("");
              } else if (window.location.protocol === "https:") {
                setAgentStatus("offline");
                setAgentError(
                  "Mixed content: your browser blocks HTTP to the Local Recognition Agent from an HTTPS page.\n" +
                  "Open this page over HTTP (http://), or run the agent with --https + a certificate the browser trusts."
                );
              } else {
                setAgentStatus("offline");
                setAgentError(
                  "Local Recognition Agent not running.\n\nStart it with:\n" + buildAgentCommand()
                );
              }
            })
            .catch(() => {
              setAgentStatus("offline");
              if (window.location.protocol === "https:") {
                setAgentError(
                  "Mixed content: your browser blocks HTTP to the Local Recognition Agent from an HTTPS page.\n" +
                  "Open this page over HTTP (http://), or run the agent with --https + a certificate the browser trusts."
                );
              } else {
                setAgentError(
                  "Local Recognition Agent not running.\n\nStart it with:\n" + buildAgentCommand()
                );
              }
            });
    });
    };
    tick();
    agentHealthTimerRef.current = window.setInterval(tick, AGENT_HEALTH_MS);
  }

  useEffect(() => {
    if (config?.inference_mode === "local") startAgentHealthPoll(config.agent_port || "8101");
    return () => {
      if (agentHealthTimerRef.current) window.clearInterval(agentHealthTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config?.inference_mode, config?.agent_port]);

  if (!config) {
    return (
      <SetupForm
        formNodeId={formNodeId} setFormNodeId={setFormNodeId}
        formDisplayName={formDisplayName} setFormDisplayName={setFormDisplayName}
        formActivity={formActivity} setFormActivity={setFormActivity}
        formCameraLabel={formCameraLabel} setFormCameraLabel={setFormCameraLabel}
        formInference={formInference} setFormInference={setFormInference}
        formAgentPort={formAgentPort} setFormAgentPort={setFormAgentPort}
        activities={activities} cameras={cameras} setupError={setupError}
        onSubmit={submitSetup}
      />
    );
  }

  if (config.inference_mode === "local") {
    return (
      <MultiCameraStation
        config={config} activityName={activityName}
        centralOnline={centralOnline} setCentralOnline={setCentralOnline}
        agentStatus={agentStatus} agentIndexInfo={agentIndexInfo} agentError={agentError}
      />
    );
  }

  return (
    <SingleCameraStation
      config={config} activityName={activityName}
      centralOnline={centralOnline} setCentralOnline={setCentralOnline}
    />
  );
}

// ===========================================================================
// SETUP FORM — unchanged from the single-station design; registers the HOST
// (a PC, or a phone). Multiple physical cameras are added AFTER this, one at
// a time, only in the "local" branch below.
// ===========================================================================

function SetupForm(props: {
  formNodeId: string; setFormNodeId: (v: string) => void;
  formDisplayName: string; setFormDisplayName: (v: string) => void;
  formActivity: string; setFormActivity: (v: string) => void;
  formCameraLabel: string; setFormCameraLabel: (v: string) => void;
  formInference: InferenceMode; setFormInference: (v: InferenceMode) => void;
  formAgentPort: string; setFormAgentPort: (v: string) => void;
  activities: { id: string; name: string }[]; cameras: CameraDevice[]; setupError: string;
  onSubmit: (e: React.FormEvent) => void;
}) {
  const {
    formNodeId, setFormNodeId, formDisplayName, setFormDisplayName, formActivity, setFormActivity,
    formCameraLabel, setFormCameraLabel, formInference, setFormInference, formAgentPort, setFormAgentPort,
    activities, cameras, setupError, onSubmit,
  } = props;
  return (
    <div className="max-w-xl mx-auto">
      <PageHeader title="Set up this device as a camera node" subtitle="Every station registers itself once — after that it remembers its setup on this browser." />
      <Card>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          {setupError && <div className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">{setupError}</div>}
          <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
            Node ID
            <Input value={formNodeId} onChange={(e) => setFormNodeId(e.target.value)} placeholder="CAM-02" required />
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
            Display name
            <Input value={formDisplayName} onChange={(e) => setFormDisplayName(e.target.value)} placeholder="Entrance camera" />
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
            Activity
            <select className="rounded-lg border border-gray-300 px-3 py-2 text-sm" value={formActivity} onChange={(e) => setFormActivity(e.target.value)}>
              <option value="">None</option>
              {activities.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
            Recognition
            <select className="rounded-lg border border-gray-300 px-3 py-2 text-sm" value={formInference} onChange={(e) => setFormInference(e.target.value as InferenceMode)}>
              <option value="local">Local Agent on this PC (Windows station — supports multiple cameras)</option>
              <option value="central">Central inference (phone / iPad / no agent — one camera)</option>
            </select>
          </label>
          {formInference === "central" && (
            <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
              Camera
              <select className="rounded-lg border border-gray-300 px-3 py-2 text-sm" value={formCameraLabel} onChange={(e) => setFormCameraLabel(e.target.value)}>
                <option value="">Let the browser choose</option>
                {cameras.map((c) => <option key={c.deviceId} value={c.label}>{c.label}</option>)}
              </select>
            </label>
          )}
          {formInference === "local" && (
            <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
              Local Agent port
              <Input value={formAgentPort} onChange={(e) => setFormAgentPort(e.target.value)} placeholder="8101" />
              <span className="text-xs text-gray-400 font-normal">
                Run it with: python node_agent.py --node-id {formNodeId || "CAM-02"} --port {formAgentPort || "8101"} --central {window.location.origin}
                <br />
                You will add your physical cameras (built-in, USB webcams…) one at a time on the next screen.
              </span>
            </label>
          )}
          <Button type="submit">Register this station</Button>
        </form>
      </Card>
    </div>
  );
}

// ===========================================================================
// MULTI-CAMERA STATION (local inference, this task) — one PC, several
// physical cameras, each its own independent recognition source.
// ===========================================================================

interface CameraInstanceInternal {
  deviceId: string;
  label: string;
  cameraId: string;
  stream: MediaStream | null;
  active: boolean;
  videoEl: HTMLVideoElement | null;
  heartbeatTimer: number | null;
  faceStaleTimer: number | null;
  // FLICKER FIX — see getVideoRefCallback(). This function is created ONCE
  // per camera and reused on every render, instead of the previous inline
  // arrow function that was recreated every render (see the audit in the
  // report). React treats a changed ref-prop identity as "detach the old
  // ref, attach the new one", which reassigns video.srcObject — and because
  // that inline function was rebuilt on every recognition tick, EVERY scan
  // for EVERY camera was reassigning srcObject on EVERY card, which is what
  // looked like continuous flicker/refresh. Caching it here means the ref
  // identity React sees is stable across re-renders unless this camera's
  // own instance is actually torn down.
  videoRefCallback: ((el: HTMLVideoElement | null) => void) | null;
  // Diagnostics only (§4) — real counters, not guesses, so the fix can be
  // verified rather than asserted. See the "Show diagnostics" toggle.
  getUserMediaCalls: number;
  srcObjectAssignments: number;
  // Recording (Task 1D §5-8) — one independent MediaRecorder per camera,
  // built from a CLONE of this camera's own stream. Never a second
  // getUserMedia call, and stopping it never touches `stream` above.
  recorder: MediaRecorder | null;
  recordingChunks: Blob[];
  recordingId: string | null;
  recordingStartedAt: number | null;
  recordingTimer: number | null;
  // Live Feed / detail-click handlers (Task 1E) — cached once per camera,
  // same idiom as videoRefCallback above. Passing these as props to
  // <CameraCardView> with a STABLE identity is what lets React.memo actually
  // bail out when only the Live Feed panel (a sibling, unrelated piece of
  // state) changes, instead of recreating every card's click handlers (and
  // therefore re-rendering every card) on every recognition tick.
  cardCallbacks: { onStartRecording: () => void; onStopRecording: () => void; onOpenDetail: () => void } | null;
}
interface CameraCard {
  cameraId: string; // stable identity — also the React key and the backend node_id (§18)
  deviceId: string;
  label: string;
  status: "starting" | "on" | "off" | "error";
  error: string;
  // null = no valid detection result yet this session (just started, or the
  // agent has never once answered) — deliberately distinct from 0, which
  // means "a real scan came back and genuinely found nobody" (§16: never
  // show 0 FACES when there is actually no valid result to show).
  faceCount: number | null;
  // The stale-timeout fired (§7): the last count is too old to trust and is
  // not shown — this is what stops a leftover "2 FACES" from sitting on
  // screen forever once the agent stops answering.
  detectionUnavailable: boolean;
  recording: boolean;
  recordingError: string;
  recordingElapsedSec: number;
}

// ---- Live Recognition Feed + Session History (Task 1E) --------------------
// Presentation-only, derived entirely from the SAME data.matches result
// scanOnce already fetches for attendance — never a second /recognize call.
// Central's attendance semantics are completely unaffected by any of this.

// One row in the LIVE feed panel — plain primitives/arrays only (never a Map)
// so it can be handed to <RecognitionPanel> as ordinary React state/props.
interface LiveFeedEntry {
  participantId: string;
  displayName: string;
  cameraIds: string[]; // cameras that currently (within the last 5s) see them
  firstSeenAt: number;
  lastSeenAt: number;
}
// One row in the SESSION history (global or per-camera) — unique by
// participantId, first-sighting timestamp only, never overwritten.
interface SessionEntry {
  participantId: string;
  displayName: string;
  firstSeenAt: number;
}
// Internal bookkeeping for the live feed only — never passed as a prop.
// The ROW's own visibility (>= 5s from ITS OWN last sighting, by ANY camera)
// is governed solely by rowExpireTimerId. Each camera's badge in cameraIds
// has its OWN independent fade-out timer in cameraTimers, so "CAM-01 ·
// CAM-03" can shrink to just "CAM-03" (a camera that stopped seeing this
// person drops out) without resetting or ending the row itself.
interface LiveFeedEntryInternal {
  participantId: string;
  displayName: string;
  cameraTimers: Map<string, number>;
  firstSeenAt: number;
  lastSeenAt: number;
  rowExpireTimerId: number | null;
}
// How long a Live Feed row (and each camera's badge within it) stays visible
// after its own last sighting. Reuses the SAME 5000ms constant already
// established for SingleCameraStation's flash entries below — one definition
// of "how long a recognition stays on screen" for this whole file.
const LIVE_FEED_MS = FLASH_MS;

// A stable, shared no-op — used only for the impossible case of a card
// rendering before its instance exists, so the ref prop is still never a
// freshly-allocated function even in that edge case.
const NOOP_REF = () => {};

type DetectionSessionState = "stopped" | "starting" | "active" | "error";

// One physical camera this browser knows about, whether currently plugged in
// or not. This is what survives navigation/refresh (§3/§7) — deviceId+label
// identify the hardware, cameraId is the STABLE recognition-source identity
// assigned to it once and never reused for a different device (§18), and
// enabled is the checkbox state. The MediaStream itself is never persisted —
// only ever recreated fresh from this record (§3).
interface SavedCameraSelection {
  deviceId: string;
  label: string;
  cameraId: string; // "" until first enabled — assigned lazily, see nextCameraId()
  enabled: boolean;
}

// How long a face count is trusted after its last successful scan before the
// card admits it might be stale (§7). 400ms scan interval means this is ~5
// scan attempts' worth of grace — enough to absorb an occasional 503 (agent
// busy) or one dropped frame without flickering, short enough that a camera
// whose detection has genuinely stopped is never shown as if it were current.
const FACE_STALE_MS = 2000;

// A blank-permission device always gets exactly this fallback label from
// cameras.ts's listCameras() — used here as the "has permission actually been
// granted yet" signal, since a real label never happens to look like this.
const BLANK_LABEL_RE = /^Camera \d+$/;

function selectionsStorageKey(stationNodeId: string): string {
  return `reconize_cctv_selection_${stationNodeId}`;
}
function loadSelections(stationNodeId: string): SavedCameraSelection[] {
  try {
    const raw = localStorage.getItem(selectionsStorageKey(stationNodeId));
    return raw ? (JSON.parse(raw) as SavedCameraSelection[]) : [];
  } catch {
    return [];
  }
}
function saveSelections(stationNodeId: string, selections: SavedCameraSelection[]): void {
  try {
    localStorage.setItem(selectionsStorageKey(stationNodeId), JSON.stringify(selections));
  } catch {
    /* private mode / storage disabled — the page still works this session, just without persistence */
  }
}

// The next stable camera_id for a NEWLY-enabled device at this station -
// never reassigns an id a device already has (§18), and never reuses a
// number already taken by another device, even one currently unavailable.
function nextCameraId(stationNodeId: string, existing: SavedCameraSelection[]): string {
  const prefix = `${stationNodeId}-`;
  let max = 0;
  for (const s of existing) {
    if (s.cameraId.startsWith(prefix)) {
      const n = parseInt(s.cameraId.slice(prefix.length), 10);
      if (!Number.isNaN(n)) max = Math.max(max, n);
    }
  }
  return `${prefix}${String(max + 1).padStart(2, "0")}`;
}

function MultiCameraStation({
  config, activityName, centralOnline, setCentralOnline, agentStatus, agentIndexInfo, agentError,
}: {
  config: NodeConfig;
  activityName: (id: string) => string;
  centralOnline: boolean; setCentralOnline: (v: boolean) => void;
  agentStatus: "unknown" | "ready" | "offline"; agentIndexInfo: string; agentError: string;
}) {
  const [selections, setSelections] = useState<SavedCameraSelection[]>(() => loadSelections(config.node_id));
  const [availableDeviceIds, setAvailableDeviceIds] = useState<Set<string>>(new Set());
  const [cards, setCards] = useState<CameraCard[]>([]);
  const [permissionGranted, setPermissionGranted] = useState(false);
  const [checkingPermission, setCheckingPermission] = useState(true);
  const [showDiagnostics, setShowDiagnostics] = useState(false);

  // ---- Activity-controlled detection (Task 1D §14-22) --------------------
  const [activities, setActivities] = useState<{ id: string; name: string }[]>([]);
  const [selectedActivityId, setSelectedActivityId] = useState("");
  const [detectionState, setDetectionState] = useState<DetectionSessionState>("stopped");
  const [detectionError, setDetectionError] = useState("");

  // ---- Live Recognition Feed + Session History (Task 1E) -----------------
  const [liveFeed, setLiveFeed] = useState<LiveFeedEntry[]>([]);
  const [sessionList, setSessionList] = useState<SessionEntry[]>([]);
  const [detailCameraId, setDetailCameraId] = useState<string | null>(null);
  // participantId -> row. Mutated directly, then mirrored into liveFeed state
  // via syncLiveFeedState() whenever it changes.
  const liveFeedMapRef = useRef<Map<string, LiveFeedEntryInternal>>(new Map());
  // participantId -> entry, global, unique, append-only for the whole
  // detection session — mirrored into sessionList state on first sighting.
  const sessionMapRef = useRef<Map<string, SessionEntry>>(new Map());
  // cameraId -> (participantId -> entry): this camera's OWN unique
  // recognized-participant history. Deliberately NOT React state — the
  // detail modal reads it directly at render time, and MultiCameraStation
  // already re-renders on every successful scan (updateFaceCount below
  // always calls setCards), so the modal sees fresh data for free with no
  // extra state and no extra re-renders of the camera grid.
  const perCameraSessionRef = useRef<Map<string, Map<string, SessionEntry>>>(new Map());

  // The mutable, per-camera internals — deliberately NOT React state, so a
  // recognition tick on CAM-02 does not cause CAM-01's card to re-render.
  // Each Camera Instance owns its own MediaStream, its own busy/active flag —
  // nothing here is shared between cameras (agentStatus/centralOnline are the
  // only genuinely shared facts: one Local Agent, one Central connection,
  // underneath all of them). Keyed by the STABLE cameraId, not a per-mount
  // counter, so the same physical camera always lands on the same entry.
  const instancesRef = useRef<Map<string, CameraInstanceInternal>>(new Map());
  const selectionsRef = useRef(selections);
  selectionsRef.current = selections;
  const agentStatusRef = useRef(agentStatus);
  agentStatusRef.current = agentStatus;
  // Read inside scanOnce/recording — a ref rather than closing over state,
  // since scanOnce runs from a long-lived while-loop, not a fresh render.
  const detectionStateRef = useRef<DetectionSessionState>("stopped");
  detectionStateRef.current = detectionState;
  const selectedActivityIdRef = useRef("");
  selectedActivityIdRef.current = selectedActivityId;

  useEffect(() => {
    apiGet("/api/activities").then((d) => setActivities(d.activities)).catch(() => {});
  }, []);

  // Tab close/refresh while a recording is in flight (§28) — real, native
  // warning. In-app SPA navigation (sidebar links) is NOT interactively
  // blocked here — see the report's known limitations — but any recording
  // still gets safely finalized on unmount (stopCameraForSelection below),
  // never corrupted or silently dropped.
  useEffect(() => {
    function onBeforeUnload(e: BeforeUnloadEvent) {
      const anyRecording = [...instancesRef.current.values()].some((i) => i.recorder && i.recorder.state !== "inactive");
      if (anyRecording) {
        e.preventDefault();
        e.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  // ---- Restore on mount (§9): load saved selection, enumerate devices,
  // reopen every enabled+available camera under its OWN remembered cameraId
  // — never a freshly-invented one (§18/§19). --------------------------------
  useEffect(() => {
    let cancelled = false;
    async function init() {
      const devices = await listCameras().catch(() => [] as CameraDevice[]);
      if (cancelled) return;
      const granted = devices.length > 0 && devices.some((d) => d.label && !BLANK_LABEL_RE.test(d.label));
      setPermissionGranted(granted);
      setCheckingPermission(false);
      mergeAndRestore(devices, granted);
    }
    init();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A camera reconnecting (or a new one appearing) mid-session should not
  // need a manual toggle to be noticed (§24) — a previously-enabled device
  // that becomes available again auto-restores exactly like on mount.
  useEffect(() => {
    function onDeviceChange() {
      listCameras().then((devices) => {
        const availableIds = new Set(devices.map((d) => d.deviceId));
        setAvailableDeviceIds(availableIds);
        for (const sel of selectionsRef.current) {
          if (sel.enabled && sel.cameraId && availableIds.has(sel.deviceId) && !instancesRef.current.has(sel.cameraId)) {
            startCameraForSelection(sel);
          }
        }
      }).catch(() => {});
    }
    navigator.mediaDevices?.addEventListener?.("devicechange", onDeviceChange);
    return () => navigator.mediaDevices?.removeEventListener?.("devicechange", onDeviceChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Page cleanup (§3/§20/§21): every remaining Camera Instance is fully torn
  // down — no webcam LED stays lit after this page goes — but the SAVED
  // selection (enabled/cameraId) is deliberately left untouched in
  // localStorage, so the next visit restores exactly what was running here.
  useEffect(() => {
    return () => {
      for (const cameraId of instancesRef.current.keys()) stopCameraForSelection(cameraId);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function updateCard(cameraId: string, patch: Partial<CameraCard>) {
    setCards((prev) => prev.map((c) => (c.cameraId === cameraId ? { ...c, ...patch } : c)));
  }

  function mergeAndRestore(devices: CameraDevice[], granted: boolean) {
    const availableIds = new Set(devices.map((d) => d.deviceId));
    setAvailableDeviceIds(availableIds);

    // Every previously-saved device is kept even if it is missing right now
    // (§8/§24 — UNAVAILABLE, not deleted); every currently-available device
    // never seen before joins the list, unchecked, with no cameraId yet.
    const byDeviceId = new Map(loadSelections(config.node_id).map((s) => [s.deviceId, s]));
    for (const d of devices) {
      const existing = byDeviceId.get(d.deviceId);
      if (!existing) {
        byDeviceId.set(d.deviceId, { deviceId: d.deviceId, label: d.label, cameraId: "", enabled: false });
      } else if (d.label && d.label !== existing.label && !BLANK_LABEL_RE.test(d.label)) {
        // A real label just became readable (permission granted this visit,
        // or the device changed) — remember it instead of the stale one.
        byDeviceId.set(d.deviceId, { ...existing, label: d.label });
      }
    }
    const merged = [...byDeviceId.values()];
    setSelections(merged);
    saveSelections(config.node_id, merged);

    if (!granted) return; // §10 — do not attempt to open anything until access is actually granted

    for (const sel of merged) {
      if (sel.enabled && sel.cameraId && availableIds.has(sel.deviceId)) {
        startCameraForSelection(sel);
      }
    }
  }

  async function handleEnableAccess() {
    try {
      const devices = await listCamerasWithPermission();
      setPermissionGranted(true);
      mergeAndRestore(devices, true);
    } catch {
      /* permission denied or no camera — the button simply stays for retry, cameraUnavailableReason() covers the messaging elsewhere */
    }
  }

  // ---- Checkbox toggle (§6) — replaces "+ Add Camera" one-at-a-time -------
  async function toggleCamera(deviceId: string) {
    const current = selectionsRef.current;
    const idx = current.findIndex((s) => s.deviceId === deviceId);
    if (idx === -1) return;
    const sel = current[idx];
    const turningOn = !sel.enabled;

    // §27 — unchecking a camera that is actively recording must not silently
    // destroy the file. Ask first; a cancel leaves the checkbox untouched.
    if (!turningOn && sel.cameraId) {
      const internal = instancesRef.current.get(sel.cameraId);
      if (internal?.recorder && internal.recorder.state !== "inactive") {
        const confirmed = window.confirm(
          `${sel.label} is recording.\nStop recording and disable this camera?`,
        );
        if (!confirmed) return;
        stopRecordingFor(sel.cameraId); // finalize (uploads the clip) before the stream is released below
      }
    }

    const updated: SavedCameraSelection = {
      ...sel,
      enabled: turningOn,
      // Assigned ONCE, on first-ever enable, then kept forever for this
      // deviceId (§18) — unchecking never clears it, so re-checking the same
      // physical camera later reuses the same recognition source identity
      // instead of registering a new one with Central (§19).
      cameraId: sel.cameraId || (turningOn ? nextCameraId(config.node_id, current) : sel.cameraId),
    };
    const next = [...current];
    next[idx] = updated;
    setSelections(next);
    saveSelections(config.node_id, next);

    if (turningOn) {
      await startCameraForSelection(updated);
    } else {
      stopCameraForSelection(updated.cameraId);
    }
  }

  // ---- Start (§9/§18: one getUserMedia call PER physical camera, under its
  // OWN stable cameraId) ----------------------------------------------------
  async function startCameraForSelection(sel: SavedCameraSelection) {
    if (!sel.cameraId) return;
    const cameraId = sel.cameraId;

    if (!instancesRef.current.has(cameraId)) {
      instancesRef.current.set(cameraId, {
        deviceId: sel.deviceId, label: sel.label, cameraId, stream: null, active: false,
        videoEl: null, heartbeatTimer: null, faceStaleTimer: null, videoRefCallback: null,
        getUserMediaCalls: 0, srcObjectAssignments: 0,
        recorder: null, recordingChunks: [], recordingId: null, recordingStartedAt: null, recordingTimer: null,
        cardCallbacks: null,
      });
    }
    const internal = instancesRef.current.get(cameraId)!;

    // A reopened stream starts fresh detection state (§20) — never a
    // leftover count from whatever this camera showed before it was stopped.
    setCards((prev) =>
      prev.some((c) => c.cameraId === cameraId)
        ? prev.map((c) => (c.cameraId === cameraId ? { ...c, status: "starting", error: "", faceCount: null, detectionUnavailable: false } : c))
        : [...prev, {
            cameraId, deviceId: sel.deviceId, label: sel.label, status: "starting", error: "",
            faceCount: null, detectionUnavailable: false, recording: false, recordingError: "", recordingElapsedSec: 0,
          }],
    );

    const blocked = cameraUnavailableReason();
    if (blocked) {
      updateCard(cameraId, { status: "error", error: blocked });
      return;
    }
    try {
      // One getUserMedia call per physical camera, period — nothing in this
      // task's recording or activity-gating additions calls this again.
      // Counted so the flicker fix is verifiable rather than asserted (§4).
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { deviceId: { exact: sel.deviceId }, ...CAMERA_CONSTRAINTS },
      });
      internal.stream = stream;
      internal.getUserMediaCalls++;
      console.debug(`[CCTV] ${cameraId} getUserMedia call #${internal.getUserMediaCalls}`);
      if (internal.videoEl && internal.videoEl.srcObject !== stream) {
        internal.videoEl.srcObject = stream;
        internal.srcObjectAssignments++;
      }
      listCameras().then((d) => setAvailableDeviceIds(new Set(d.map((x) => x.deviceId)))).catch(() => {});

      stream.getVideoTracks()[0]?.addEventListener("ended", () => {
        internal.active = false;
        updateCard(cameraId, { status: "error", error: "The camera stopped unexpectedly (unplugged, or access revoked)." });
      });
    } catch (err) {
      updateCard(cameraId, { status: "error", error: describeCameraError(err) });
      return;
    }

    // Registers this ONE physical camera as its own recognition source with
    // Central. The backend upserts by node_id (api/nodes.py's register_node),
    // so calling this again for the SAME cameraId — every remount, every
    // reconnect — updates the one existing row rather than creating another;
    // duplicate registration is prevented by that existing backend behavior,
    // not by anything new here (§19, verified in the automated tests below).
    try {
      await apiPostJson("/api/node/register", {
        node_id: cameraId, display_name: `${config.display_name} · ${sel.label}`,
        activity_id: config.activity_id || null, camera_label: sel.label,
        mode: "always", inference_mode: "local",
      });
    } catch {
      /* registration is best-effort — local recognition still runs even if Central is briefly unreachable */
    }

    internal.active = true;
    updateCard(cameraId, { status: "on", error: "" });
    runHeartbeat(internal);
    armFaceStaleTimer(cameraId, internal);
    runScanLoop(cameraId, internal);
  }

  // ---- Stop (§6/§18) — unchecking is the only "remove": this camera only,
  // others keep running, and the saved selection's enabled flag (already
  // updated in toggleCamera) is what actually persists the decision. -------
  function stopCameraForSelection(cameraId: string) {
    const internal = instancesRef.current.get(cameraId);
    if (!internal) return;
    internal.active = false;
    // If this camera's own detail modal happened to be open, close it rather
    // than leaving it pointed at an id that no longer has a card — otherwise
    // it would silently reappear if this same cameraId got re-enabled later.
    setDetailCameraId((prev) => (prev === cameraId ? null : prev));
    if (internal.heartbeatTimer) window.clearInterval(internal.heartbeatTimer);
    internal.heartbeatTimer = null;
    if (internal.faceStaleTimer) window.clearTimeout(internal.faceStaleTimer);
    internal.faceStaleTimer = null;
    // §28 — a recording in progress is finalized (never abandoned mid-buffer)
    // whenever this camera stops, including page unmount: recorder.stop()
    // flushes the last chunk and its onstop handler uploads it, which still
    // completes normally even after this component has unmounted (the fetch
    // keeps running; only the tab closing would cut it off, which is what
    // the beforeunload warning above is for).
    if (internal.recorder && internal.recorder.state !== "inactive") {
      internal.recorder.stop();
    }
    internal.recorder = null;
    if (internal.stream) {
      internal.stream.getTracks().forEach((t) => t.stop());
      internal.stream = null;
    }
    if (internal.videoEl) internal.videoEl.srcObject = null;
    instancesRef.current.delete(cameraId);
    setCards((prev) => prev.filter((c) => c.cameraId !== cameraId));
  }

  // ---- Heartbeat — one per camera, tagged with THAT camera's cameraId -----
  function runHeartbeat(internal: CameraInstanceInternal) {
    const tick = () => {
      if (!internal.active) return;
      apiPostJson("/api/node/heartbeat", {
        node_id: internal.cameraId, recording: false, local_agent_ok: agentStatusRef.current === "ready",
      })
        .then(() => setCentralOnline(true))
        .catch(() => setCentralOnline(false));
    };
    tick();
    internal.heartbeatTimer = window.setInterval(tick, HEARTBEAT_MS);
  }

  // ---- Independent recognition loop per camera (§10 of the previous task) -
  // Each camera's while-loop only ever awaits ITS OWN fetch calls — there is
  // no shared busyRef anywhere, so one camera waiting on a slow recognize()
  // response can never stall another camera's next tick.
  async function runScanLoop(cameraId: string, internal: CameraInstanceInternal) {
    while (internal.active) {
      await scanOnce(cameraId, internal);
      if (!internal.active) return;
      await new Promise((r) => setTimeout(r, SCAN_DELAY_MS));
    }
  }

  async function scanOnce(cameraId: string, internal: CameraInstanceInternal) {
    // §16/§18/§19 — the camera stays live and the scan loop keeps ticking
    // regardless of the detection session; this is what makes a tick into a
    // genuine no-op (no capture, no agent call, no Central call) rather than
    // "recognition secretly still running" while the UI claims DETECTION OFF.
    if (detectionStateRef.current !== "active") return;
    if (agentStatusRef.current !== "ready") return; // never silently fall back to Central
    const video = internal.videoEl;
    if (!video || video.videoWidth === 0) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    const blob: Blob | null = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
    if (!blob || !internal.active) return;

    let data: { faces_total: number; matches: { participant_id: string; name: string; confidence: number }[] };
    try {
      const form = new FormData();
      form.append("photo", blob, "scan.jpg");
      form.append("camera_id", internal.cameraId);
      const res = await fetch(`http://127.0.0.1:${config.agent_port}/recognize`, { method: "POST", body: form });
      if (res.status === 503) return; // agent's own bounded queue is full — this camera just tries again next tick; face count stays at its last value until the stale timer decides otherwise
      if (!res.ok) throw new Error(String(res.status));
      data = await res.json();
    } catch {
      // A transient failure on THIS camera's request does not flip the
      // shared agentStatus badge — the dedicated health poll owns that, so
      // one flaky frame can never make the other cameras look offline too.
      // The face count is likewise left untouched here (not zeroed) — only
      // the stale timer below is allowed to decide a count can no longer be
      // trusted.
      return;
    }
    if (!internal.active) return;

    // Face count reflects EVERY detected face — known participant or not.
    // Updated on every successful scan, whether or not anyone was actually
    // recognized, and BEFORE the recognition/attendance step below so the
    // card's headline number never waits on Central.
    updateFaceCount(cameraId, internal, data.faces_total);

    const recognized = data.matches;
    // Task 1E — reuses THIS SAME recognition result for the Live Feed /
    // Session History; no second /recognize call is ever made for the panel.
    // No-ops internally when recognized is empty.
    recordRecognition(cameraId, recognized);
    if (recognized.length === 0) return; // nothing recognized — the card already has its face count; nothing to report to Central this tick

    // Recognition/attendance keeps running exactly as before — it is simply
    // no longer reflected on the card itself (no names, no bounding boxes).
    // activity_id here is the CCTV detection session's own selected Activity
    // (§15/§22) — deliberately NOT config.activity_id, which is only this
    // station's original setup-time metadata used for register/heartbeat.
    try {
      const body = JSON.stringify({
        node_id: internal.cameraId, camera_label: internal.label,
        activity_id: selectedActivityIdRef.current || null, matches: recognized,
      });
      const form = new FormData();
      form.append("body", body);
      form.append("image", blob, "event.jpg");
      await apiPostForm("/api/node/event", form);
    } catch {
      setCentralOnline(false);
    }
  }

  // ---- Face count + stale-timeout --------------------------------------
  function updateFaceCount(cameraId: string, internal: CameraInstanceInternal, count: number) {
    updateCard(cameraId, { faceCount: count, detectionUnavailable: false });
    armFaceStaleTimer(cameraId, internal);
  }
  function armFaceStaleTimer(cameraId: string, internal: CameraInstanceInternal) {
    if (internal.faceStaleTimer) window.clearTimeout(internal.faceStaleTimer);
    internal.faceStaleTimer = window.setTimeout(() => {
      updateCard(cameraId, { detectionUnavailable: true });
    }, FACE_STALE_MS);
  }

  // ---- Live Recognition Feed + Session History (Task 1E) -----------------
  // Unknown/unrecognized faces never reach here — data.matches already
  // contains ONLY recognized participants (see node_agent.py / nodes.py);
  // the raw face_total (which includes unknowns) only ever feeds the card's
  // "N FACES DETECTED" count above, never this panel.
  function recordRecognition(cameraId: string, matches: { participant_id: string; name: string; confidence: number }[]) {
    if (matches.length === 0) return;
    const now = Date.now();
    let camSession = perCameraSessionRef.current.get(cameraId);
    if (!camSession) {
      camSession = new Map();
      perCameraSessionRef.current.set(cameraId, camSession);
    }
    let sessionChanged = false;

    for (const m of matches) {
      const pid = m.participant_id;

      // Per-camera session (§f) — unique per camera, first-seen kept, never overwritten.
      if (!camSession.has(pid)) {
        camSession.set(pid, { participantId: pid, displayName: m.name, firstSeenAt: now });
      }

      // Global session (§e) — unique for the whole detection session, add once.
      if (!sessionMapRef.current.has(pid)) {
        sessionMapRef.current.set(pid, { participantId: pid, displayName: m.name, firstSeenAt: now });
        sessionChanged = true;
      }

      // Live feed (§b/§c) — refresh (never duplicate) this participant's row,
      // rearming ITS OWN 5s visibility timer from THIS sighting, and rearm
      // just this camera's own badge timer within the row.
      let entry = liveFeedMapRef.current.get(pid);
      if (!entry) {
        entry = { participantId: pid, displayName: m.name, cameraTimers: new Map(), firstSeenAt: now, lastSeenAt: now, rowExpireTimerId: null };
        liveFeedMapRef.current.set(pid, entry);
      }
      entry.displayName = m.name;
      entry.lastSeenAt = now;
      if (entry.rowExpireTimerId) window.clearTimeout(entry.rowExpireTimerId);
      entry.rowExpireTimerId = window.setTimeout(() => expireLiveFeedRow(pid), LIVE_FEED_MS);
      const oldCamTimer = entry.cameraTimers.get(cameraId);
      if (oldCamTimer) window.clearTimeout(oldCamTimer);
      entry.cameraTimers.set(cameraId, window.setTimeout(() => dropCameraFromRow(pid, cameraId), LIVE_FEED_MS));
    }

    syncLiveFeedState();
    if (sessionChanged) setSessionList([...sessionMapRef.current.values()].sort((a, b) => a.firstSeenAt - b.firstSeenAt));
  }

  // A row's own minimum-lifetime timer fired: it has not been re-sighted by
  // ANY camera for LIVE_FEED_MS — remove the whole row (never before then,
  // regardless of how many entries are already in the panel — no hard cap).
  function expireLiveFeedRow(participantId: string) {
    const entry = liveFeedMapRef.current.get(participantId);
    if (!entry) return;
    for (const t of entry.cameraTimers.values()) window.clearTimeout(t);
    liveFeedMapRef.current.delete(participantId);
    syncLiveFeedState();
  }
  // One camera's own badge timer fired: that camera has not re-sighted this
  // participant recently — drop just that camera from the displayed list.
  // The row itself is untouched; only expireLiveFeedRow ever removes it.
  function dropCameraFromRow(participantId: string, cameraId: string) {
    const entry = liveFeedMapRef.current.get(participantId);
    if (!entry) return;
    entry.cameraTimers.delete(cameraId);
    syncLiveFeedState();
  }
  function syncLiveFeedState() {
    setLiveFeed(
      [...liveFeedMapRef.current.values()]
        .map((e) => ({
          participantId: e.participantId, displayName: e.displayName,
          cameraIds: [...e.cameraTimers.keys()].sort(),
          firstSeenAt: e.firstSeenAt, lastSeenAt: e.lastSeenAt,
        }))
        .sort((a, b) => b.lastSeenAt - a.lastSeenAt),
    );
  }
  // START DETECTION resets Live Feed + global session + every per-camera
  // session to empty (§g) — clearing all outstanding timers first so no
  // stale timeout from the previous session can touch the new one's map.
  function resetRecognitionPanels() {
    for (const entry of liveFeedMapRef.current.values()) {
      if (entry.rowExpireTimerId) window.clearTimeout(entry.rowExpireTimerId);
      for (const t of entry.cameraTimers.values()) window.clearTimeout(t);
    }
    liveFeedMapRef.current.clear();
    sessionMapRef.current.clear();
    perCameraSessionRef.current.clear();
    setLiveFeed([]);
    setSessionList([]);
  }

  // ---- FLICKER FIX — one stable ref-callback function PER camera, cached
  // on the instance and reused across every render (see the report's root-
  // cause analysis on CameraInstanceInternal.videoRefCallback above). -------
  function getVideoRefCallback(cameraId: string): (el: HTMLVideoElement | null) => void {
    const internal = instancesRef.current.get(cameraId);
    if (!internal) return NOOP_REF;
    if (!internal.videoRefCallback) {
      internal.videoRefCallback = (el) => {
        const inst = instancesRef.current.get(cameraId);
        if (!inst) return;
        inst.videoEl = el;
        // Only touch srcObject when it is genuinely a different stream —
        // this is the second half of the fix: even if this callback WERE
        // invoked again (e.g. after a real reconnect), a same-object
        // reassignment is skipped rather than forcing the video element to
        // reset its decode pipeline for no reason.
        if (el && inst.stream && el.srcObject !== inst.stream) {
          el.srcObject = inst.stream;
          inst.srcObjectAssignments++;
          console.debug(`[CCTV] ${cameraId} srcObject assignment #${inst.srcObjectAssignments}`);
        }
      };
    }
    return internal.videoRefCallback;
  }

  // ---- Recording (§5-9) — reuses the camera's OWN stream via .clone(),
  // never a second getUserMedia call. Independent per camera: starting or
  // stopping CAM-01's recorder never touches CAM-02's. -------------------
  function startRecordingFor(cameraId: string) {
    const internal = instancesRef.current.get(cameraId);
    if (!internal || !internal.stream) return;
    updateCard(cameraId, { recordingError: "" });

    const recordStream = internal.stream.clone();
    const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp9") ? "video/webm;codecs=vp9" : "video/webm";
    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(recordStream, { mimeType });
    } catch (err) {
      updateCard(cameraId, { recordingError: err instanceof Error ? err.message : "This browser cannot record video." });
      recordStream.getTracks().forEach((t) => t.stop());
      return;
    }
    internal.recordingChunks = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) internal.recordingChunks.push(e.data);
    };
    recorder.onerror = () => {
      // §13 — one recorder failing must not touch this camera's live preview
      // or detection, and must not affect any OTHER camera at all.
      updateCard(cameraId, { recordingError: "Recording failed. Live preview and detection are unaffected.", recording: false });
    };
    recorder.onstop = async () => {
      recordStream.getTracks().forEach((t) => t.stop()); // releases the CLONE only — the live camera stream is untouched (§8)
      const blob = new Blob(internal.recordingChunks, { type: mimeType });
      const recordingId = internal.recordingId;
      internal.recordingId = null;
      if (internal.recordingTimer) {
        window.clearInterval(internal.recordingTimer);
        internal.recordingTimer = null;
      }
      if (!recordingId || blob.size === 0) return;
      try {
        const form = new FormData();
        form.append("recording_id", recordingId);
        form.append("clip", blob, "clip.webm");
        await apiPostForm("/api/node/recording/stop", form);
      } catch (err) {
        updateCard(cameraId, { recordingError: err instanceof Error ? err.message : "Could not upload the finished recording." });
      }
    };

    apiPostJson("/api/node/recording/start", {
      // §25 — whatever Activity is currently selected in the CCTV view,
      // recording or not; never fabricated, left null when none is chosen.
      node_id: cameraId, node_name: internal.label, activity_id: selectedActivityIdRef.current || null,
    })
      .then((r) => {
        internal.recordingId = r.recording_id;
        internal.recorder = recorder;
        internal.recordingStartedAt = Date.now();
        recorder.start(1000); // 1s timeslices — a crash still leaves most of the clip
        updateCard(cameraId, { recording: true, recordingElapsedSec: 0 });
        internal.recordingTimer = window.setInterval(() => {
          updateCard(cameraId, {
            recordingElapsedSec: Math.floor((Date.now() - (internal.recordingStartedAt ?? Date.now())) / 1000),
          });
        }, 1000);
      })
      .catch((err) => {
        updateCard(cameraId, { recordingError: err instanceof Error ? err.message : "Could not start recording." });
        recordStream.getTracks().forEach((t) => t.stop());
      });
  }

  function stopRecordingFor(cameraId: string) {
    const internal = instancesRef.current.get(cameraId);
    if (!internal || !internal.recorder) return;
    if (internal.recorder.state !== "inactive") internal.recorder.stop();
    internal.recorder = null;
    updateCard(cameraId, { recording: false });
  }

  function recordAll() {
    for (const card of cards) {
      if (card.status === "on" && !card.recording) startRecordingFor(card.cameraId);
    }
  }
  function stopAllRecordings() {
    for (const card of cards) {
      if (card.recording) stopRecordingFor(card.cameraId);
    }
  }

  // Cached once per camera (same idiom as getVideoRefCallback) so these
  // three callbacks keep a STABLE identity across renders — required for
  // React.memo(CameraCardView) to actually skip re-rendering a camera whose
  // own card data hasn't changed, even though the Live Feed panel (a sibling
  // piece of state) re-renders this whole component on every recognition.
  function getCardCallbacks(cameraId: string): { onStartRecording: () => void; onStopRecording: () => void; onOpenDetail: () => void } {
    const internal = instancesRef.current.get(cameraId);
    if (!internal) return { onStartRecording: NOOP_REF, onStopRecording: NOOP_REF, onOpenDetail: NOOP_REF };
    if (!internal.cardCallbacks) {
      internal.cardCallbacks = {
        onStartRecording: () => startRecordingFor(cameraId),
        onStopRecording: () => stopRecordingFor(cameraId),
        onOpenDetail: () => setDetailCameraId(cameraId),
      };
    }
    return internal.cardCallbacks;
  }

  // ---- Activity-controlled detection session (§14-22) ---------------------
  function startDetection() {
    if (!selectedActivityId) {
      setDetectionError("SELECT AN ACTIVITY FIRST");
      return;
    }
    if (!cards.some((c) => c.status === "on")) {
      setDetectionError("Enable at least one camera first.");
      return;
    }
    if (agentStatus !== "ready") {
      setDetectionError(agentError || "Local Recognition Agent is not ready.");
      return;
    }
    setDetectionError("");
    // A fresh session starts with fresh results (§20) — never a leftover
    // count from before detection was stopped.
    setCards((prev) => prev.map((c) => ({ ...c, faceCount: null, detectionUnavailable: false })));
    // Task 1E §g — START DETECTION also resets Live Feed + global session +
    // every per-camera session to empty.
    resetRecognitionPanels();
    setDetectionState("active");
  }
  function stopDetection() {
    // §19 — this only stops the AI scan loops (via detectionStateRef, read
    // inside scanOnce). Camera streams and any active recordings are
    // completely untouched here. Task 1E §g — Live Feed / Session History are
    // likewise untouched: scanOnce's own top-of-function guard means no new
    // entries are added once stopped, but existing Live Feed rows keep
    // ticking down their own already-armed timers, and all session history
    // stays exactly as it was for viewing.
    setDetectionState("stopped");
  }

  const detailCard = detailCameraId ? cards.find((c) => c.cameraId === detailCameraId) ?? null : null;
  const detailSession = detailCameraId
    ? [...(perCameraSessionRef.current.get(detailCameraId)?.values() ?? [])].sort((a, b) => a.firstSeenAt - b.firstSeenAt)
    : [];

  return (
    <div className="max-w-[1600px] mx-auto">
      <PageHeader
        title="CCTV View"
        subtitle={`${config.display_name} · ${config.node_id}${config.activity_id ? " · " + activityName(config.activity_id) : ""}`}
        action={
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={centralOnline ? "good" : "bad"}>{centralOnline ? "Central connected" : "CENTRAL SERVER DISCONNECTED"}</Badge>
            <Badge tone={agentStatus === "ready" ? "good" : agentStatus === "offline" ? "bad" : "default"}>
              {agentStatus === "ready" ? "LOCAL RECOGNITION READY" : agentStatus === "offline" ? "LOCAL RECOGNITION AGENT OFFLINE" : "Checking agent…"}
            </Badge>
            {agentError && (
              <div className="text-xs text-gray-500 font-mono whitespace-pre-line max-w-xs text-right">
                {agentError}
              </div>
            )}
          </div>
        }
      />

      {!checkingPermission && !permissionGranted && (
        <Card className="mb-4 border-amber-200 bg-amber-50">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="text-sm text-amber-900">Camera access has not been granted in this browser yet.</div>
            <Button onClick={handleEnableAccess}>Enable Camera Access</Button>
          </div>
        </Card>
      )}

      {/* Activity-controlled detection session (§14-22) — one Activity for
          the whole CCTV view, locked while a session is active so events
          can never be silently split across two activities mid-session. */}
      <Card className="mb-4">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm font-medium text-gray-700">
            Activity
            <select
              className="rounded-lg border border-gray-300 px-2 py-1.5 text-sm"
              value={selectedActivityId}
              disabled={detectionState === "active"}
              onChange={(e) => setSelectedActivityId(e.target.value)}
            >
              <option value="">Choose an activity…</option>
              {activities.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </label>
          <Badge tone={detectionState === "active" ? "good" : detectionState === "error" ? "bad" : "default"}>
            Detection: {detectionState === "active" ? "ACTIVE" : detectionState === "error" ? "ERROR" : "STOPPED"}
          </Badge>
          {detectionState === "active" ? (
            <Button variant="danger" onClick={stopDetection}>Stop Detection</Button>
          ) : (
            <Button onClick={startDetection}>Start Detection</Button>
          )}
          <label className="flex items-center gap-1.5 text-xs text-gray-400 ml-auto">
            <input type="checkbox" checked={showDiagnostics} onChange={(e) => setShowDiagnostics(e.target.checked)} />
            Show diagnostics
          </label>
        </div>
        {detectionError && <div className="text-sm text-red-600 font-medium mt-2">{detectionError}</div>}
      </Card>

      {/* Camera selection checklist (§4/§5) — replaces the old one-at-a-time
          "+ Add Camera" flow. Every checkbox change is immediate: no Apply
          button, no separate Stop/Remove controls (§6). */}
      <Card className="mb-4">
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs font-semibold text-gray-500 uppercase">Cameras</div>
          {cards.some((c) => c.status === "on") && (
            <div className="flex gap-2">
              <Button variant="secondary" onClick={recordAll}>Record All</Button>
              <Button variant="secondary" onClick={stopAllRecordings}>Stop All Recordings</Button>
            </div>
          )}
        </div>
        {selections.length === 0 ? (
          <div className="text-sm text-gray-400">No cameras detected on this PC yet.</div>
        ) : (
          <div className="flex flex-wrap gap-x-6 gap-y-1.5">
            {selections.map((sel) => {
              const isAvailable = availableDeviceIds.has(sel.deviceId);
              return (
                <label key={sel.deviceId} className={`flex items-center gap-2 text-sm ${isAvailable ? "text-gray-800" : "text-gray-400"}`}>
                  <input
                    type="checkbox"
                    checked={sel.enabled}
                    disabled={!isAvailable}
                    onChange={() => toggleCamera(sel.deviceId)}
                  />
                  <span>{sel.label}</span>
                  {!isAvailable && <span className="text-xs text-red-500 font-semibold">UNAVAILABLE</span>}
                </label>
              );
            })}
          </div>
        )}
        {agentIndexInfo && <div className="text-xs text-gray-400 mt-2">{agentIndexInfo}</div>}
      </Card>

      {/* Task 1E §a — camera wall (~72%) + Recognition panel (~28%), side by
          side so the panel stays visible beside the wall without scrolling
          below it, designed/tested against 7 cameras. Stacks on narrow
          viewports since there is nowhere to put a side column there. */}
      <div className="flex flex-col lg:flex-row gap-4 items-start">
        <div className="min-w-0 lg:flex-[72]">
          {cards.length === 0 ? (
            <Card className="text-sm text-gray-400 text-center py-16">
              No cameras selected. Tick one above to start the live view.
            </Card>
          ) : (
            <div className={`grid gap-4 ${gridColsClass(cards.length)}`}>
              {cards.map((card) => {
                const internal = instancesRef.current.get(card.cameraId);
                const cb = getCardCallbacks(card.cameraId);
                return (
                  <CameraCardView
                    key={card.cameraId}
                    card={card}
                    detectionActive={detectionState === "active"}
                    diagnostics={showDiagnostics ? {
                      getUserMediaCalls: internal?.getUserMediaCalls ?? 0,
                      srcObjectAssignments: internal?.srcObjectAssignments ?? 0,
                    } : null}
                    onVideoRef={getVideoRefCallback(card.cameraId)}
                    onStartRecording={cb.onStartRecording}
                    onStopRecording={cb.onStopRecording}
                    onOpenDetail={cb.onOpenDetail}
                  />
                );
              })}
            </div>
          )}
        </div>
        <div className="w-full lg:flex-[28] lg:sticky lg:top-4">
          <RecognitionPanel liveFeed={liveFeed} sessionList={sessionList} />
        </div>
      </div>

      {detailCard && (
        <CameraDetailModal
          card={detailCard}
          session={detailSession}
          activityLabel={selectedActivityId ? activityName(selectedActivityId) : ""}
          onClose={() => setDetailCameraId(null)}
        />
      )}
    </div>
  );
}

// Responsive layout (§13): a single camera gets one large view instead of
// being stranded in an empty grid cell; 2 sit side by side where there is
// room; 3+ settle into a 2-wide wall (2x2, 2x3...) rather than shrinking
// every tile to fit an arbitrary fixed column count. Live video stays the
// visual priority at every camera count (§35).
function gridColsClass(count: number): string {
  if (count <= 1) return "grid-cols-1 max-w-3xl mx-auto";
  if (count <= 4) return "grid-cols-1 md:grid-cols-2";
  return "grid-cols-1 md:grid-cols-2 xl:grid-cols-3";
}

function formatDuration(totalSec: number): string {
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  return [h, m, s].map((v) => String(v).padStart(2, "0")).join(":");
}

// §20 — DETECTION OFF overrides everything else: a camera's own faceCount
// might still hold a real number from before the session was stopped, but
// that number must never be shown as if it were current once the session
// itself is off, regardless of that camera's own state.
function faceCountLabel(card: CameraCard, detectionActive: boolean): string {
  if (card.status !== "on") return "—";
  if (!detectionActive) return "DETECTION OFF";
  if (card.detectionUnavailable) return "DETECTION UNAVAILABLE";
  if (card.faceCount === null) return "Waiting for first scan…";
  if (card.faceCount === 0) return "0 FACES";
  if (card.faceCount === 1) return "1 FACE DETECTED";
  return `${card.faceCount} FACES DETECTED`;
}

// Task 1E anti-flicker safety layer: memo'd on top of the existing ref-
// stability fix (getVideoRefCallback / getCardCallbacks above). All of this
// component's function props are now cached per-camera with a stable
// identity, and `card` itself keeps the SAME object reference across
// renders unless THIS camera's own entry changed (see updateCard's .map —
// untouched entries are never replaced). That means React.memo's default
// shallow-prop comparison genuinely bails out for a camera whose data did
// not change, even though the Live Feed panel — a sibling, unrelated piece
// of state — re-renders the parent on every recognition tick. Verified by
// the "re-render count" diagnostic added to the automated test / manual
// check, not merely assumed.
const CameraCardView = memo(function CameraCardView({
  card, detectionActive, diagnostics, onVideoRef, onStartRecording, onStopRecording, onOpenDetail,
}: {
  card: CameraCard;
  detectionActive: boolean;
  diagnostics: { getUserMediaCalls: number; srcObjectAssignments: number } | null;
  onVideoRef: (el: HTMLVideoElement | null) => void;
  onStartRecording: () => void;
  onStopRecording: () => void;
  onOpenDetail: () => void;
}) {
  const hasFaces = card.status === "on" && detectionActive && !card.detectionUnavailable && (card.faceCount ?? 0) > 0;
  return (
    <Card className="p-0 overflow-hidden flex flex-col">
      {/* Click anywhere on the header/video to open this camera's session
          detail (§f) — deliberately NOT on the whole Card, so it can never
          be confused with the Start/Stop Recording button below, and never
          starts or stops the camera itself (that is the checklist's job
          only, §6). */}
      <div className="cursor-pointer" onClick={onOpenDetail} title="View this camera's session history">
        <div className="px-3 py-2 flex items-center justify-between border-b border-gray-100">
          <div>
            <div className="font-semibold text-gray-900 text-sm">{card.cameraId}</div>
            <div className="text-xs text-gray-400">{card.label}</div>
          </div>
          <Badge tone={card.recording ? "bad" : card.status === "on" ? "good" : card.status === "error" ? "bad" : "default"}>
            {card.recording
              ? `● REC ${formatDuration(card.recordingElapsedSec)}`
              : card.status === "on" ? "● LIVE" : card.status === "starting" ? "STARTING" : card.status === "error" ? "ERROR" : "STOPPED"}
          </Badge>
        </div>

        {/* Live video is the visual priority (§35) — large, unobstructed, never
            stretched (object-contain preserves the camera's own aspect ratio,
            §14). This is a direct getUserMedia MediaStream with no server
            round trip (§22): the browser's own camera feed, nothing else.
            onVideoRef is a STABLE per-camera function (see getVideoRefCallback)
            — this is the flicker fix: its identity does not change on every
            face-count/detection re-render the way an inline arrow function's
            would have. */}
        <div className="relative bg-black aspect-video flex items-center justify-center">
          <video ref={onVideoRef} autoPlay playsInline muted className="w-full h-full object-contain" style={{ transform: "scaleX(-1)" }} />
          {card.status !== "on" && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/70 text-white text-xs text-center px-3">
              {card.status === "starting" ? "Requesting camera permission…" : card.error || "Camera stopped"}
            </div>
          )}
        </div>
      </div>

      {/* No Stop/Remove buttons here — the camera checklist above is the only
          control for LIVE/OFF now (§6). Recording is the one per-card
          control this task adds (§9). */}
      <div className="p-3 flex flex-col gap-2">
        <div className={`flex items-center gap-2 text-base font-bold ${
          !detectionActive ? "text-gray-400" : card.detectionUnavailable ? "text-amber-600" : hasFaces ? "text-green-700" : "text-gray-500"
        }`}>
          <span className="text-xl leading-none">👤</span>
          <span>{faceCountLabel(card, detectionActive)}</span>
        </div>
        {card.status === "on" && (
          card.recording ? (
            <Button variant="danger" onClick={onStopRecording}>■ Stop Recording</Button>
          ) : (
            <Button onClick={onStartRecording}>● Start Recording</Button>
          )
        )}
        {card.recordingError && <div className="text-xs text-red-600">{card.recordingError}</div>}
        {diagnostics && (
          <div className="text-xs text-gray-400 font-mono">
            getUserMedia: {diagnostics.getUserMediaCalls} · srcObject writes: {diagnostics.srcObjectAssignments}
          </div>
        )}
      </div>
    </Card>
  );
});

// Task 1E §b/§c — the RECOGNIZED PEOPLE panel: a LIVE tab (deduplicated by
// participant_id, each row showing which camera(s) currently see them, no
// panel-wide clearing, no hard cap — see recordRecognition/syncLiveFeedState
// in MultiCameraStation) and a SESSION tab (global, unique, append-only for
// the whole detection session — see resetRecognitionPanels for the one
// place either is ever cleared, tied to START DETECTION).
function RecognitionPanel({ liveFeed, sessionList }: { liveFeed: LiveFeedEntry[]; sessionList: SessionEntry[] }) {
  const [tab, setTab] = useState<"live" | "session">("live");
  const tabClass = (active: boolean) =>
    `flex-1 px-3 py-2 text-xs font-semibold uppercase tracking-wide border-b-2 ${
      active ? "border-indigo-600 text-indigo-600" : "border-transparent text-gray-400 hover:text-gray-600"
    }`;
  return (
    <Card className="p-0 overflow-hidden flex flex-col">
      <div className="flex border-b border-gray-100">
        <button className={tabClass(tab === "live")} onClick={() => setTab("live")}>Live ({liveFeed.length})</button>
        <button className={tabClass(tab === "session")} onClick={() => setTab("session")}>Session ({sessionList.length})</button>
      </div>
      <div className="p-3 overflow-y-auto" style={{ maxHeight: "75vh" }}>
        {tab === "live" ? (
          liveFeed.length === 0 ? (
            <div className="text-sm text-gray-400 text-center py-10">No one recognized yet.</div>
          ) : (
            <ul className="flex flex-col gap-2">
              {liveFeed.map((e) => (
                <li key={e.participantId} className="rounded-lg bg-green-50 border border-green-100 px-3 py-2">
                  <div className="font-medium text-gray-900 text-sm truncate">{e.displayName}</div>
                  <div className="text-xs text-green-700 font-mono mt-0.5">{e.cameraIds.length ? e.cameraIds.join(" · ") : "—"}</div>
                </li>
              ))}
            </ul>
          )
        ) : sessionList.length === 0 ? (
          <div className="text-sm text-gray-400 text-center py-10">No one recognized this session yet.</div>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {sessionList.map((s) => (
              <li key={s.participantId} className="flex items-center justify-between text-sm py-1 border-b border-gray-50 last:border-0">
                <span className="text-gray-800 truncate">{s.displayName}</span>
                <span className="text-xs text-gray-400 shrink-0 ml-2">{new Date(s.firstSeenAt).toLocaleTimeString()}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

// Task 1E §f — one camera's own detail: current face count, recording/live
// status, the selected Activity, and that camera's OWN unique recognized-
// participant history (never the global session, never other cameras').
function CameraDetailModal({
  card, session, activityLabel, onClose,
}: {
  card: CameraCard;
  session: SessionEntry[];
  activityLabel: string;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-white rounded-xl max-w-md w-full max-h-[80vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
          <div>
            <div className="font-semibold text-gray-900">{card.cameraId}</div>
            <div className="text-xs text-gray-400">{card.label}</div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none px-1">&times;</button>
        </div>
        <div className="px-4 py-3 border-b border-gray-100 flex flex-wrap gap-2">
          <Badge tone={card.status === "on" ? "good" : "bad"}>{card.status === "on" ? "LIVE" : card.status.toUpperCase()}</Badge>
          <Badge tone={card.recording ? "bad" : "default"}>{card.recording ? `● REC ${formatDuration(card.recordingElapsedSec)}` : "Not recording"}</Badge>
          <Badge tone="default">{activityLabel || "No activity selected"}</Badge>
          <Badge tone="default">{card.faceCount ?? 0} face(s) now</Badge>
        </div>
        <div className="px-4 pt-3 pb-1 text-xs font-semibold text-gray-500 uppercase">Recognized this session ({session.length})</div>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          {session.length === 0 ? (
            <div className="text-sm text-gray-400 py-6 text-center">No one recognized by this camera yet.</div>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {session.map((s) => (
                <li key={s.participantId} className="flex items-center justify-between text-sm py-1 border-b border-gray-50 last:border-0">
                  <span className="text-gray-800 truncate">{s.displayName}</span>
                  <span className="text-xs text-gray-400 shrink-0 ml-2">{new Date(s.firstSeenAt).toLocaleTimeString()}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

// ===========================================================================
// SINGLE-CAMERA STATION (central inference — phone/iPad, unchanged) —
// exactly the original design: one getUserMedia call feeds preview,
// recording, the scan loop and CCTV publish. Out of scope for this task;
// kept verbatim so the mobile deep link (?inference=central) keeps working.
// ===========================================================================

function SingleCameraStation({
  config, activityName, centralOnline, setCentralOnline,
}: {
  config: NodeConfig;
  activityName: (id: string) => string;
  centralOnline: boolean; setCentralOnline: (v: boolean) => void;
}) {
  const [cameraState, setCameraState] = useState<"off" | "starting" | "on">("off");
  const [cameraError, setCameraError] = useState("");
  const [recording, setRecording] = useState(false);
  const [recordError, setRecordError] = useState("");
  const [cctvStatus, setCctvStatus] = useState<"idle" | "publishing" | "error">("idle");
  const [flash, setFlash] = useState<FlashEntry[]>([]);
  const [facesLastSeen, setFacesLastSeen] = useState(0);

  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const activeRef = useRef(false);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const recordingChunksRef = useRef<Blob[]>([]);
  const recordingIdRef = useRef<string | null>(null);
  const flashSeq = useRef(0);
  const flashTimersRef = useRef<Map<string, FlashTimer>>(new Map());
  const heartbeatTimerRef = useRef<number | null>(null);
  const signalWsRef = useRef<WebSocket | null>(null);
  const viewerPeersRef = useRef<Map<string, RTCPeerConnection>>(new Map());
  const myClientIdRef = useRef(Math.random().toString(36).slice(2));

  useEffect(() => {
    return () => {
      stopEverything();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function startCamera() {
    setCameraError("");
    const blocked = cameraUnavailableReason();
    if (blocked) {
      setCameraError(blocked);
      return;
    }
    setCameraState("starting");
    try {
      const available = await listCameras();
      const match = config.camera_label ? available.find((c) => c.label === config.camera_label) : null;
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { ...videoConstraints(match?.deviceId ?? null), ...CAMERA_CONSTRAINTS },
      });
      streamRef.current = stream;
      if (videoRef.current) videoRef.current.srcObject = stream;

      stream.getVideoTracks()[0]?.addEventListener("ended", () => {
        setCameraError("The camera stopped unexpectedly (unplugged, or access revoked). Press Start Camera to reopen it.");
        setCameraState("off");
        activeRef.current = false;
      });
    } catch (err) {
      setCameraError(describeCameraError(err));
      setCameraState("off");
      return;
    }

    setCameraState("on");
    activeRef.current = true;
    startHeartbeat();
    startScanLoop();
    startCctvPublish();
  }

  function stopEverything() {
    activeRef.current = false;
    setCameraState("off");
    if (heartbeatTimerRef.current) window.clearInterval(heartbeatTimerRef.current);
    for (const t of flashTimersRef.current.values()) {
      window.clearTimeout(t.expireId);
      window.clearTimeout(t.removeId);
    }
    flashTimersRef.current.clear();
    setFlash([]);
    stopCctvPublish();
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      recorderRef.current.stop();
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    if (videoRef.current) videoRef.current.srcObject = null;
  }

  function startHeartbeat() {
    const tick = () => {
      apiPostJson("/api/node/heartbeat", {
        node_id: config.node_id, recording, faces_seen: facesLastSeen,
      })
        .then(() => setCentralOnline(true))
        .catch(() => setCentralOnline(false));
    };
    tick();
    heartbeatTimerRef.current = window.setInterval(tick, HEARTBEAT_MS);
  }

  function captureBlob(): Promise<Blob | null> {
    const video = videoRef.current;
    if (!video || video.videoWidth === 0) return Promise.resolve(null);
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
  }

  async function startScanLoop() {
    while (activeRef.current) {
      await scanOnce();
      if (!activeRef.current) return;
      await new Promise((r) => setTimeout(r, SCAN_DELAY_MS));
    }
  }

  async function scanOnce() {
    const blob = await captureBlob();
    if (!blob) return;
    try {
      const form = new FormData();
      form.append("photo", blob, "scan.jpg");
      form.append("node_id", config.node_id);
      if (config.activity_id) form.append("activity_id", config.activity_id);
      const data = await apiPostForm("/api/node/recognize-central", form);
      setCentralOnline(true);
      setFacesLastSeen(data.faces_total);
      const people: PersonResult[] = (data.results ?? [])
        .filter((r: { status: string }) => r.status === "matched")
        .map((r: { person_id: string; participant_id: string; name?: string; first_name?: string; checkin_status: string }) => ({
          person_id: r.person_id, participant_id: r.participant_id,
          name: r.name || r.first_name || "", checkin: r.checkin_status === "already_checked_in" ? "already" : "new",
        }));
      if (people.length) announce(people);
    } catch {
      setCentralOnline(false);
    }
  }

  function announce(people: PersonResult[]) {
    const timers = flashTimersRef.current;
    for (const p of people) {
      const existing = timers.get(p.person_id);
      if (existing) {
        window.clearTimeout(existing.expireId);
        window.clearTimeout(existing.removeId);
        const { expireId, removeId } = scheduleExpiry(p.person_id, existing.key);
        timers.set(p.person_id, { key: existing.key, expireId, removeId });
        setFlash((prev) => prev.map((f) => (f.person_id === p.person_id ? { ...p, key: f.key, leaving: false } : f)));
        continue;
      }
      if (timers.size >= FLASH_MAX) {
        const oldestId = timers.keys().next().value;
        if (oldestId !== undefined) {
          const oldest = timers.get(oldestId)!;
          window.clearTimeout(oldest.expireId);
          window.clearTimeout(oldest.removeId);
          timers.delete(oldestId);
          setFlash((prev) => prev.filter((f) => f.person_id !== oldestId));
        }
      }
      const key = ++flashSeq.current;
      const { expireId, removeId } = scheduleExpiry(p.person_id, key);
      timers.set(p.person_id, { key, expireId, removeId });
      setFlash((prev) => [...prev, { ...p, key, leaving: false }]);
    }
  }
  function scheduleExpiry(personId: string, key: number) {
    const expireId = window.setTimeout(
      () => setFlash((prev) => prev.map((f) => (f.key === key ? { ...f, leaving: true } : f))),
      FLASH_MS,
    );
    const removeId = window.setTimeout(() => {
      flashTimersRef.current.delete(personId);
      setFlash((prev) => prev.filter((f) => f.key !== key));
    }, FLASH_MS + FLASH_FADE_MS);
    return { expireId, removeId };
  }

  function startRecording() {
    if (!streamRef.current) return;
    setRecordError("");
    const recordStream = streamRef.current.clone();
    const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
      ? "video/webm;codecs=vp9"
      : "video/webm";
    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(recordStream, { mimeType });
    } catch (err) {
      setRecordError(err instanceof Error ? err.message : "This browser cannot record video.");
      recordStream.getTracks().forEach((t) => t.stop());
      return;
    }
    recordingChunksRef.current = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) recordingChunksRef.current.push(e.data);
    };
    recorder.onerror = () => {
      setRecordError("Recording failed. The camera preview and recognition are unaffected.");
      setRecording(false);
    };
    recorder.onstop = async () => {
      recordStream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(recordingChunksRef.current, { type: mimeType });
      const recordingId = recordingIdRef.current;
      recordingIdRef.current = null;
      if (!recordingId || blob.size === 0) return;
      try {
        const form = new FormData();
        form.append("recording_id", recordingId);
        form.append("clip", blob, "clip.webm");
        await apiPostForm("/api/node/recording/stop", form);
      } catch (err) {
        setRecordError(err instanceof Error ? err.message : "Could not upload the finished recording.");
      }
    };

    apiPostJson("/api/node/recording/start", {
      node_id: config.node_id, node_name: config.display_name, activity_id: config.activity_id || null,
    })
      .then((r) => {
        recordingIdRef.current = r.recording_id;
        recorderRef.current = recorder;
        recorder.start(1000);
        setRecording(true);
      })
      .catch((err) => {
        setRecordError(err instanceof Error ? err.message : "Could not start recording.");
        recordStream.getTracks().forEach((t) => t.stop());
      });
  }

  function stopRecording() {
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      recorderRef.current.stop();
    }
    setRecording(false);
  }

  function startCctvPublish() {
    const token = localStorage.getItem("token") ?? "";
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${wsBase()}/api/node/signal?room=${encodeURIComponent(config.node_id)}&role=publisher&token=${encodeURIComponent(token)}`);
    } catch {
      setCctvStatus("error");
      return;
    }
    signalWsRef.current = ws;
    ws.onopen = () => setCctvStatus("publishing");
    ws.onerror = () => setCctvStatus("error");
    ws.onclose = () => setCctvStatus("idle");
    ws.onmessage = async (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.to && msg.to !== myClientIdRef.current) return;
      if (msg.type === "join") {
        await answerNewViewer(msg.from);
      } else if (msg.type === "answer") {
        const pc = viewerPeersRef.current.get(msg.from);
        if (pc) await pc.setRemoteDescription(msg.payload);
      } else if (msg.type === "ice") {
        const pc = viewerPeersRef.current.get(msg.from);
        if (pc && msg.payload) await pc.addIceCandidate(msg.payload).catch(() => {});
      } else if (msg.type === "bye") {
        viewerPeersRef.current.get(msg.from)?.close();
        viewerPeersRef.current.delete(msg.from);
      }
    };
  }

  async function answerNewViewer(viewerId: string) {
    if (!streamRef.current) return;
    const pc = new RTCPeerConnection({ iceServers: [] });
    viewerPeersRef.current.set(viewerId, pc);
    for (const track of streamRef.current.getVideoTracks()) {
      const sender = pc.addTrack(track, streamRef.current);
      try {
        const params = sender.getParameters();
        params.encodings = params.encodings?.length ? params.encodings : [{}];
        params.encodings[0].maxBitrate = 1_500_000;
        params.degradationPreference = "maintain-framerate";
        await sender.setParameters(params);
      } catch {
        /* best-effort */
      }
    }
    pc.onicecandidate = (e) => {
      if (e.candidate) signalSend({ type: "ice", from: myClientIdRef.current, to: viewerId, payload: e.candidate });
    };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    signalSend({ type: "offer", from: myClientIdRef.current, to: viewerId, payload: offer });
  }

  function signalSend(msg: unknown) {
    if (signalWsRef.current?.readyState === WebSocket.OPEN) signalWsRef.current.send(JSON.stringify(msg));
  }

  function stopCctvPublish() {
    signalWsRef.current?.close();
    signalWsRef.current = null;
    for (const pc of viewerPeersRef.current.values()) pc.close();
    viewerPeersRef.current.clear();
    setCctvStatus("idle");
  }

  return (
    <div className="max-w-5xl mx-auto">
      <PageHeader
        title={config.display_name}
        subtitle={`${config.node_id}${config.activity_id ? " · " + activityName(config.activity_id) : ""}`}
        action={
          <div className="flex gap-2">
            <Badge tone={centralOnline ? "good" : "bad"}>{centralOnline ? "Central connected" : "CENTRAL SERVER DISCONNECTED"}</Badge>
            <Badge tone="good">CENTRAL INFERENCE</Badge>
          </div>
        }
      />

      <div className="grid lg:grid-cols-[1fr_320px] gap-4">
        <Card className="relative overflow-hidden bg-black aspect-video flex items-center justify-center p-0">
          <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-contain" style={{ transform: "scaleX(-1)" }} />
          {cameraState === "off" && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/80 text-white p-6 text-center">
              <div className="text-lg font-semibold">CAMERA OFF</div>
              {cameraError && <div className="text-sm text-red-300 max-w-md">{cameraError}</div>}
              <Button onClick={startCamera}>Start Camera</Button>
            </div>
          )}
          {cameraState === "starting" && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/80 text-white">Requesting camera permission…</div>
          )}
          {cameraState === "on" && (
            <>
              <div className="absolute top-3 left-3 flex gap-2">
                {recording && <Badge tone="bad">● REC</Badge>}
                <Badge tone={cctvStatus === "publishing" ? "good" : "default"}>
                  {cctvStatus === "publishing" ? "CCTV live" : cctvStatus === "error" ? "STREAM UNAVAILABLE" : "CCTV connecting…"}
                </Badge>
              </div>
              <div className="absolute bottom-3 left-3 flex gap-2">
                {!recording ? (
                  <Button onClick={startRecording}>Start Recording</Button>
                ) : (
                  <Button variant="danger" onClick={stopRecording}>Stop Recording</Button>
                )}
                <Button variant="secondary" onClick={stopEverything}>Stop Camera</Button>
              </div>
            </>
          )}
        </Card>

        <div className="flex flex-col gap-3">
          <Card>
            <div className="text-xs font-semibold text-gray-500 uppercase mb-2">Recent Detections</div>
            {flash.length === 0 && <div className="text-sm text-gray-400">Nobody recognised yet.</div>}
            <div className="flex flex-col gap-2">
              {flash.map((p) => (
                <div
                  key={p.key}
                  className={
                    "px-3 py-2 rounded-xl text-sm font-semibold transition-all duration-300 " +
                    (p.leaving ? "opacity-0 translate-x-2 " : "opacity-100 ") +
                    (p.checkin === "new" ? "bg-green-100 text-green-800" : "bg-blue-100 text-blue-800")
                  }
                >
                  {p.name}
                </div>
              ))}
            </div>
          </Card>
          {recordError && <Card className="border-red-200 bg-red-50 text-sm text-red-700">{recordError}</Card>}
        </div>
      </div>
    </div>
  );
};
