import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { apiGet, apiPostForm, apiPostJson, ApiError } from "../api/client";
import {
  listCameras,
  getKioskOverride,
  setKioskOverride,
  resolveCameraDeviceId,
  videoConstraints,
  type CameraDevice,
} from "../api/cameras";

interface RecognitionResult {
  bbox: number[]; // [x1, y1, x2, y2] — not displayed anywhere in this UI, kept for /api/uploads parity
  confidence: number;
  status: "matched" | "unknown";
  person_id?: string;
  first_name?: string;
  name?: string;
  participant_id?: string;
  checkin_status?: "new" | "already_checked_in";
}

interface ServerTimings {
  upload_read: number;
  image_decode: number;
  image_resize: number;
  face_detection: number;
  embedding: number;
  comparison: number;
  database_read: number;
  database_write: number;
  image_save: number;
  total: number;
}

interface ServerSystem {
  inference_device: string;
  model_name: string;
  registered_faces_indexed: number;
}

interface RecognitionResponse {
  faces_total: number;
  faces_matched: number;
  faces_unknown: number;
  processing_duration_ms: number;
  threshold_used: number;
  results: RecognitionResult[];
  timings_ms?: ServerTimings;
  system?: ServerSystem;
}

interface ClientTimings {
  frontend_capture: number;
  network_and_overhead: number;
  frontend_render: number;
  round_trip_total: number;
}

interface SystemInfo {
  cpu: string;
  cpu_cores_logical: number;
  cpu_cores_physical: number;
  cpu_usage_percent: number;
  ram_total_gb: number;
  ram_available_gb: number;
  ram_usage_percent: number;
  gpu: string;
  python_version: string;
  insightface_version: string;
  onnxruntime_version: string;
  model_name: string;
  inference_device: string;
  registered_faces_indexed: number;
}

type KioskState = "idle" | "consent" | "scanning" | "result";
type KioskMode = "tap" | "always";

const MODE_KEY = "reconize_kiosk_mode";
const FLASH_MS = 2200;          // how long a recognised name stays on screen
const REFLASH_SUPPRESS_MS = 8000; // don't re-announce the same person for this long

function getKioskModeOverride(): KioskMode | null {
  try {
    const v = localStorage.getItem(MODE_KEY);
    return v === "tap" || v === "always" ? v : null;
  } catch {
    return null;
  }
}

function setKioskModeOverride(mode: KioskMode | null): void {
  try {
    if (mode) localStorage.setItem(MODE_KEY, mode);
    else localStorage.removeItem(MODE_KEY);
  } catch {
    /* storage disabled - fall back to the app-wide setting */
  }
}
type ConsentChoice = "consent" | "decline";
interface PersonResult {
  person_id: string;
  name: string;
  checkin: "new" | "already";
}

