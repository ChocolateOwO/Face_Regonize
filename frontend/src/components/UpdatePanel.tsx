import { useEffect, useRef, useState } from "react";
import { apiGet, apiPostJson, API_BASE, ApiError } from "../api/client";
import { Badge, Button, Card } from "./ui";
import { updateStatusBadge } from "../updateStatus";

interface UpdateStatus {
  current_version: string;
  latest_version: string | null;
  update_available: boolean;
  release_notes: string;
  release_url: string;
  release_name: string;
  last_checked: string | null;
  github_configured: boolean;
  error: string;
  no_published_release?: boolean;
  check_failed?: boolean;
  maintenance_active: boolean;
  abandoned_update: boolean;
  abandoned_log_path: string;
}

interface Progress {
  status: string;
  stage: string;
  error?: string;
  to_version?: string;
  log_path?: string;
  database_restored?: boolean;
  previous_version_healthy?: boolean;
}

// What /api/update/preflight proved before the admin confirms.
interface Preflight {
  target_tag: string;
  target_version: string;
  current_version: string;
  checks: string[];
}

const IN_PROGRESS = ["preparing", "backing_up", "updating", "restarting", "verifying", "rolling_back"];

export default function UpdatePanel() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [showNotes, setShowNotes] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // True while the backend is deliberately down mid-update. Connection
  // failures in this window are expected, not a crash, and must not be
  // rendered as one.
  const [reconnecting, setReconnecting] = useState(false);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const pollRef = useRef<number | null>(null);

  function loadStatus() {
    apiGet("/api/update/status")
      .then((s) => {
        setStatus(s);
        setError("");
      })
      .catch(() => {});
  }

  useEffect(() => {
    loadStatus();
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, []);

  // While an update runs the backend goes away and comes back. Poll the
  // health endpoint with plain fetch so a failed request here never touches
  // the shared api client's error handling (which would log the admin out or
  // surface a generic failure on every other page).
  function startPolling() {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/health`);
        if (!res.ok) throw new Error("unhealthy");
        setReconnecting(false);
        const p: Progress = await apiGet("/api/update/progress");
        setProgress(p);
        if (!IN_PROGRESS.includes(p.status)) {
          if (pollRef.current) window.clearInterval(pollRef.current);
          pollRef.current = null;
          loadStatus();
        }
      } catch {
        // Backend restarting — expected.
        setReconnecting(true);
      }
    }, 2000);
  }

  async function check() {
    setBusy(true);
    setError("");
    try {
      await apiPostJson("/api/update/check", {});
      loadStatus();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not check for updates.");
    } finally {
      setBusy(false);
    }
  }

  // Step 1: prove the update can run (release verified, no active work,
  // backups possible) and show the admin exactly what would happen.
  async function review() {
    setBusy(true);
    setError("");
    setPreflight(null);
    try {
      setPreflight(await apiPostJson("/api/update/preflight", {}));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not check whether the update can run.");
    } finally {
      setBusy(false);
    }
  }

  // Step 2: only after the admin confirms the named version.
  async function install() {
    if (!preflight) return;
    setBusy(true);
    setError("");
    try {
      await apiPostJson("/api/update/install", {
        tag: preflight.target_tag,
        confirm_version: preflight.target_version,
      });
      setPreflight(null);
      setProgress({ status: "preparing", stage: "Preparing update..." });
      startPolling();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the update.");
    } finally {
      setBusy(false);
    }
  }

  if (!status) return null;

  const running = progress && IN_PROGRESS.includes(progress.status);
  const finished = progress && !IN_PROGRESS.includes(progress.status);
  const badge = updateStatusBadge(status);

  return (
    <Card>
      <h2 className="font-semibold text-gray-900 mb-3">Application Updates</h2>

      <div className="space-y-3 text-sm">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div>
            <span className="text-gray-500">Application Version:</span>{" "}
            <strong className="text-gray-900">{status.current_version}</strong>
          </div>
          {!running && (
            <Button variant="secondary" onClick={check} disabled={busy}>
              {busy ? "Checking..." : "Check for Updates"}
            </Button>
          )}
        </div>

        {!status.github_configured ? (
          <div className="text-sm text-gray-500 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
            Updates are not configured on this machine. Set <code>GITHUB_REPO</code> in <code>.env</code> (for example{" "}
            <code>owner/Reconize</code>) to enable them. No access token is required.
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <span className="text-gray-500">Update Status:</span>
            {badge && <Badge tone={badge.tone}>{badge.label}</Badge>}
          </div>
        )}

        {status.last_checked && (
          <div className="text-xs text-gray-400">
            Last checked: {new Date(status.last_checked).toLocaleString()}
          </div>
        )}

        {status.error && (
          <div className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
            {status.error}
          </div>
        )}

        {status.abandoned_update && (
          <div className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
            A previous update did not complete. Technical log:{" "}
            <code className="text-xs">{status.abandoned_log_path}</code>
          </div>
        )}

        {error && (
          <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2 whitespace-pre-wrap">
            {error}
          </div>
        )}

        {/* Update available — admin decides. Nothing installs on its own. */}
        {status.update_available && !running && !finished && (
          <div className="border border-indigo-200 bg-indigo-50 rounded-lg p-4">
            <div className="font-semibold text-indigo-900 mb-2">New Update Available</div>
            <div className="grid grid-cols-2 gap-4 mb-3 text-sm">
              <div>
                <div className="text-xs text-gray-500">Current</div>
                <div className="font-medium">v{status.current_version}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500">Available</div>
                <div className="font-medium text-indigo-700">v{status.latest_version}</div>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => setShowNotes((v) => !v)}>
                What's New
              </Button>
              {!preflight && (
                <Button onClick={review} disabled={busy}>
                  {busy ? "Checking..." : "Update Now"}
                </Button>
              )}
              <Button
                variant="secondary"
                onClick={() => {
                  setPreflight(null);
                  setStatus({ ...status, update_available: false });
                }}
              >
                Later
              </Button>
            </div>
            {preflight && (
              <div className="mt-3 bg-white border border-indigo-200 rounded-lg p-3">
                <div className="font-semibold text-gray-900 mb-1">
                  Install v{preflight.target_version} (installed: v{preflight.current_version})?
                </div>
                <ul className="text-xs text-gray-600 list-disc pl-4 mb-2">
                  {preflight.checks.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
                <div className="text-xs text-gray-700 mb-3">
                  The database and .env are backed up and verified first. Reconize then restarts and is
                  unavailable for about 1–3 minutes; open pages and camera nodes reconnect afterwards. If the new
                  version does not start, the previous version and its database are restored automatically.
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button onClick={install} disabled={busy}>
                    Install v{preflight.target_version}
                  </Button>
                  <Button variant="secondary" onClick={() => setPreflight(null)} disabled={busy}>
                    Cancel
                  </Button>
                </div>
              </div>
            )}
            {showNotes && (
              <div className="mt-3 bg-white border border-indigo-100 rounded-lg p-3">
                <div className="font-medium text-sm mb-1">{status.release_name || `v${status.latest_version}`}</div>
                <pre className="text-xs text-gray-700 whitespace-pre-wrap font-sans">
                  {status.release_notes || "No release notes were provided."}
                </pre>
                {status.release_url && (
                  <a
                    href={status.release_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs text-indigo-600 hover:underline mt-2 inline-block"
                  >
                    View on GitHub →
                  </a>
                )}
              </div>
            )}
          </div>
        )}

        {running && (
          <div className="border border-indigo-200 bg-indigo-50 rounded-lg p-4">
            <div className="font-semibold text-indigo-900 mb-1">
              {reconnecting ? "Restarting / Reconnecting…" : progress?.stage || "Working..."}
            </div>
            <div className="text-xs text-indigo-700">
              {reconnecting
                ? "The backend is restarting. This is part of the update — please wait."
                : "Do not close the application."}
            </div>
            <div className="w-full bg-white/60 rounded-full h-2 mt-3 overflow-hidden">
              <div className="bg-indigo-600 h-2 rounded-full animate-pulse w-full" />
            </div>
          </div>
        )}

        {finished && progress?.status === "completed" && (
          <div className="border border-green-200 bg-green-50 rounded-lg p-4">
            <div className="font-semibold text-green-800">✓ UPDATE COMPLETE</div>
            <div className="text-sm text-green-700">Version {progress.to_version}</div>
            <Button className="mt-3" onClick={() => window.location.reload()}>
              Reload
            </Button>
          </div>
        )}

        {finished && progress?.status !== "completed" && (
          <div className="border border-red-200 bg-red-50 rounded-lg p-4">
            <div className="font-semibold text-red-800">UPDATE FAILED</div>
            <div className="text-sm text-red-700">
              {progress?.status === "rolled_back"
                ? "The previous version has been restored."
                : "The update did not complete."}
              {progress?.database_restored ? " The database backup was restored." : ""}
            </div>
            {progress?.status === "rolled_back" && progress?.previous_version_healthy === false && (
              <div className="text-sm text-red-800 mt-2">
                The previous version did not come back on its own. Start Reconize again with start.bat (or the
                Hub's Open button); if it still does not start, send the technical log below.
              </div>
            )}
            {progress?.error && (
              <pre className="text-xs text-red-600 mt-2 whitespace-pre-wrap font-sans">{progress.error}</pre>
            )}
            {progress?.log_path && (
              <div className="text-xs text-gray-500 mt-2">
                Technical log: <code>{progress.log_path}</code>
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
