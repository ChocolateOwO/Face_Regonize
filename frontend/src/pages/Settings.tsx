import { useEffect, useState } from "react";
import { apiGet, apiPostJson, apiPutJson } from "../api/client";
import { Button, Card, Input, PageHeader } from "../components/ui";
import UpdatePanel from "../components/UpdatePanel";

interface DataCounts {
  attendance_count: number;
  recognition_history_count: number;
  upload_count: number;
}

type ClearAction = "attendance" | "history" | "all";

export default function Settings() {
  const [values, setValues] = useState<Record<string, string> | null>(null);
  const [saved, setSaved] = useState(false);
  const [counts, setCounts] = useState<DataCounts | null>(null);
  const [confirmStep, setConfirmStep] = useState<{ action: ClearAction; step: 1 | 2 } | null>(null);
  const [clearing, setClearing] = useState(false);
  const [clearMessage, setClearMessage] = useState("");

  useEffect(() => {
    apiGet("/api/settings").then(setValues);
    refreshCounts();
  }, []);

  function refreshCounts() {
    apiGet("/api/admin/data-counts").then(setCounts).catch(() => {});
  }

  async function runClearAction(action: ClearAction) {
    setClearing(true);
    setClearMessage("");
    try {
      if (action === "attendance") {
        const res = await apiPostJson("/api/admin/clear-attendance", {});
        setClearMessage(`Cleared ${res.deleted} attendance record${res.deleted === 1 ? "" : "s"}.`);
      } else if (action === "history") {
        const res = await apiPostJson("/api/admin/clear-recognition-history", {});
        setClearMessage(`Cleared ${res.deleted} recognition history record${res.deleted === 1 ? "" : "s"}.`);
      } else {
        const res = await apiPostJson("/api/admin/clear-all-event-data", {});
        setClearMessage(`Cleared ${res.attendance_deleted} attendance record${res.attendance_deleted === 1 ? "" : "s"} and ${res.history_deleted} recognition history record${res.history_deleted === 1 ? "" : "s"}.`);
      }
      refreshCounts();
    } catch {
      setClearMessage("Could not complete the action — please try again.");
    } finally {
      setClearing(false);
      setConfirmStep(null);
    }
  }

  function set(key: string, value: string) {
    setValues((v) => (v ? { ...v, [key]: value } : v));
    setSaved(false);
  }

  async function save() {
    if (!values) return;
    const { storage_path: _sp, ...rest } = values;
    void _sp;
    await apiPutJson("/api/settings", { values: rest });
    setSaved(true);
  }

  if (!values) return null;

  return (
    <div>
      <PageHeader title="Settings" subtitle="Face recognition thresholds, event details, and storage." />

      <div className="space-y-4 max-w-xl">
        <Card>
          <h2 className="font-semibold text-gray-900 mb-3">Face Recognition</h2>
          <div className="space-y-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">Similarity Threshold ({(Number(values.face_match_threshold) * 100).toFixed(0)}%)</label>
              <input
                type="range"
                min={0.2}
                max={0.8}
                step={0.01}
                value={values.face_match_threshold}
                onChange={(e) => set("face_match_threshold", e.target.value)}
                className="w-full"
              />
              <p className="text-xs text-gray-400 mt-1">Higher = stricter matching, fewer false positives.</p>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">Minimum Detection Confidence</label>
              <Input value={values.face_detection_confidence} onChange={(e) => set("face_detection_confidence", e.target.value)} />
            </div>
            <label className="flex items-center gap-2 text-sm text-gray-700 pt-1">
              <input
                type="checkbox"
                checked={values.debug_mode === "true"}
                onChange={(e) => set("debug_mode", e.target.checked ? "true" : "false")}
              />
              Show Performance Metrics (development/debug mode)
            </label>
            <p className="text-xs text-gray-400 -mt-2">
              Shows a per-stage timing breakdown and system info panel on the Face Recognition page.
            </p>
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold text-gray-900 mb-3">Event</h2>
          <div className="space-y-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">Event Name</label>
              <Input value={values.event_name} onChange={(e) => set("event_name", e.target.value)} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">Event Date</label>
              <Input type="date" value={values.event_date} onChange={(e) => set("event_date", e.target.value)} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">Event Location</label>
              <Input value={values.event_location} onChange={(e) => set("event_location", e.target.value)} />
            </div>
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold text-gray-900 mb-3">System</h2>
          <div className="space-y-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">Application Name</label>
              <Input value={values.app_name} onChange={(e) => set("app_name", e.target.value)} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">Image Storage Directory</label>
              <Input value={values.storage_path} disabled className="bg-gray-50 text-gray-500" />
            </div>
          </div>
        </Card>

        <UpdatePanel />

        <Card className="bg-indigo-50 border-indigo-100">
          <p className="text-sm text-indigo-900">
            🔒 <strong>Privacy:</strong> Face images and embeddings are stored locally on this machine. Recognition runs entirely
            on-device — no facial data is sent to any external API.
          </p>
        </Card>

        <div className="flex items-center gap-3">
          <Button onClick={save}>Save Settings</Button>
          {saved && <span className="text-sm text-green-600">Saved ✓</span>}
        </div>

        <Card className="border-red-200">
          <h2 className="font-semibold text-red-700 mb-1">Data Reset</h2>
          <p className="text-xs text-gray-500 mb-3">
            Clears event data only — participants, face embeddings, imported people, and recognition settings are never touched by
            these actions.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button variant="danger" onClick={() => setConfirmStep({ action: "attendance", step: 1 })}>
              Clear Check-In Data
            </Button>
            <Button variant="danger" onClick={() => setConfirmStep({ action: "history", step: 1 })}>
              Clear Recognition History
            </Button>
            <Button variant="danger" onClick={() => setConfirmStep({ action: "all", step: 1 })}>
              Clear All Event Data
            </Button>
          </div>
          {clearMessage && <p className="text-sm text-gray-600 mt-3">{clearMessage}</p>}
        </Card>
      </div>

      {confirmStep && counts && (
        <ClearConfirmDialog
          action={confirmStep.action}
          step={confirmStep.step}
          counts={counts}
          clearing={clearing}
          onCancel={() => setConfirmStep(null)}
          onConfirm={() => {
            if (confirmStep.action === "all" && confirmStep.step === 1) {
              setConfirmStep({ action: "all", step: 2 });
            } else {
              runClearAction(confirmStep.action);
            }
          }}
        />
      )}
    </div>
  );
}