// Self-paced scan loop instead of a fixed setInterval: each scan is awaited
// to completion, then we wait SCAN_DELAY_MS before starting the next one.
const SCAN_DELAY_MS = 200;
// How long the result screen stays up before auto-returning to IDLE.
const RESULT_SCREEN_MS = 1800;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default function Recognition() {
  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [cameras, setCameras] = useState<CameraDevice[]>([]);
  const [cameraLabelSetting, setCameraLabelSetting] = useState("");
  const [cameraOverride, setCameraOverrideState] = useState<string | null>(getKioskOverride());
  // "tap" = TAP TO SCAN + PDPA prompt (original). "always" = camera runs
  // continuously, no prompt, people are checked in as they are seen.
  const [kioskMode, setKioskMode] = useState<KioskMode>(
    (new URLSearchParams(window.location.search).get("mode") as KioskMode | null) ??
      getKioskModeOverride() ??
      "tap",
  );
  const [modeFromSettings, setModeFromSettings] = useState<KioskMode>("tap");
  const [modeOverridden, setModeOverridden] = useState(getKioskModeOverride() !== null);
  const [activityName, setActivityName] = useState("");
  // A "station" is this window pinned to one activity and one camera by its
  // URL: /recognition?activity=<id>&camera=<deviceId>&mode=<tap|always>.
  // Several windows can therefore run at once against different activities,
  // or against the SAME activity with different cameras. URL wins over the
  // per-machine override, which wins over the app-wide Settings default -
  // and because it lives in the URL rather than localStorage, two windows on
  // the same machine cannot overwrite each other's choice.
  const [searchParams] = useSearchParams();
  const pinnedActivity = searchParams.get("activity") ?? "";
  const pinnedCamera = searchParams.get("camera") ?? "";
  const pinnedModeParam = searchParams.get("mode");
  const pinnedMode: KioskMode | null =
    pinnedModeParam === "tap" || pinnedModeParam === "always" ? pinnedModeParam : null;
  const isStation = Boolean(pinnedActivity || pinnedCamera || pinnedMode);
  // Transient "recognised" banner in always-on mode; the loop keeps running.
  const [flash, setFlash] = useState<PersonResult[] | null>(null);
  const flashTimeoutRef = useRef<number | null>(null);
  // person_id -> timestamp, so somebody standing in frame is not announced on
  // every single tick. Purely cosmetic; the backend still decides check-in.
  const recentlyFlashedRef = useRef<Map<string, number>>(new Map());
  const streamRef = useRef<MediaStream | null>(null);
  const activeRef = useRef(false); // true while the scan loop should keep running
  const consentRef = useRef<ConsentChoice | null>(null); // session-scoped only — no backend field for this
  const resultTimeoutRef = useRef<number | null>(null);

  const [kioskState, setKioskState] = useState<KioskState>("idle");
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [result, setResult] = useState<RecognitionResponse | null>(null);
  const [resultPeople, setResultPeople] = useState<PersonResult[] | null>(null);
  const [clientTimings, setClientTimings] = useState<ClientTimings | null>(null);
  const [error, setError] = useState("");
  const [debugMode, setDebugMode] = useState(false);
  const [systemInfo, setSystemInfo] = useState<SystemInfo | null>(null);

  useEffect(() => {
    apiGet("/api/settings").then((s) => {
      setDebugMode(s.debug_mode === "true");
      setCameraLabelSetting(s.camera_label ?? "");
      const fromSettings: KioskMode = s.kiosk_mode === "always" ? "always" : "tap";
      setModeFromSettings(fromSettings);
      // A pinned mode is fixed; otherwise a local override wins; otherwise
      // follow the app-wide setting.
      if (!pinnedMode && getKioskModeOverride() === null) setKioskMode(fromSettings);
    });
    apiGet("/api/activities")
      .then((d) => {
        const wanted = pinnedActivity || d.current_activity_id;
        const match = d.activities.find((a: { id: string }) => a.id === wanted);
        setActivityName(match ? match.name : "");
        if (pinnedActivity && !match) {
          setError("This station is pinned to an activity that no longer exists. Reopen it from the Activities page.");
        }
      })
      .catch(() => {});
    // Populate the switcher up front when the browser already has permission
    // from an earlier visit; without it the list is unlabelled until scanning
    // starts. Never prompts on its own.
    listCameras().then(setCameras).catch(() => {});
  }, []);

  useEffect(() => {
    if (debugMode && !systemInfo) {
      apiGet("/api/system/info").then(setSystemInfo).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debugMode]);

  useEffect(() => {
    function onFsChange() {
      setIsFullscreen(document.fullscreenElement === containerRef.current);
    }
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  useEffect(() => {
    return () => {
      stopCamera(); // stop the camera if the user navigates away
      if (resultTimeoutRef.current) window.clearTimeout(resultTimeoutRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function tapToScan() {
    setError("");
    if (kioskMode === "always") {
      // No prompt in this mode - consent comes from the registration form,
      // recorded against the participant at import time.
      startScanning();
      return;
    }
    setKioskState("consent");
  }

  async function selectConsent(choice: ConsentChoice) {
    consentRef.current = choice; // recorded for this session only — both choices proceed to scanning
    await startScanning();
  }

  // In always-on mode the kiosk is meant to need no interaction at all, so
  // the camera starts by itself once we know that is the active mode. Guarded
  // on kioskState so a running scan is never restarted underneath itself.
  useEffect(() => {
    if (kioskMode === "always" && kioskState === "idle" && !activeRef.current) {
      startScanning();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kioskMode, kioskState]);

  useEffect(() => {
    return () => {
      if (flashTimeoutRef.current) window.clearTimeout(flashTimeoutRef.current);
    };
  }, []);

  function chooseMode(mode: KioskMode | null) {
    setKioskModeOverride(mode);
    setModeOverridden(mode !== null);
    const effective = mode ?? modeFromSettings;
    setKioskMode(effective);
    // Leaving always-on must actually release the camera, not just repaint.
    if (effective === "tap") {
      activeRef.current = false;
      stopCamera();
      setFlash(null);
      setKioskState("idle");
    }
  }

  function chooseCamera(deviceId: string) {
    // "" means fall back to the app-wide default from Settings.
    setKioskOverride(deviceId || null);
    setCameraOverrideState(deviceId || null);
  }

  async function startScanning() {
    setError("");
    try {
      // Which camera: this kiosk's local override, else the app-wide default
      // from Settings (matched by label), else whatever the browser picks.
      const available = await listCameras();
      // A pinned camera is used as-is. It is NOT silently swapped for another
      // if it has been unplugged: two stations sharing one machine would
      // otherwise quietly collapse onto the same camera.
      const deviceId = pinnedCamera || resolveCameraDeviceId(available, cameraLabelSetting);
      const stream = await navigator.mediaDevices.getUserMedia({
        video: videoConstraints(deviceId),
      });
      // Labels are only readable once permission is granted, so refresh the
      // list now that the stream is open - this is what fills the switcher.
      listCameras().then(setCameras).catch(() => {});
      streamRef.current = stream;
      if (videoRef.current) videoRef.current.srcObject = stream;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not access the camera.");
      consentRef.current = null;
      setKioskState("idle");
      return;
    }
    activeRef.current = true;
    setKioskState("scanning");
    runScanLoop();
  }

  // Self-paced loop: scan, wait a small fixed delay, scan again. Keeps going
  // while nobody known is recognized (empty frame or unknown-only faces
  // don't stop it) — stops the instant a scan tick finds at least one known
  // participant, releases the camera, and shows the result.
  async function runScanLoop() {
    while (activeRef.current) {
      const people = await scanFrame();
      if (people.length > 0) {
        if (kioskMode === "always") {
          // Stay on the camera: announce whoever is newly recognised and keep
          // scanning, so a queue can walk past without anyone touching it.
          announce(people);
        } else {
          activeRef.current = false;
          stopCamera();
          recordConsent(people.map((p) => p.person_id)); // fire-and-forget — never blocks showing the result
          showResult(people);
          return;
        }
      }
      if (!activeRef.current) return;
      await sleep(SCAN_DELAY_MS);
    }
  }

  // Show a recognised person briefly without interrupting the loop. Somebody
  // lingering in frame is only announced once per REFLASH_SUPPRESS_MS.
  function announce(people: PersonResult[]) {
    const now = Date.now();
    const fresh = people.filter((p) => {
      const last = recentlyFlashedRef.current.get(p.person_id);
      return last === undefined || now - last > REFLASH_SUPPRESS_MS;
    });
    if (fresh.length === 0) return;
    for (const p of fresh) recentlyFlashedRef.current.set(p.person_id, now);

    setFlash(fresh);
    if (flashTimeoutRef.current) window.clearTimeout(flashTimeoutRef.current);
    flashTimeoutRef.current = window.setTimeout(() => setFlash(null), FLASH_MS);
  }

  // PDPA: the consent choice picked before this scan session started is
  // recorded against whichever known participant(s) just got recognized —
  // consent is only ever tied to a known participant, never logged
  // anonymously. A failure here must never disrupt the check-in flow itself.
  function recordConsent(personIds: string[]) {
    if (!consentRef.current || personIds.length === 0) return;
    apiPostJson("/api/pdpa/record", {
      person_ids: personIds,
      choice: consentRef.current === "consent" ? "consented" : "declined",
    }).catch(() => {});
  }

  function showResult(people: PersonResult[]) {
    setResultPeople(people);
    setKioskState("result");
    resultTimeoutRef.current = window.setTimeout(() => {
      setResultPeople(null);
      setResult(null);
      setClientTimings(null);
      consentRef.current = null;
      setKioskState("idle");
    }, RESULT_SCREEN_MS);
  }

  function stopCamera() {
    activeRef.current = false;
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    if (videoRef.current) videoRef.current.srcObject = null;
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      containerRef.current?.requestFullscreen();
    }
  }

  function captureBlob(): Promise<Blob | null> {
    const video = videoRef.current;
    if (!video) return Promise.resolve(null);
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
  }

  // Runs one recognize call and reports which known participants (if any)
  // were found in this frame — the loop above decides whether that means
  // "stop and show the result" or "keep scanning."
  async function scanFrame(): Promise<PersonResult[]> {
    try {
      const tCaptureStart = performance.now();
      const blob = await captureBlob();
      const tCaptureEnd = performance.now();
      if (!blob) return [];

      const form = new FormData();
      form.append("photo", blob, "scan.jpg");
      // The backend can no longer infer this: with stations running side by
      // side there is no single "current" activity to read server-side.
      if (pinnedActivity) form.append("activity_id", pinnedActivity);

      const tFetchStart = performance.now();
      const data: RecognitionResponse = await apiPostForm(`/api/recognition/upload${debugMode ? "?debug=true" : ""}`, form);
      const tFetchEnd = performance.now();

      setResult(data);
      setError("");

      if (debugMode) {
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        const tRenderDone = performance.now();
        const roundTrip = tFetchEnd - tFetchStart;
        setClientTimings({
          frontend_capture: tCaptureEnd - tCaptureStart,
          network_and_overhead: Math.max(roundTrip - (data.processing_duration_ms ?? 0), 0),
          frontend_render: tRenderDone - tFetchEnd,
          round_trip_total: roundTrip,
        });
      }

      return data.results
        .filter((r): r is RecognitionResult & { person_id: string } => r.status === "matched" && !!r.person_id)
        .map((r) => ({
          person_id: r.person_id,
          name: (r.first_name || r.name || "").toUpperCase(),
          checkin: (r.checkin_status === "already_checked_in" ? "already" : "new") as "new" | "already",
        }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Recognition request failed.");
      return [];
    }
  }

  return (
    <div ref={containerRef} className="relative h-full w-full bg-black overflow-hidden">
      {/* Mirrored like a mirror/selfie view so a participant standing at the
          kiosk sees themselves move the way they expect. Display only: the
          frame sent for recognition is drawn from the video element itself
          (captureFrame -> drawImage), which reads the source bitmap and is
          unaffected by this CSS transform, so the stored event image and the
          embedding stay true-to-life. The overlays are siblings of the video,
          not children, so none of their text is mirrored. */}
      <video
        ref={videoRef}
        autoPlay
        playsInline
        muted
        className="w-full h-full object-contain"
        style={{ transform: "scaleX(-1)" }}
      />

      {/* IDLE — camera off, no recognition API calls. */}
      {kioskState === "idle" && (
        <div className="absolute inset-0 bg-black flex flex-col items-center justify-center gap-8">
          {isStation && (
            <div className="absolute top-6 left-6 text-xs font-medium text-white/60 bg-white/10 px-3 py-1.5 rounded-full backdrop-blur">
              STATION{activityName ? ` · ${activityName}` : ""}
            </div>
          )}
          <div className="text-white text-5xl sm:text-6xl font-extrabold tracking-wide">EVENT CHECK-IN</div>
          {activityName && (
            <div className="text-indigo-300 text-2xl font-semibold -mt-4">{activityName}</div>
          )}
          {kioskMode === "always" ? (
            <div className="text-white/60 text-xl">Starting camera...</div>
          ) : (
            <button
              onClick={tapToScan}
              className="px-14 py-7 rounded-2xl bg-indigo-600 hover:bg-indigo-700 text-white text-3xl sm:text-4xl font-bold shadow-2xl"
            >
              TAP TO SCAN
            </button>
          )}
          {/* Mode switch for THIS machine. The app-wide default is in Settings.
              Hidden on a pinned station: its mode comes from the URL, and a
              dropdown that silently disagreed with it would be a trap. */}
          <div className={`absolute top-6 left-1/2 -translate-x-1/2 items-center gap-2 ${isStation ? "hidden" : "flex"}`}>
            <span className="text-white/40 text-xs">Mode</span>
            <select
              value={modeOverridden ? kioskMode : ""}
              onChange={(e) => chooseMode((e.target.value || null) as KioskMode | null)}
              className="rounded-lg bg-white/10 text-white text-sm px-3 py-2 backdrop-blur border border-white/20"
            >
              <option value="" className="text-black">
                Default ({modeFromSettings === "always" ? "Always on" : "Tap to scan"})
              </option>
              <option value="tap" className="text-black">Tap to scan (ask consent)</option>
              <option value="always" className="text-black">Always on (no prompt)</option>
            </select>
          </div>
          {/* Local override for this machine only. The app-wide default lives
              in Settings; this is for swapping camera at the kiosk itself. Only
              shown when there is actually more than one camera to choose. */}
          {cameras.length > 1 && !isStation && (
            <div className="absolute bottom-6 left-1/2 -translate-x-1/2 flex items-center gap-2">
              <span className="text-white/50 text-xs">Camera</span>
              <select
                value={cameraOverride ?? ""}
                onChange={(e) => chooseCamera(e.target.value)}
                className="rounded-lg bg-white/10 text-white text-sm px-3 py-2 backdrop-blur border border-white/20"
              >
                <option value="" className="text-black">
                  {cameraLabelSetting ? `Default (${cameraLabelSetting})` : "Default"}
                </option>
                {cameras.map((c) => (
                  <option key={c.deviceId} value={c.deviceId} className="text-black">
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
          )}
          <button
            onClick={toggleFullscreen}
            className="absolute bottom-6 right-6 px-4 py-2 rounded-lg bg-white/10 hover:bg-white/20 text-white text-sm font-medium backdrop-blur"
          >
            {isFullscreen ? "Exit Full Screen" : "⛶ Full Screen"}
          </button>
          <Link
            to="/"
            className="absolute bottom-6 left-6 text-xs font-medium text-white/50 hover:text-white/90 bg-black/40 px-2.5 py-1 rounded-full"
          >
            ← Dashboard
          </Link>
        </div>
      )}

      {/* ALWAYS-ON: the camera stays live. A recognised person is announced
          for a couple of seconds over the video while scanning continues. */}
      {kioskMode === "always" && kioskState === "scanning" && (
        <>
          <div className="absolute top-6 left-1/2 -translate-x-1/2 flex flex-col items-center gap-1 pointer-events-none">
            <div className="text-white/70 text-sm bg-black/40 rounded-full px-4 py-1 backdrop-blur">
              {activityName ? `Checking in to ${activityName}` : "Check-in running"}
            </div>
          </div>
          {flash && (
            <div className="absolute bottom-10 left-1/2 -translate-x-1/2 flex flex-col items-center gap-2 pointer-events-none">
              {flash.map((p) => (
                <div
                  key={p.person_id}
                  className={`px-8 py-4 rounded-2xl text-white text-3xl font-bold shadow-2xl backdrop-blur ${
                    p.checkin === "new" ? "bg-green-600/90" : "bg-blue-600/90"
                  }`}
                >
                  {p.checkin === "new" ? "✓ " : "• "}
                  {p.name}
                  <span className="block text-base font-medium opacity-80">
                    {p.checkin === "new"
                      ? activityName
                        ? `Checked in to ${activityName}`
                        : "Checked in"
                      : "Already checked in"}
                  </span>
                </div>
              ))}
            </div>
          )}
          <button
            onClick={() => chooseMode("tap")}
            className="absolute bottom-6 right-6 px-4 py-2 rounded-lg bg-white/10 hover:bg-white/20 text-white text-sm font-medium backdrop-blur"
          >
            Stop always-on
          </button>
        </>
      )}

      {/* CONSENT — still camera off. Either choice proceeds to scanning; the
          choice is only recorded for this session (consentRef). */}
      {kioskState === "consent" && (
        <div className="absolute inset-0 bg-black flex flex-col items-center justify-center gap-10 px-8 complete-backdrop">
          <div className="text-white text-2xl sm:text-3xl font-semibold text-center max-w-2xl">
            Do you consent to the use of your personal/face data for this event?
          </div>
          <div className="flex flex-wrap justify-center gap-4">
            <button
              onClick={() => selectConsent("consent")}
              className="px-10 py-5 rounded-xl bg-green-600 hover:bg-green-700 text-white text-xl font-bold shadow-xl"
            >
              CONSENT
            </button>
            <button
              onClick={() => selectConsent("decline")}
              className="px-10 py-5 rounded-xl bg-gray-700 hover:bg-gray-600 text-white text-xl font-bold shadow-xl"
            >
              DO NOT CONSENT
            </button>
          </div>
        </div>
      )}

      {/* SCANNING — camera live, continuous recognition until a known
          participant is found. No bounding boxes, no per-frame name list. */}
      {kioskState === "scanning" && (
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 flex flex-col items-center gap-2 pointer-events-none">
          <div className="text-white text-3xl sm:text-4xl font-bold tracking-wide bg-black/40 px-6 py-3 rounded-xl">SCANNING...</div>
          <div className="text-white/80 text-lg sm:text-xl font-medium bg-black/40 px-4 py-2 rounded-lg">LOOK AT THE CAMERA</div>
        </div>
      )}

      {/* RESULT — full-viewport, camera already released. Only first names,
          no confidence, no IDs, no bounding boxes. */}
      {kioskState === "result" &&
        resultPeople &&
        (() => {
          const allNew = resultPeople.every((p) => p.checkin === "new");
          const allAlready = resultPeople.every((p) => p.checkin === "already");

          if (allNew) {
            return (
              <div className="absolute inset-0 bg-green-600 flex flex-col items-center justify-center gap-6 complete-backdrop px-8">
                <div className="text-white text-8xl sm:text-9xl leading-none">✓</div>
                <div className="text-white text-5xl sm:text-6xl font-extrabold tracking-wide">COMPLETE</div>
                <div className="flex flex-col items-center gap-2 mt-2">
                  {resultPeople.map((p, i) => (
                    <div key={i} className="text-white text-3xl sm:text-4xl font-semibold">
                      {p.name}
                    </div>
                  ))}
                </div>
              </div>
            );
          }

          if (allAlready) {
            return (
              <div className="absolute inset-0 bg-blue-600 flex flex-col items-center justify-center gap-6 complete-backdrop px-8">
                <div className="text-white text-8xl sm:text-9xl leading-none">✓</div>
                <div className="text-white text-4xl sm:text-5xl font-extrabold tracking-wide text-center">YOU ALREADY CHECKED IN</div>
                <div className="flex flex-col items-center gap-2 mt-2">
                  {resultPeople.map((p, i) => (
                    <div key={i} className="text-white text-3xl sm:text-4xl font-semibold">
                      {p.name}
                    </div>
                  ))}
                </div>
              </div>
            );
          }

          // Mixed: some new, some already checked in — one screen, each
          // person's own status shown beside their name.
          return (
            <div className="absolute inset-0 bg-blue-600 flex flex-col items-center justify-center gap-6 complete-backdrop px-8">
              <div className="text-white text-7xl sm:text-8xl leading-none">✓</div>
              <div className="text-white text-3xl sm:text-4xl font-extrabold tracking-wide text-center">CHECK-IN RESULT</div>
              <div className="flex flex-col gap-3 mt-2 w-full max-w-2xl">
                {resultPeople.map((p, i) => (
                  <div
                    key={i}
                    className="flex items-center justify-between gap-6 text-white text-2xl sm:text-3xl font-semibold border-b border-white/20 pb-2"
                  >
                    <span>✓ {p.name}</span>
                    <span className="text-lg sm:text-xl font-bold uppercase tracking-wide opacity-90">
                      {p.checkin === "new" ? "COMPLETE" : "ALREADY CHECKED IN"}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          );
        })()}

      {error && (
        <div className="absolute top-4 right-4 max-w-sm text-sm text-white bg-red-600/90 rounded-lg px-3 py-2 shadow-lg">{error}</div>
      )}

      {/* Debug/performance panel — developer-mode only (Settings → debug
          mode), rendered as a small corner overlay so it never occupies the
          recognition screen during normal kiosk operation. */}
      {debugMode && result?.timings_ms && (
        <div className="absolute bottom-4 right-4 max-w-xs bg-gray-900/90 text-gray-100 rounded-lg p-3 font-mono text-[11px] leading-relaxed overflow-x-auto max-h-64 overflow-y-auto backdrop-blur">
          <div className="text-gray-400 mb-2">Perf — faces: {result.faces_total}</div>
          <PerfLine label="Frontend capture" ms={clientTimings?.frontend_capture} />
          <PerfLine label="Image decode" ms={result.timings_ms.image_decode} />
          <PerfLine label="Image resize" ms={result.timings_ms.image_resize} />
          <PerfLine label="Face detection" ms={result.timings_ms.face_detection} />
          <PerfLine label="Embedding" ms={result.timings_ms.embedding} />
          <PerfLine label="Comparison" ms={result.timings_ms.comparison} />
          <PerfLine label="Check-in lookup" ms={result.timings_ms.database_read} />
          <PerfLine label="DB write" ms={result.timings_ms.database_write} note="background" />
          <PerfLine label="Image save" ms={result.timings_ms.image_save} note="background" />
          <PerfLine label="Network + overhead" ms={clientTimings?.network_and_overhead} />
          <PerfLine label="Frontend render" ms={clientTimings?.frontend_render} />
          <div className="border-t border-gray-700 my-1.5" />
          <PerfLine label="SERVER TOTAL" ms={result.timings_ms.total} bold />
          <PerfLine label="ROUND-TRIP TOTAL" ms={clientTimings?.round_trip_total} bold />
          {result.system && (
            <div className="text-gray-400 mt-2 pt-2 border-t border-gray-700">
              {result.system.inference_device} · {result.system.model_name} · {result.system.registered_faces_indexed} indexed
            </div>
          )}
          {systemInfo && (
            <div className="text-gray-400 mt-1">
              CPU {systemInfo.cpu_cores_physical}c/{systemInfo.cpu_cores_logical}t {systemInfo.cpu_usage_percent}% · RAM {systemInfo.ram_available_gb}/{systemInfo.ram_total_gb}GB
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function PerfLine({ label, ms, note, bold }: { label: string; ms?: number; note?: string; bold?: boolean }) {
  return (
    <div className={`flex justify-between ${bold ? "text-white font-bold" : ""}`}>
      <span>
        {label}
        {note && <span className="text-gray-500"> ({note})</span>}:
      </span>
      <span>{ms !== undefined ? `${ms.toFixed(1)} ms` : "—"}</span>
    </div>
  );
}
