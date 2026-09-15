import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiDelete, apiGet, apiPostFormWithProgress, apiPostJson, ApiError } from "../api/client";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Spinner } from "../components/ui";
import PhotoBatchDownload from "../components/PhotoBatchDownload";

interface Batch {
  id: string;
  label: string;
  source_type?: "drive" | "local";
  status: "pending" | "processing" | "pausing" | "paused" | "resuming" | "stopping" | "cancelled" | "ready"
    | "needs_retry" | "uploading" | "upload_failed" | "syncing_drive" | "completed" | "failed";
  // Pause / Resume / Delete — what the backend allows right now.
  can_pause?: boolean;
  can_resume?: boolean;
  can_delete?: boolean;
  pause_diagnostics?: {
    pause_requested_at: string | null; last_heartbeat_at: string | null; seconds_since_heartbeat: number | null;
    workers_active: boolean; stage: string; slow: boolean; stale: boolean;
  };
  download_ready?: boolean;
  current_stage: string;
  eta_seconds: number | null;
  eta_estimating: boolean;
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
  if (status === "ready") return <Badge tone="good">Ready</Badge>;
  // Photos are already processed and previewable here — only the (manually
  // started) Drive upload is outstanding, so this must not read as "still
  // processing". "syncing_drive" is the old automatic-mirror status.
  if (status === "uploading" || status === "syncing_drive") return <Badge tone="warn">Uploading to Drive</Badge>;
  if (status === "upload_failed") return <Badge tone="bad">Drive Upload Failed</Badge>;
  if (status === "pausing") return <Badge tone="warn">Pausing…</Badge>;
  if (status === "paused") return <Badge tone="warn">Paused</Badge>;
  if (status === "resuming") return <Badge tone="warn">Resuming…</Badge>;
  if (status === "cancelled") return <Badge>Stopped</Badge>;
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
    if (!batches?.some((b) => ["pending", "processing", "uploading", "syncing_drive"].includes(b.status))) return;
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
    if (!window.confirm("Delete this batch permanently?\n\nThis removes the batch's photos, processed outputs and records from this computer. Participants, attendance, consent history, upload history and other batches are not affected, and Google Drive source photos are not deleted. This cannot be undone.")) return;
    setDeleting(id);
    try { await apiDelete(`/api/photo-batches/${id}`); load(); }
    catch (err) { setConnectError(err instanceof ApiError ? err.message : "Could not delete batch."); }
    finally { setDeleting(null); }
  }

  // Pause / Resume; Delete is a separate action.
  async function pauseBatch(id: string) {
    if (!window.confirm("Pause processing? Completed work will be kept. You can resume this batch later.")) return;
    setDeleting(id);
    try { await apiPostJson(`/api/photo-batches/${id}/pause`, {}); load(); }
    catch (err) { setConnectError(err instanceof ApiError ? err.message : "Could not pause processing."); }
    finally { setDeleting(null); }
  }

  async function resumeBatch(id: string) {
    setDeleting(id);
    try { await apiPostJson(`/api/photo-batches/${id}/resume`, {}); load(); }
    catch (err) { setConnectError(err instanceof ApiError ? err.message : "Could not resume processing."); }
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
        <p className="text-xs font-semibold uppercase tracking-wide text-indigo-500 mb-1">Source — Photographer's Google Drive</p>
        <p className="text-sm text-indigo-900">
          📁 Share the photographer's Drive folder (Viewer access is enough — this account only reads) with:{" "}
          {driveEmail ? (
            <code className="bg-white px-1.5 py-0.5 rounded">{driveEmail}</code>
          ) : (
            <span className="text-red-700">Google Drive is not configured yet — set GOOGLE_SERVICE_ACCOUNT_KEY_PATH in .env.</span>
          )}
          {driveEmail && <> — this is what makes "Google Drive folder" work when creating a batch below. It does not depend on the Drive connection underneath.</>}
        </p>
      </Card>

      <Card className="mb-4 bg-amber-50 border-amber-100">
        <p className="text-xs font-semibold uppercase tracking-wide text-amber-600 mb-1">Destination — My Google Drive (optional)</p>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <p className="text-sm text-amber-900">
            ✍️ Optional: also mirror processed photos into a new folder in <strong>your own Google Drive</strong> (named after the source folder).{" "}
            {oauth?.connected ? (
              <>
                Connected as <code className="bg-white px-1.5 py-0.5 rounded">{oauth.email}</code>.
              </>
            ) : (
              "Not connected — this only disables the optional Drive mirror above. Batches still process and are fully viewable/downloadable in the app either way."
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
                <th className="text-left px-4 py-3">Recognized / Ambience</th>
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
                    {b.recognized_photos} / {b.ambience_photos}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{new Date(b.created_at).toLocaleString()}</td>
                  <td className="px-4 py-3 text-right"><div className="flex justify-end items-start gap-2">
                    <PhotoBatchDownload id={b.id} ready={b.download_ready} status={b.status} stopping={deleting === b.id} />
                    {b.can_pause && (
                      <Button variant="secondary" disabled={deleting === b.id} onClick={() => pauseBatch(b.id)}>{deleting === b.id ? "Pausing…" : "Pause processing"}</Button>
                    )}
                    {b.status === "pausing" && !b.can_pause && <Button variant="secondary" disabled>Pausing…</Button>}
                    {b.status === "resuming" && <Button disabled>Resuming…</Button>}
                    {b.can_resume && (
                      <Button disabled={deleting === b.id} onClick={() => resumeBatch(b.id)}>Resume processing</Button>
                    )}
                    {!b.can_pause && !["pausing", "resuming", "processing", "pending"].includes(b.status) && (
                      <span title={b.can_delete === false ? "Something is still using this batch — try again when it finishes." : undefined}>
                        <Button variant="danger" disabled={deleting === b.id || b.can_delete === false}
                          onClick={() => deleteBatch(b.id)}>{deleting === b.id ? "Deleting…" : "Delete"}</Button>
                      </span>
                    )}
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

/** Browsers only set this for a folder pick, and it is the ONLY way to tell a
 *  folder-sourced file from an individually-picked one. */
function relativePath(file: File): string {
  return (file as { webkitRelativePath?: string }).webkitRelativePath || "";
}

function isImageFile(file: File): boolean {
  return file.type.startsWith("image/") || /\.(jpe?g|png|webp|bmp|tiff?|heic|heif|gif|avif)$/i.test(file.name);
}

type MaskChoice = { id: string; label: string; data_url: string };

function NewBatchModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [sourceType, setSourceType] = useState<"drive" | "local">("drive");
  const [url, setUrl] = useState("");
  const [localLabel, setLocalLabel] = useState("");
  const [localFiles, setLocalFiles] = useState<File[]>([]);
  const [retentionDays, setRetentionDays] = useState(7);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [logo, setLogo] = useState<string | null>(null);
  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [logoSize, setLogoSize] = useState(15);
  const [logoPosition, setLogoPosition] = useState("bottom-right");
  // Phase D2 — how declined participants are concealed. "blur" (default),
  // "emoji:<id>" (built-in, drawn by the backend), or "image" (custom PNG).
  const [maskStyle, setMaskStyle] = useState("blur");
  const [maskChoices, setMaskChoices] = useState<MaskChoice[]>([]);
  const [maskFile, setMaskFile] = useState<File | null>(null);
  const [maskDataUrl, setMaskDataUrl] = useState<string | null>(null);
  const [skippedCount, setSkippedCount] = useState(0);
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  const [successMessage, setSuccessMessage] = useState("");
  const filesInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    apiGet("/api/photo-batches/mask-styles")
      .then((r) => setMaskChoices(Array.isArray(r?.styles) ? r.styles : []))
      .catch(() => setMaskChoices([]));  // blur and custom PNG still work without the catalog
  }, []);

  function pickMask(file: File | undefined) {
    if (!file) { setMaskFile(null); setMaskDataUrl(null); return; }
    setMaskFile(file);
    const reader = new FileReader();
    reader.onload = () => setMaskDataUrl(typeof reader.result === "string" ? reader.result : null);
    reader.readAsDataURL(file);
  }

  function pickLogo(file: File | undefined) {
    if (!file) { setLogo(null); setLogoFile(null); return; }
    setLogoFile(file);
    const reader = new FileReader();
    reader.onload = () => setLogo(typeof reader.result === "string" ? reader.result : null);
    reader.readAsDataURL(file);
  }

  function addLocalFiles(picked: FileList | null) {
    if (!picked || picked.length === 0) return;
    // Materialise the FileList into a real array HERE, synchronously. It is a
    // live view onto the input's own files, and the caller clears the input
    // (input.value = "") immediately after this returns so the same file can be
    // picked again — which would empty this list before React ever ran a lazy
    // setState updater over it.
    const chosen = Array.from(picked);
    const images = chosen.filter(isImageFile);
    if (images.length) setLocalFiles((prev) => [...prev, ...images]);
    // A folder pick returns EVERY file in the folder — browsers do not apply
    // `accept` to directory selection — so non-images are dropped here rather
    // than uploaded just to be rejected server-side.
    setSkippedCount((n) => n + (chosen.length - images.length));
  }

  function removeLocalFile(index: number) {
    setLocalFiles((prev) => prev.filter((_, i) => i !== index));
  }

  function clearLocalFiles() {
    setLocalFiles([]);
    setSkippedCount(0);
  }

  const folderNames = Array.from(
    new Set(localFiles.map(relativePath).filter(Boolean).map((path) => path.split("/")[0])),
  );
  const looseFileCount = localFiles.filter((f) => !relativePath(f)).length;

  async function submit() {
    setError("");
    setSuccessMessage("");
    if (sourceType === "drive") {
      if (!url.trim()) return setError("Paste the Drive folder link or ID.");
    } else {
      if (!localLabel.trim()) return setError("Name this photo batch.");
      if (localFiles.length === 0) return setError("Choose at least one photo (or a folder) to upload.");
    }
    if (maskStyle === "image" && !maskFile) return setError("Choose a PNG image for the custom privacy mask.");
    setLoading(true);
    try {
      if (sourceType === "drive") {
        await apiPostJson("/api/photo-batches", { drive_folder_url: url.trim(), retention_days: retentionDays, logo_data_url: logo, logo_size: logoSize / 100, logo_position: logoPosition, mask_style: maskStyle, mask_data_url: maskStyle === "image" ? maskDataUrl : null });
        onCreated();
        return;
      }

      const form = new FormData();
      form.append("label", localLabel.trim());
      form.append("retention_days", String(retentionDays));
      form.append("logo_position", logoPosition);
      form.append("logo_size", String(logoSize / 100));
      if (logoFile) form.append("logo", logoFile);
      form.append("mask_style", maskStyle);
      if (maskStyle === "image" && maskFile) form.append("mask", maskFile);
      for (const file of localFiles) form.append("files", file, relativePath(file) || file.name);

      setUploadPercent(0);
      const batch = await apiPostFormWithProgress("/api/photo-batches/upload", form, setUploadPercent);
      setUploadPercent(null);
      setSuccessMessage(`✓ Uploaded ${localFiles.length} photo${localFiles.length === 1 ? "" : "s"} — batch "${batch?.label ?? localLabel.trim()}" created, processing has started.`);
      window.setTimeout(onCreated, 1400);  // let the result actually be read before the modal closes
    } catch (err) {
      setUploadPercent(null);
      setError(err instanceof ApiError ? err.message : "Could not create the batch.");
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      {/* max-h + inner scroll: once a folder's worth of filenames is listed the
          modal outgrows the viewport, and a fixed overlay cannot be scrolled —
          the footer buttons would be clipped off-screen and unclickable. */}
      <div className="bg-white rounded-xl w-full max-w-md p-6 max-h-[90vh] flex flex-col">
        <h2 className="font-semibold text-lg mb-4 shrink-0">New Photo Batch</h2>
        <div className="space-y-3 overflow-y-auto flex-1 -mx-6 px-6">
          <div>
            <label className="block text-sm text-gray-600 mb-1">Where are the photos coming from?</label>
            <div className="flex gap-4 text-sm">
              <label className="flex items-center gap-1.5">
                <input type="radio" checked={sourceType === "drive"} onChange={() => setSourceType("drive")} />
                Photographer's Google Drive folder
              </label>
              <label className="flex items-center gap-1.5">
                <input type="radio" checked={sourceType === "local"} onChange={() => setSourceType("local")} />
                Upload from this computer
              </label>
            </div>
          </div>
          {sourceType === "drive" ? (
            <div>
              <label className="block text-sm text-gray-600 mb-1">Photographer's Google Drive folder link or ID</label>
              <Input placeholder="https://drive.google.com/drive/folders/..." value={url} onChange={(e) => setUrl(e.target.value)} />
              <p className="text-xs text-gray-400 mt-1">Read-only — uses the service account above. This is separate from "My Google Drive" below and does not require it to be connected.</p>
            </div>
          ) : (
            <>
              <div>
                <label className="block text-sm text-gray-600 mb-1">Batch name</label>
                <Input placeholder="e.g. Graduation Day 2026" value={localLabel} onChange={(e) => setLocalLabel(e.target.value)} />
              </div>
              <div>
                <label className="block text-sm text-gray-600 mb-1">Photos</label>
                {/* Real <input type=file> elements, hidden — two clearly distinct
                    labeled buttons trigger them, instead of showing the raw native
                    file inputs (whose browser-default button text/appearance for
                    a plain multi-file picker and a directory picker looks the
                    same, which is exactly what was unclear about the previous version). */}
                <input
                  ref={filesInputRef} type="file" accept="image/*" multiple className="hidden"
                  onChange={(e) => { addLocalFiles(e.target.files); e.target.value = ""; }}
                />
                <input
                  ref={folderInputRef} type="file" multiple className="hidden"
                  // @ts-expect-error webkitdirectory/directory are non-standard but widely supported HTML attributes for folder selection
                  webkitdirectory="" directory=""
                  onChange={(e) => { addLocalFiles(e.target.files); e.target.value = ""; }}
                />
                <div className="grid grid-cols-2 gap-2">
                  <button type="button" onClick={() => filesInputRef.current?.click()}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm font-medium text-gray-700 hover:bg-gray-50">
                    📄 Choose Files
                  </button>
                  <button type="button" onClick={() => folderInputRef.current?.click()}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm font-medium text-gray-700 hover:bg-gray-50">
                    📁 Choose Folder
                  </button>
                </div>
                <p className="text-xs text-gray-400 mt-1">Pick individual photos, or an entire folder — either can be used more than once to add more. Duplicate filenames are kept and renamed automatically.</p>

                {localFiles.length === 0 ? (
                  <div className="mt-2 text-xs text-gray-500 bg-gray-50 border border-dashed border-gray-300 rounded-lg px-3 py-2">
                    Nothing selected yet.
                    {skippedCount > 0 && <span className="text-amber-700"> {skippedCount} non-image file{skippedCount === 1 ? " was" : "s were"} skipped.</span>}
                  </div>
                ) : (
                  <div className="mt-2 border border-green-300 bg-green-50 rounded-lg overflow-hidden">
                    <div className="px-3 py-2">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold text-green-800">✓ Ready to upload</span>
                        <button type="button" className="text-xs text-green-700 hover:underline" onClick={clearLocalFiles}>Clear all</button>
                      </div>
                      <div className="flex flex-wrap gap-1.5 mt-1.5">
                        {folderNames.map((name) => (
                          <span key={name} className="text-xs bg-white border border-green-300 text-green-800 rounded px-1.5 py-0.5">📁 Folder: <strong>{name}</strong></span>
                        ))}
                        {looseFileCount > 0 && (
                          <span className="text-xs bg-white border border-green-300 text-green-800 rounded px-1.5 py-0.5">📄 Files: <strong>{looseFileCount}</strong> chosen individually</span>
                        )}
                      </div>
                      <p className="text-sm text-green-900 mt-1.5">
                        <strong>{localFiles.length}</strong> photo{localFiles.length === 1 ? "" : "s"} detected and will be uploaded.
                        {skippedCount > 0 && <span className="text-amber-700"> ({skippedCount} non-image file{skippedCount === 1 ? "" : "s"} skipped)</span>}
                      </p>
                    </div>
                    <ul className="max-h-32 overflow-y-auto divide-y divide-green-100 border-t border-green-200 bg-white">
                      {localFiles.map((file, index) => (
                        <li key={`${relativePath(file) || file.name}-${index}`} className="flex items-center justify-between text-xs text-gray-600 px-3 py-1">
                          <span className="truncate">{relativePath(file) || file.name}</span>
                          <button type="button" className="text-red-500 hover:underline ml-2 shrink-0" onClick={() => removeLocalFile(index)}>Remove</button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </>
          )}
          <div>
            <label className="block text-sm text-gray-600 mb-1">Privacy mask for declined participants</label>
            <div className="flex flex-wrap gap-2" role="group" aria-label="Privacy mask style">
              {[
                { id: "blur", label: "Blur (default)", preview: <span className="w-10 h-10 rounded-full bg-gradient-to-br from-gray-300 to-gray-500 blur-[3px]" /> },
                ...maskChoices.map((c) => ({ id: c.id, label: c.label, preview: <img src={c.data_url} alt="" className="w-10 h-10" /> })),
                { id: "image", label: "Custom PNG", preview: maskDataUrl
                  ? <img src={maskDataUrl} alt="" className="w-10 h-10 object-contain" />
                  : <span className="w-10 h-10 rounded border border-dashed border-gray-300 flex items-center justify-center text-gray-400">PNG</span> },
              ].map(({ id, label, preview }) => {
                const selected = maskStyle === id;
                return (
                  <button key={id} type="button" aria-pressed={selected} title={label} onClick={() => setMaskStyle(id)}
                    className={"flex flex-col items-center gap-1 w-20 p-2 rounded-lg border text-xs text-gray-700 " + (selected ? "border-blue-500 bg-blue-50 ring-2 ring-blue-200" : "border-gray-200 hover:bg-gray-50")}>
                    {preview}
                    <span className="text-center leading-tight">{label}</span>
                  </button>
                );
              })}
            </div>
            {maskStyle === "image" && (
              <div className="mt-2">
                <Input type="file" accept="image/png" aria-label="Custom mask PNG" onChange={(e) => pickMask(e.target.files?.[0])} />
                <p className="text-xs text-gray-400 mt-1">PNG, up to 2 MB and 2048×2048 px, at least 30% opaque.</p>
              </div>
            )}
            <p className="text-xs text-gray-400 mt-1">Declined faces are always blurred first; an emoji or image is placed on top, so transparent areas still show only blur.</p>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">MEDIA logo (optional PNG)</label>
            <Input type="file" accept="image/png" onChange={(e) => pickLogo(e.target.files?.[0])} />
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
          {uploadPercent !== null && (
            <div className="bg-indigo-50 border border-indigo-200 rounded-lg px-3 py-2">
              <div className="flex items-center justify-between text-sm text-indigo-900">
                <span>Uploading {localFiles.length} photo{localFiles.length === 1 ? "" : "s"}…</span>
                <span className="font-semibold">{uploadPercent}%</span>
              </div>
              <div className="h-2 bg-indigo-100 rounded mt-1.5 overflow-hidden">
                <div className="h-full bg-indigo-600 transition-all" style={{ width: `${uploadPercent}%` }} />
              </div>
              {uploadPercent === 100 && <p className="text-xs text-indigo-700 mt-1">Upload complete — the server is accepting the files…</p>}
            </div>
          )}
          {successMessage && <div className="text-sm text-green-800 bg-green-50 border border-green-300 rounded-lg px-3 py-2">{successMessage}</div>}
          {error && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</div>}
        </div>
        <div className="flex justify-end gap-2 mt-5 shrink-0 border-t border-gray-100 pt-4">
          <Button variant="secondary" onClick={onClose} disabled={loading}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={loading}>
            {successMessage ? "Done" : uploadPercent !== null ? `Uploading ${uploadPercent}%` : loading ? "Starting..." : "Start Processing"}
          </Button>
        </div>
      </div>
    </div>
  );
}
