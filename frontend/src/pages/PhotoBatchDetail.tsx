import { memo, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiDelete, apiGet, apiPostJson, apiPutJson, fileUrl, ApiError } from "../api/client";
import { Badge, Button, Card, EmptyState, PageHeader, Spinner , Pagination} from "../components/ui";
import PhotoBatchDownload from "../components/PhotoBatchDownload";
import DriveDestinationPanel from "../components/DriveDestinationPanel";
import BoxEditor from "../components/BoxEditor";
import ImageViewer from "../components/ImageViewer";
import ExportSelectionPanel, { DEFAULT_EXPORT_SELECTION, type ExportSelectionValue } from "../components/ExportSelectionPanel";

interface Batch {
  id: string;
  label: string;
  // "ready" = local processing finished; every result below is final and
  // previewable. Google Drive upload to a manually-chosen destination is a
  // separate, explicit action from here: ready -> uploading -> completed, or
  // -> upload_failed. "syncing_drive" is the old automatic-mirror status,
  // kept only for batches created before this flow existed.
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
  // How long the most recent processing run took. Null while running, and
  // null for batches that finished before the timestamps were persisted.
  elapsed_seconds: number | null;
  // Backend-owned: true while anything is actually happening to this batch
  // (processing, retry, or a Drive upload). Drives the poll rate.
  live?: boolean;
  local_status?: string;
  drive_status?: string;
  total_photos: number;
  processed_photos: number;
  faces_detected: number;
  faces_recognized: number;
  faces_unknown: number;
  consented_faces: number;
  not_consented_faces: number;
  blurred_faces: number;
  recognized_photos: number;
  ambience_photos: number;
  review_photos: number;
  failed_photos: number;
  last_error: string | null;
  drive_failed_photos: number;
  drive_error: string | null;
  retention_days: number;
  retention_start_at: string;
  delete_at: string;
  source_folder_url: string | null;
  processed_folder_url: string | null;
  media_folder_url: string | null;
  ambience_folder_url: string | null;
  review_folder_url: string | null;
}

interface Participant {
  person_id: string;
  participant_id: string;
  first_name: string;
  last_name: string;
  photo_count: number;
  folder_url: string | null;
}

interface Photo {
  id: string;
  filename: string;
  classification: string;
  faces_total: number;
  faces_matched: number;
  faces_unknown: number;
  original_path: string;
  media_path: string | null;
  // The 320px grid tile. Null only when there is nothing safe to derive it
  // from; the grid then falls back to MEDIA, never to ORIGINAL.
  thumbnail_path: string | null;
  drive_upload_status: string;
  drive_error: string | null;
}

type Tab = "people" | "ambience" | "media";

/** Phase G4 — the backend sends null while it warms up (too few photos for a
 *  stable average), which is a different thing from having no estimate. */
function formatEta(seconds: number | null, estimating: boolean): string {
  if (seconds === null) return estimating ? "Estimating…" : "—";
  if (seconds < 60) return `about ${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `about ${hours}h ${minutes % 60}m`;
}

/** Phase G4 — the FINAL duration, so unlike the ETA it is exact, not "about".
 *  Seconds are kept alongside minutes because a small batch finishing in
 *  "2 min" reads as far vaguer than "2 min 34 sec". */
function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds} sec`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes} min ${rest} sec` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} hr ${minutes % 60} min`;
}

const LIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 15000;
// Right after Resume the backend is already working, but at the 2 s cadence the
// page can sit on the old count for a whole tick after the first photo lands.
// Poll fast just until the count actually moves, then go back to normal.
const RESUME_POLL_MS = 500;
const RESUME_POLL_WINDOW_MS = 10000;

/** Shallow compare of the batch payload. Every field is a primitive, so this
 *  is ~45 `===` checks per tick — cheap, and it cannot miss a rendered field
 *  the way a hand-picked list could. An identical response keeps the previous
 *  object, so React bails out and nothing re-renders. */
