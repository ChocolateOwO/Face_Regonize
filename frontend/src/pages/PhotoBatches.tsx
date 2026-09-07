import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiDelete, apiGet, apiPostJson, ApiError } from "../api/client";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Spinner } from "../components/ui";
import PhotoBatchDownload from "../components/PhotoBatchDownload";

interface Batch {
  id: string;
  label: string;
  status: "pending" | "processing" | "syncing_drive" | "completed" | "failed";
  download_ready?: boolean;
  current_stage: string;
  total_photos: number;
  processed_photos: number;
  recognized_photos: number;
  ambience_photos: number;
  review_photos: number;
  failed_photos: number;
  created_at: string;
}

function StatusBadge({ status }: { status: string }) {
  if (status === "completed") return <Badge tone="good">Completed</Badge>;
  if (status === "failed") return <Badge tone="bad">Failed</Badge>;
  if (status === "processing") return <Badge tone="warn">Processing</Badge>;
  // Photos are already processed and previewable here — only the Drive mirror
  // is outstanding, so this must not read as "still processing".
  if (status === "syncing_drive") return <Badge tone="warn">Syncing to Drive</Badge>;
  return <Badge>Pending</Badge>;
}

export default function PhotoBatches() {
  const [batches, setBatches] = useState<Batch[] | null>(null);
  const [driveEmail, setDriveEmail] = useState("");
  const [oauth, setOauth] = useState<{ connected: boolean; email: string | null } | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [connectError, setConnectError] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);

  function load() {
    apiGet("/api/photo-batches").then(setBatches);
  }

  function loadOauthStatus() {
    apiGet("/api/photo-batches/drive-oauth/status").then(setOauth);
  }

  useEffect(() => {
    load();
    loadOauthStatus();
    apiGet("/api/photo-batches/drive-info").then((d) => setDriveEmail(d.service_account_email));

    const params = new URLSearchParams(window.location.search);
    if (params.get("drive_connected")) {
      loadOauthStatus();
      window.history.replaceState({}, "", window.location.pathname);
    } else if (params.get("drive_error")) {
      setConnectError(params.get("drive_error") || "Could not connect Google Drive.");
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  // Reflect the local/Drive boundary without requiring a page reload.
  useEffect(() => {
    if (!batches?.some((b) => ["pending", "processing", "syncing_drive"].includes(b.status))) return;
    const timer = window.setInterval(load, 2000);
    return () => window.clearInterval(timer);
  }, [batches]);

  async function connectDrive() {
    setConnectError("");
    try {
      const res = await apiGet("/api/photo-batches/drive-oauth/start");
      window.location.href = res.auth_url;
    } catch (err) {
      setConnectError(err instanceof ApiError ? err.message : "Could not start Google Drive connection.");
    }
  }
  async function deleteBatch(id: string) {
    if (!window.confirm("Delete this photo processing batch? If it is running, processing will stop first. Local batch outputs will be removed; Google Drive source photos are not deleted.")) return;
    setDeleting(id);
    try { await apiDelete(`/api/photo-batches/${id}`); load(); }
    catch (err) { setConnectError(err instanceof ApiError ? err.message : "Could not delete batch."); }
    finally { setDeleting(null); }
  }

  return (
    <div>
      <PageHeader
        title="Event Photos"
        subtitle="Process a photographer's Google Drive folder — sort by participant, apply PDPA blur, and manage retention."
        action={<Button onClick={() => setShowNew(true)}>+ New Batch</Button>}
      />

      <Card className="mb-4 bg-indigo-50 border-indigo-100">
        <p className="text-sm text-indigo-900">
          📁 Share the photographer's Drive folder (Viewer access is enough — this account only reads) with:{" "}
          {driveEmail ? (
            <code className="bg-white px-1.5 py-0.5 rounded">{driveEmail}</code>
          ) : (
            <span className="text-red-700">Google Drive is not configured yet — set GOOGLE_SERVICE_ACCOUNT_KEY_PATH in .env.</span>
          )}
        </p>
      </Card>

      <Card className="mb-4 bg-amber-50 border-amber-100">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <p className="text-sm text-amber-900">
            ✍️ Processed photos are uploaded into a new folder in <strong>your own Google Drive</strong> (named after the source folder).{" "}
            {oauth?.connected ? (
              <>
                Connected as <code className="bg-white px-1.5 py-0.5 rounded">{oauth.email}</code>.
              </>
            ) : (
              "Not connected — batches still process and are viewable in the app, but nothing will be uploaded to Drive."
            )}
          </p>
          <Button variant={oauth?.connected ? "secondary" : "primary"} onClick={connectDrive}>
            {oauth?.connected ? "Reconnect Google Drive" : "Connect Google Drive"}
          </Button>
        </div>
        {connectError && <div className="text-sm text-red-600 mt-2">{connectError}</div>}
      </Card>

      <Card className="p-0 overflow-hidden">
        {!batches ? (
          <Spinner />
        ) : batches.length === 0 ? (
          <EmptyState>No photo batches yet. Click "+ New Batch" to process a photographer's folder.</EmptyState>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-3">Batch</th>
                <th className="text-left px-4 py-3">Status</th>
                <th className="text-left px-4 py-3">Photos</th>
                <th className="text-left px-4 py-3">Recognized / Ambience / Review</th>
                <th className="text-left px-4 py-3">Created</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {batches.map((b) => (
                <tr key={b.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <Link to={`/photo-batches/${b.id}`} className="text-indigo-600 hover:underline font-medium">
                      {b.label}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={b.status} />
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {b.processed_photos} / {b.total_photos}
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {b.recognized_photos} / {b.ambience_photos} / {b.review_photos}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{new Date(b.created_at).toLocaleString()}</td>
                  <td className="px-4 py-3 text-right"><div className="flex justify-end items-start gap-2">
                    <PhotoBatchDownload id={b.id} ready={b.download_ready} status={b.status} stopping={deleting === b.id} />
                    <Button variant="danger" disabled={deleting === b.id} onClick={() => deleteBatch(b.id)}>{deleting === b.id ? "Stopping..." : "Delete"}</Button>
                  </div></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {showNew && (
        <NewBatchModal
          onClose={() => setShowNew(false)}
          onCreated={() => {
            setShowNew(false);
            load();
          }}
        />
      )}
    </div>
  );
}

function NewBatchModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [url, setUrl] = useState("");
  const [retentionDays, setRetentionDays] = useState(7);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [logo, setLogo] = useState<string | null>(null);
  const [logoSize, setLogoSize] = useState(15);
  const [logoPosition, setLogoPosition] = useState("bottom-right");

  async function submit() {
    setError("");
    if (!url.trim()) return setError("Paste the Drive folder link or ID.");
    setLoading(true);
    try {
      await apiPostJson("/api/photo-batches", { drive_folder_url: url.trim(), retention_days: retentionDays, logo_data_url: logo, logo_size: logoSize / 100, logo_position: logoPosition });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the batch.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl w-full max-w-md p-6">
        <h2 className="font-semibold text-lg mb-4">New Photo Batch</h2>
        <div className="space-y-3">
          <div>
            <label className="block text-sm text-gray-600 mb-1">Google Drive folder link or ID</label>
            <Input placeholder="https://drive.google.com/drive/folders/..." value={url} onChange={(e) => setUrl(e.target.value)} />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">MEDIA logo (optional PNG)</label>
            <Input type="file" accept="image/png" onChange={(e) => { const file = e.target.files?.[0]; if (!file) return setLogo(null); const reader = new FileReader(); reader.onload = () => setLogo(typeof reader.result === "string" ? reader.result : null); reader.readAsDataURL(file); }} />
            {logo && <>
              <div className="grid grid-cols-2 gap-2 mt-2">
                <label className="text-xs text-gray-600">Size: {logoSize}%<input className="w-full" type="range" min="2" max="50" value={logoSize} onChange={(e) => setLogoSize(Number(e.target.value))} /></label>
                <label className="text-xs text-gray-600">Position<select className="w-full border rounded px-2 py-1" value={logoPosition} onChange={(e) => setLogoPosition(e.target.value)}>{["top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"].map((position) => <option key={position}>{position}</option>)}</select></label>
              </div>
              <div className="relative mt-2 h-36 bg-gray-100 border rounded overflow-hidden" aria-label="Logo placement preview"><img src={logo} alt="Logo preview" className="absolute object-contain" style={{ width: `${logoSize}%`, left: logoPosition.endsWith("left") ? "2%" : logoPosition.endsWith("right") ? `${98 - logoSize}%` : `${(100 - logoSize) / 2}%`, top: logoPosition.startsWith("top") ? "2%" : `${98 - logoSize}%` }} /></div>
            </>}
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">Data retention period</label>
            <select
              value={retentionDays}
              onChange={(e) => setRetentionDays(Number(e.target.value))}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
            >
              {[1, 2, 3, 4, 5, 6, 7].map((d) => (
                <option key={d} value={d}>
                  {d} Day{d === 1 ? "" : "s"}
                </option>
              ))}
            </select>
            <p className="text-xs text-gray-400 mt-1">This app's processing records for the batch are automatically deleted after this period. Processed photos already placed in Google Drive are not affected.</p>
          </div>
          {error && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</div>}
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={loading}>
            {loading ? "Starting..." : "Start Processing"}
          </Button>
        </div>
      </div>
    </div>
  );
}