function ClearConfirmDialog({
  action,
  step,
  counts,
  clearing,
  onCancel,
  onConfirm,
}: {
  action: ClearAction;
  step: 1 | 2;
  counts: DataCounts;
  clearing: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  let body: string;
  let confirmLabel: string;

  if (action === "attendance") {
    body = `Are you sure you want to clear ${counts.attendance_count} attendance record${counts.attendance_count === 1 ? "" : "s"}? This data will be gone forever and cannot be recovered.`;
    confirmLabel = "Clear Attendance";
  } else if (action === "history") {
    body = `Are you sure you want to clear ${counts.recognition_history_count} recognition history record${counts.recognition_history_count === 1 ? "" : "s"}? This data will be gone forever and cannot be recovered.`;
    confirmLabel = "Clear History";
  } else if (step === 1) {
    body = `This will clear ${counts.attendance_count} attendance record${counts.attendance_count === 1 ? "" : "s"} AND ${counts.recognition_history_count} recognition history record${counts.recognition_history_count === 1 ? "" : "s"}. Continue?`;
    confirmLabel = "Continue";
  } else {
    body = "Are you sure? This is your final confirmation — this data will be gone forever and cannot be recovered. Participants, face embeddings, and settings will not be affected.";
    confirmLabel = "Clear All Event Data";
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 px-4">
      <div className="bg-white rounded-xl shadow-2xl p-6 max-w-sm w-full">
        <p className="text-sm text-gray-800 mb-5">{body}</p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel} disabled={clearing}>
            Cancel
          </Button>
          <Button variant="danger" onClick={onConfirm} disabled={clearing}>
            {clearing ? "Clearing…" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