function sameBatch(a: Batch, b: Batch): boolean {
  const ra = a as unknown as Record<string, unknown>;
  const rb = b as unknown as Record<string, unknown>;
  const keys = new Set([...Object.keys(ra), ...Object.keys(rb)]);
  for (const k of keys) if (ra[k] !== rb[k]) return false;
  return true;
}

/** One participant card. Memoized, and given the participant's id rather than
 *  a closure built per card, so a parent re-render cannot re-render all of
 *  them. */
const ParticipantCard = memo(function ParticipantCard({
  participant: p,
  onSelect,
}: {
  participant: Participant;
  onSelect: (personId: string) => void;
}) {
  return (
    <div className="border border-gray-200 rounded-lg p-3 hover:border-indigo-400">
      <button onClick={() => onSelect(p.person_id)} className="text-left w-full">
        <div className="font-medium text-gray-900">
          {p.participant_id} {p.first_name} {p.last_name}
        </div>
        <div className="text-xs text-gray-500">{p.photo_count} photo(s) — view</div>
      </button>
      {p.folder_url && (
        <a
          href={p.folder_url}
          target="_blank"
          rel="noreferrer"
          className="inline-block mt-2 text-xs text-indigo-600 hover:underline"
        >
          Open in Google Drive →
        </a>
      )}
    </div>
  );
});

/** The People list as its own subtree. Its props are only the list and the
 *  stable selector — no batch/progress object — so status and ETA updates
 *  cannot reach it. */
const ParticipantList = memo(function ParticipantList({
  participants,
  onSelect,
}: {
  participants: Participant[];
  onSelect: (personId: string) => void;
}) {
  return (
    <div className="grid md:grid-cols-3 gap-3">
      {participants.map((p) => (
        <ParticipantCard key={p.person_id} participant={p} onSelect={onSelect} />
      ))}
    </div>
  );
});

/** One grid tile.
 *
 *  Phase I4/J1 — renders the 320px thumbnail, not the ~5 MB artifact: a
 *  50-photo page used to pull ~270 MB and decode full 3600x2400 frames, which
 *  is what made paging and 50/100-per-page stutter.
 *
 *  The fallback chain deliberately ends at MEDIA, the finalized
 *  privacy-rendered copy, and never at ORIGINAL — a declined participant's
 *  face is masked in every grid view, not only the Media tab.
 *
 *  Memoized so re-renders of the page (the 2s status poll, pagination state)
 *  do not re-render every tile.
 */
const PhotoTile = memo(function PhotoTile({ photo, onOpen }: { photo: Photo; onOpen?: (photoId: string) => void }) {
  const src = photo.thumbnail_path || photo.media_path;
  const canOpen = !!onOpen && !!photo.media_path;
  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => canOpen && onOpen?.(photo.id)}
        disabled={!canOpen}
        title={canOpen ? "View full size" : undefined}
        className="block w-full disabled:cursor-default"
      >
      {src ? (
        <img
          src={fileUrl(src)}
          loading="lazy"
          decoding="async"
          alt={photo.filename}
          className="w-full aspect-video object-cover bg-gray-50"
        />
      ) : (
        <div className="w-full aspect-video bg-gray-50" />
      )}
      </button>
      <div className="p-2">
        <div className="text-xs text-gray-500 truncate">{photo.filename}</div>
        {photo.drive_upload_status === "failed" && (
          <div className="text-[10px] text-red-600 mt-0.5" title={photo.drive_error || ""}>
            Drive upload failed
          </div>
        )}
      </div>
    </div>
  );
});

export default function PhotoBatchDetail() {
  const { id } = useParams();
  const [batch, setBatch] = useState<Batch | null>(null);
  const [tab, setTab] = useState<Tab>("people");
  const [participants, setParticipants] = useState<Participant[] | null>(null);
  const [selectedPerson, setSelectedPerson] = useState<Participant | null>(null);
  const [photos, setPhotos] = useState<Photo[] | null>(null);
  const [photoTotal, setPhotoTotal] = useState(0);
  const [photoPage, setPhotoPage] = useState(1);
  const [photoPageSize, setPhotoPageSize] = useState(50);
  const [showRetention, setShowRetention] = useState(false);
  // Phase D1 — which photo the box editor is open on, and a counter that
  // refetches the grid after a save (its thumbnail URL changes).
  const [editorPhotoId, setEditorPhotoId] = useState<string | null>(null);
  const [photoReload, setPhotoReload] = useState(0);
  // Pause / Resume, and a separate Delete.
  const navigate = useNavigate();
  const [pauseRequested, setPauseRequested] = useState(false);
  const [resumeRequested, setResumeRequested] = useState(false);
  useEffect(() => {
    // Clicks stay disabled only until the backend reports the next state.
    if (batch?.status !== "processing" && batch?.status !== "pending") setPauseRequested(false);
    if (batch?.status !== "paused") setResumeRequested(false);
  }, [batch?.status]);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const openEditor = useCallback((photoId: string) => setEditorPhotoId(photoId), []);
  // Phase O — one selection for both Download and the Drive upload.
  const [exportSelection, setExportSelection] = useState<ExportSelectionValue>(DEFAULT_EXPORT_SELECTION);
  // Phase J2 — the full-resolution viewer (MEDIA only) opens from a tile;
  // its toolbar hands off to the box editor.
  const [viewerIndex, setViewerIndex] = useState<number | null>(null);
  const openViewer = useCallback((photoId: string) => {
    const index = photos?.findIndex((p) => p.id === photoId) ?? -1;
    if (index >= 0) setViewerIndex(index);
  }, [photos]);
  // The page used to poll every 2 s forever and commit a full re-render on
  // every tick — even for a batch that had long finished. With 82 participant
  // cards that measured as an ~80 ms long task per tick under CPU load, which
  // is the People-view scroll stutter. Now the backend says whether anything
  // is live: poll fast only then, fall back to a slow heartbeat otherwise (so a
  // Drive upload started from another tab never goes stale), and never
  // re-render for a response that changed nothing.
  // Compared against a ref, not inside a setState updater: scheduling an
  // update with an identical value still makes React render this component
  // once before bailing out. Skipping the call entirely means an unchanged
  // heartbeat costs nothing at all.
  const lastBatch = useRef<Batch | null>(null);
  const loadBatch = useCallback(() => {
    if (!id) return;
    apiGet(`/api/photo-batches/${id}`).then((next: Batch) => {
      if (lastBatch.current && sameBatch(lastBatch.current, next)) return;
      lastBatch.current = next;
      setBatch(next);
    });
  }, [id]);

  // Resume's fast window: the count this batch had when Resume was pressed,
  // and the moment the window expires. Both are cleared as soon as either
  // condition is met, so exactly one interval is ever running.
  const progressAtResume = useRef<number | null>(null);
  const [fastPollUntil, setFastPollUntil] = useState<number | null>(null);
  useEffect(() => {
    // A different batch (or leaving the page) never inherits the fast window.
    setFastPollUntil(null);
    progressAtResume.current = null;
  }, [id]);
  useEffect(() => {
    if (fastPollUntil === null) return;
    const moved = progressAtResume.current !== null && batch !== null
      && batch.processed_photos > progressAtResume.current;
    const remaining = fastPollUntil - Date.now();
    if (moved || remaining <= 0) {
      setFastPollUntil(null);
      return;
    }
    const timer = window.setTimeout(() => setFastPollUntil(null), remaining);
    return () => window.clearTimeout(timer);
  }, [fastPollUntil, batch]);

  const live = batch?.live ?? true;
  useEffect(() => {
    loadBatch();
  }, [loadBatch]);
  useEffect(() => {
    const period = fastPollUntil !== null ? RESUME_POLL_MS : live ? LIVE_POLL_MS : IDLE_POLL_MS;
    const timer = window.setInterval(loadBatch, period);
    return () => window.clearInterval(timer);
  }, [loadBatch, live, fastPollUntil]);

  useEffect(() => {
    if (!id) return;
    apiGet(`/api/photo-batches/${id}/participants`).then(setParticipants);
  }, [id, batch?.status]);

  // Stable across status ticks: it changes only when the participant list
  // itself does, so the memoized cards are not re-rendered by polling.
  const selectParticipant = useCallback(
    (personId: string) => setSelectedPerson(participants?.find((p) => p.person_id === personId) ?? null),
    [participants],
  );

  // Web preview data — read from local storage paths via /api/files, so this
  // works regardless of whether the Google Drive mirror succeeded.
  useEffect(() => {
    if (!id) return;
    if (tab === "people" && !selectedPerson) {
      setPhotos(null);
      return;
    }
    // Phase I4 — the endpoint is paginated now and returns
    // {items,total,page,page_size} instead of a bare array.
    const params = new URLSearchParams({ page: String(photoPage), page_size: String(photoPageSize) });
    if (selectedPerson) params.set("person_id", selectedPerson.person_id);
    else if (tab !== "media") params.set("classification", tab);
    // Cancel the previous page's request. Paging quickly used to leave several
    // in flight, and whichever finished last won — so the grid could settle on
    // a page the user had already left.
    const controller = new AbortController();
    apiGet(`/api/photo-batches/${id}/photos?${params}`, controller.signal)
      .then((res) => {
        setPhotos(res.items);
        setPhotoTotal(res.total);
      })
      .catch((e) => {
        if ((e as Error)?.name !== "AbortError") throw e;
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, tab, selectedPerson, batch?.status, photoPage, photoPageSize, photoReload]);

  // A different tab or participant is a different result set — staying on
  // page 7 of the previous one would show an empty grid.
  useEffect(() => { setPhotoPage(1); }, [tab, selectedPerson]);

  if (!batch) return <Spinner label="Loading batch..." />;

  const progressPct = batch.total_photos > 0 ? Math.round((batch.processed_photos / batch.total_photos) * 100) : 0;

  // Local results are finished and safe to show from the moment phase 1 ends
  // ("ready"), whether or not a Drive upload has since been started, failed,
  // or succeeded. "syncing_drive"/"completed" also cover batches created
  // under the old automatic-mirror flow.
  const localDone = ["ready", "uploading", "upload_failed", "syncing_drive", "completed"].includes(batch.status);

  async function pauseProcessing() {
    if (!batch) return;
    if (!window.confirm("Pause processing? Completed work will be kept. You can resume this batch later.")) return;
    setPauseRequested(true);
    setActionError("");
    try {
      await apiPostJson(`/api/photo-batches/${batch.id}/pause`, {});
      loadBatch();
    } catch (err) {
      setPauseRequested(false);
      setActionError(err instanceof ApiError ? err.message : "Could not pause processing.");
    }
  }

  async function resumeProcessing() {
    if (!batch) return;
    setResumeRequested(true);
    setActionError("");
    try {
      await apiPostJson(`/api/photo-batches/${batch.id}/resume`, {});
      progressAtResume.current = batch.processed_photos;
      setFastPollUntil(Date.now() + RESUME_POLL_WINDOW_MS);
      loadBatch();
    } catch (err) {
      setResumeRequested(false);
      setActionError(err instanceof ApiError ? err.message : "Could not resume processing.");
    }
  }

  async function deleteThisBatch() {
    if (!batch) return;
    if (!window.confirm("Delete this batch permanently?\n\nThis removes the batch's photos, processed outputs and records from this computer. Participants, attendance, consent history, upload history and other batches are not affected. This cannot be undone.")) return;
    setDeleteBusy(true);
    setActionError("");
    try {
      await apiDelete(`/api/photo-batches/${batch.id}`);
      navigate("/photo-batches");
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not delete the batch.");
      loadBatch();
    } finally {
      setDeleteBusy(false);
    }
  }

  const exportParticipants = participants
    ? participants.map((p) => ({ person_id: p.person_id, label: `${p.participant_id} ${p.first_name} ${p.last_name}`.trim() }))
    : null;

  const statusLabel =
    batch.status === "ready" ? "local processing complete"
    : batch.status === "uploading" || batch.status === "syncing_drive" ? "uploading to Google Drive"
    : batch.status === "upload_failed" ? "Google Drive upload failed"
    : batch.status === "pausing" ? "pausing…"
    : batch.status === "paused" ? "processing paused"
    : batch.status === "resuming" ? "resuming…"
    : batch.status === "cancelled" ? "processing stopped"
    : batch.status;

  return (
    <div>
      <PageHeader
        title={batch.label}
        subtitle={`Batch status: ${statusLabel}`}
        action={
          <div className="flex items-start gap-2">
          {batch.can_pause && (
            <Button variant="secondary" disabled={pauseRequested} onClick={pauseProcessing}>
              {pauseRequested ? "Pausing…" : "Pause processing"}
            </Button>
          )}
          {batch.status === "pausing" && !batch.can_pause && <Button variant="secondary" disabled>Pausing…</Button>}
          {batch.status === "resuming" && <Button disabled>Resuming…</Button>}
          {batch.can_resume && (
            <Button disabled={resumeRequested} onClick={resumeProcessing}>
              {resumeRequested ? "Resuming…" : "Resume processing"}
            </Button>
          )}
          {(batch.status === "paused" || batch.can_delete) && (
            <span title={batch.can_delete ? undefined : "Something is still using this batch — try again in a moment."}>
              <Button variant="danger" disabled={deleteBusy || !batch.can_delete} onClick={deleteThisBatch}>
                {deleteBusy ? "Deleting…" : "Delete batch"}
              </Button>
            </span>
          )}
          <PhotoBatchDownload id={batch.id} ready={batch.download_ready} status={batch.status} selection={exportSelection} />
          <Link to="/photo-batches">
            <Button variant="secondary">Back to Event Photos</Button>
          </Link>
          </div>
        }
      />

      {actionError && (
        <div role="alert" className="mb-4 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
          {actionError}
        </div>
      )}

      {batch.status === "paused" && (
        <Card className="mb-4 border-amber-200">
          <h2 className="font-semibold text-amber-800 mb-1">Processing paused</h2>
          <p className="text-sm text-gray-600">{batch.current_stage}</p>
          <p className="text-xs text-gray-500 mt-1">
            Completed work is kept. Resume processing continues from here. Delete batch is a separate, permanent action.
          </p>
        </Card>
      )}

      {batch.status === "pausing" && batch.pause_diagnostics?.slow && (
        <Card className="mb-4 border-amber-300">
          <h2 className="font-semibold text-amber-800 mb-1">Pause is taking longer than expected</h2>
          <p className="text-sm text-gray-600">Waiting at: {batch.pause_diagnostics.stage || "—"}</p>
          <p className="text-xs text-gray-500 mt-1">
            Last worker activity:{" "}
            {batch.pause_diagnostics.seconds_since_heartbeat != null ? `${batch.pause_diagnostics.seconds_since_heartbeat} s ago` : "none recorded"}
            {" · "}workers {batch.pause_diagnostics.workers_active ? "still running" : "not running"}.
            {" "}Resume and Delete stay unavailable until the pause completes.
          </p>
        </Card>
      )}

      {(batch.status === "pending" || batch.status === "processing" || batch.status === "pausing"
        || batch.status === "resuming") && (
        <Card className="mb-4">
          <h2 className="font-semibold text-gray-900 mb-3">Event Photo Processing</h2>
          <div className="w-full bg-gray-100 rounded-full h-3 mb-2">
            <div className="bg-indigo-600 h-3 rounded-full transition-all" style={{ width: `${progressPct}%` }} />
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm mb-2">
            <div>
              Photos found: <strong>{batch.total_photos}</strong>
            </div>
            <div>
              Processed: <strong>{batch.processed_photos} / {batch.total_photos}</strong>
            </div>
            <div>
              Progress: <strong>{progressPct}%</strong>
            </div>
            <div>
              Faces detected: <strong>{batch.faces_detected}</strong>
            </div>
            <div>
              Recognized: <strong>{batch.faces_recognized}</strong>
            </div>
            <div>
              Unknown: <strong>{batch.faces_unknown}</strong>
            </div>
            <div>
              Consented faces: <strong>{batch.consented_faces}</strong>
            </div>
            <div>
              Not consented faces: <strong>{batch.not_consented_faces}</strong>
            </div>
          </div>
          {batch.current_stage && <p className="text-xs text-gray-400 mt-2">{batch.current_stage}</p>}
          <p className="text-xs text-gray-500 mt-1">
            Time remaining: <strong>{formatEta(batch.eta_seconds, batch.eta_estimating)}</strong>
          </p>
          <Spinner />
        </Card>
      )}

      {batch.status === "failed" && (
        <Card className="mb-4 border-red-200">
          <h2 className="font-semibold text-red-700 mb-1">Processing Failed</h2>
          <p className="text-sm text-gray-600">{batch.current_stage}</p>
        </Card>
      )}

      {localDone && (
        <>
          <Card className="mb-4">
            <div className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
              <h2 className="font-semibold text-gray-900 text-lg">Local Processing Complete ✓</h2>
              {/* Backend-authoritative: computed from persisted timestamps, so
                  it survives a refresh. Batches processed before this shipped
                  have no timestamps and simply show nothing. */}
              {batch.elapsed_seconds !== null && batch.elapsed_seconds !== undefined && (
                <span className="text-sm text-gray-500">
                  Completed in <strong className="text-gray-700">{formatDuration(batch.elapsed_seconds)}</strong>
                </span>
              )}
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <Stat label="Total Photos" value={batch.total_photos} />
              <Stat label="Recognized Photos" value={batch.recognized_photos} tone="good" />
              <Stat label="Ambience Photos" value={batch.ambience_photos} />
              <Stat label="Recognized Faces" value={batch.faces_recognized} tone="good" />
              <Stat label="Unknown Faces" value={batch.faces_unknown} tone="warn" />
              <Stat label="Blurred Faces" value={batch.blurred_faces} tone="bad" />
              <Stat label="Consented Faces" value={batch.consented_faces} tone="good" />
            </div>
            {batch.failed_photos > 0 && (
              <div className="mt-4 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                {batch.failed_photos} photo(s) failed to process and were skipped.
                {batch.last_error && <div className="text-xs text-amber-600 mt-1">Last error: {batch.last_error}</div>}
              </div>
            )}
          </Card>

          <Card className="mb-4">
            <ExportSelectionPanel value={exportSelection} onChange={setExportSelection} participants={exportParticipants} />
            <div className="border-t border-gray-100 my-3" />
            <DriveDestinationPanel
              batchId={batch.id}
              status={batch.status}
              currentStage={batch.current_stage}
              driveError={batch.drive_error}
              processedFolderUrl={batch.processed_folder_url}
              selection={exportSelection}
            />
          </Card>
        </>
      )}

      <RetentionPanel batch={batch} onOpen={() => setShowRetention(true)} />

      <Card className="p-0 overflow-hidden">
        <div className="flex border-b border-gray-100">
          {(["people", "ambience", "media"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => {
                setTab(t);
                setSelectedPerson(null);
              }}
              className={`px-4 py-3 text-sm font-medium capitalize ${
                tab === t ? "border-b-2 border-indigo-600 text-indigo-600" : "text-gray-500 hover:text-gray-700"
              }`}
            >
              {t === "people" ? "People" : t}
            </button>
          ))}
        </div>

        <div className="p-4">
          {tab === "people" && !selectedPerson && (
            <>
              {!participants ? (
                <Spinner />
              ) : participants.length === 0 ? (
                <EmptyState>No recognized participants yet.</EmptyState>
              ) : (
                <ParticipantList participants={participants} onSelect={selectParticipant} />
              )}
            </>
          )}

          {(tab !== "people" || selectedPerson) && (
            <>
              {selectedPerson && (
                <button onClick={() => setSelectedPerson(null)} className="text-sm text-indigo-600 hover:underline mb-3">
                  ← Back to participants
                </button>
              )}
              {!photos ? (
                <Spinner />
              ) : photos.length === 0 ? (
                <EmptyState>No photos here.</EmptyState>
              ) : (
                <div className="grid md:grid-cols-4 gap-3">
                  {photos.map((p) => (
                    <PhotoTile key={p.id} photo={p} onOpen={openViewer} />
                  ))}
                </div>
              )}
              {photos && photoTotal > 0 && (
                <div className="-mx-4 mt-2">
                  <Pagination
                    page={photoPage}
                    pageSize={photoPageSize}
                    total={photoTotal}
                    onPageChange={setPhotoPage}
                    onPageSizeChange={(size) => { setPhotoPageSize(size); setPhotoPage(1); }}
                  />
                </div>
              )}
            </>
          )}
        </div>
      </Card>

      {viewerIndex !== null && photos && photos[viewerIndex] && (
        <ImageViewer
          photos={photos}
          index={viewerIndex}
          onIndexChange={setViewerIndex}
          onClose={() => setViewerIndex(null)}
          onEdit={(photoId) => {
            setViewerIndex(null);
            openEditor(photoId);
          }}
        />
      )}

      {editorPhotoId && (
        <BoxEditor
          batchId={batch.id}
          photoId={editorPhotoId}
          onClose={() => setEditorPhotoId(null)}
          onSaved={() => setPhotoReload((n) => n + 1)}
        />
      )}

      {showRetention && (
        <RetentionModal
          batchId={batch.id}
          currentDays={batch.retention_days}
          onClose={() => setShowRetention(false)}
          onChanged={() => {
            setShowRetention(false);
            loadBatch();
          }}
        />
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "good" | "warn" | "bad" }) {
  const toneClass = tone === "good" ? "text-green-600" : tone === "warn" ? "text-amber-600" : tone === "bad" ? "text-red-600" : "text-gray-900";
  return (
    <div>
      <div className={`text-2xl font-bold ${toneClass}`}>{value}</div>
      <div className="text-xs text-gray-500">{label}</div>
    </div>
  );
}

function RetentionPanel({ batch, onOpen }: { batch: Batch; onOpen: () => void }) {
  const deleteAt = new Date(batch.delete_at);
  const isActive = deleteAt.getTime() > Date.now();
  return (
    <Card className="mb-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="font-semibold text-gray-900 mb-2">Data Retention</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-1 text-sm">
            <div>
              <span className="text-gray-500">Retention period:</span> <strong>{batch.retention_days} Day{batch.retention_days === 1 ? "" : "s"}</strong>
            </div>
            <div>
              <span className="text-gray-500">Started:</span> {new Date(batch.retention_start_at).toLocaleString()}
            </div>
            <div>
              <span className="text-gray-500">Scheduled deletion:</span> {deleteAt.toLocaleString()}
            </div>
            <div>
              <span className="text-gray-500">Status:</span>{" "}
              <Badge tone={isActive ? "good" : "bad"}>{isActive ? "ACTIVE" : "EXPIRED"}</Badge>
            </div>
          </div>
          <p className="text-xs text-gray-400 mt-2">
            Deletes this app's processing records only. Photos already placed in Google Drive are not affected — remove them there if needed.
          </p>
        </div>
        <Button variant="secondary" onClick={onOpen}>
          Change Retention
        </Button>
      </div>
    </Card>
  );
}

function RetentionModal({
  batchId,
  currentDays,
  onClose,
  onChanged,
}: {
  batchId: string;
  currentDays: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [password, setPassword] = useState("");
  const [days, setDays] = useState(currentDays);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit() {
    setError("");
    if (!password) return setError("Enter your admin password to confirm.");
    setLoading(true);
    try {
      await apiPutJson(`/api/photo-batches/${batchId}/retention`, { password, retention_days: days });
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change retention.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl w-full max-w-sm p-6">
        <h2 className="font-semibold text-lg mb-1">Change Retention Period</h2>
        <p className="text-xs text-gray-500 mb-4">Requires your admin password to confirm.</p>
        <div className="space-y-3">
          <div>
            <label className="block text-sm text-gray-600 mb-1">New retention period</label>
            <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm">
              {[1, 2, 3, 4, 5, 6, 7].map((d) => (
                <option key={d} value={d}>
                  {d} Day{d === 1 ? "" : "s"}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">Admin password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
            />
          </div>
          {error && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</div>}
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={loading}>
            {loading ? "Saving..." : "Confirm"}
          </Button>
        </div>
      </div>
    </div>
  );
}
