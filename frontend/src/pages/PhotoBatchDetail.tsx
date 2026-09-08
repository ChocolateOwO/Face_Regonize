import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiGet, apiPutJson, fileUrl, ApiError } from "../api/client";
import { Badge, Button, Card, EmptyState, PageHeader, Spinner } from "../components/ui";
import PhotoBatchDownload from "../components/PhotoBatchDownload";
import DriveDestinationPanel from "../components/DriveDestinationPanel";

interface Batch {
  id: string;
  label: string;
  // "ready" = local processing finished; every result below is final and
  // previewable. Google Drive upload to a manually-chosen destination is a
  // separate, explicit action from here: ready -> uploading -> completed, or
  // -> upload_failed. "syncing_drive" is the old automatic-mirror status,
  // kept only for batches created before this flow existed.
  status: "pending" | "processing" | "ready" | "uploading" | "upload_failed" | "syncing_drive" | "completed" | "failed";
  download_ready?: boolean;
  current_stage: string;
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
  drive_upload_status: string;
  drive_error: string | null;
}

type Tab = "people" | "ambience" | "review" | "media";

export default function PhotoBatchDetail() {
  const { id } = useParams();
  const [batch, setBatch] = useState<Batch | null>(null);
  const [tab, setTab] = useState<Tab>("people");
  const [participants, setParticipants] = useState<Participant[] | null>(null);
  const [selectedPerson, setSelectedPerson] = useState<Participant | null>(null);
  const [photos, setPhotos] = useState<Photo[] | null>(null);
  const [showRetention, setShowRetention] = useState(false);
  const pollRef = useRef<number | null>(null);

  function loadBatch() {
    if (id) apiGet(`/api/photo-batches/${id}`).then(setBatch);
  }

  useEffect(() => {
    loadBatch();
    pollRef.current = window.setInterval(loadBatch, 2000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (!id) return;
    apiGet(`/api/photo-batches/${id}/participants`).then(setParticipants);
  }, [id, batch?.status]);

  // Web preview data — read from local storage paths via /api/files, so this
  // works regardless of whether the Google Drive mirror succeeded.
  useEffect(() => {
    if (!id) return;
    if (tab === "people" && !selectedPerson) {
      setPhotos(null);
      return;
    }
    if (selectedPerson) {
      apiGet(`/api/photo-batches/${id}/photos?person_id=${selectedPerson.person_id}`).then(setPhotos);
    } else if (tab === "media") {
      apiGet(`/api/photo-batches/${id}/photos`).then(setPhotos);
    } else {
      apiGet(`/api/photo-batches/${id}/photos?classification=${tab}`).then(setPhotos);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, tab, selectedPerson, batch?.status]);

  if (!batch) return <Spinner label="Loading batch..." />;

  const progressPct = batch.total_photos > 0 ? Math.round((batch.processed_photos / batch.total_photos) * 100) : 0;

  // Local results are finished and safe to show from the moment phase 1 ends
  // ("ready"), whether or not a Drive upload has since been started, failed,
  // or succeeded. "syncing_drive"/"completed" also cover batches created
  // under the old automatic-mirror flow.
  const localDone = ["ready", "uploading", "upload_failed", "syncing_drive", "completed"].includes(batch.status);

  const statusLabel =
    batch.status === "ready" ? "local processing complete"
    : batch.status === "uploading" || batch.status === "syncing_drive" ? "uploading to Google Drive"
    : batch.status === "upload_failed" ? "Google Drive upload failed"
    : batch.status;

  return (
    <div>
      <PageHeader
        title={batch.label}
        subtitle={`Batch status: ${statusLabel}`}
        action={
          <div className="flex items-start gap-2">
          <PhotoBatchDownload id={batch.id} ready={batch.download_ready} status={batch.status} />
          <Link to="/photo-batches">
            <Button variant="secondary">Back to Event Photos</Button>
          </Link>
          </div>
        }
      />

      {(batch.status === "pending" || batch.status === "processing") && (
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
            <h2 className="font-semibold text-gray-900 mb-3 text-lg">Local Processing Complete ✓</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <Stat label="Total Photos" value={batch.total_photos} />
              <Stat label="Recognized Photos" value={batch.recognized_photos} tone="good" />
              <Stat label="Ambience Photos" value={batch.ambience_photos} />
              <Stat label="Review Photos" value={batch.review_photos} tone="warn" />
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
            <DriveDestinationPanel
              batchId={batch.id}
              status={batch.status}
              currentStage={batch.current_stage}
              driveError={batch.drive_error}
              processedFolderUrl={batch.processed_folder_url}
            />
          </Card>
        </>
      )}

      <RetentionPanel batch={batch} onOpen={() => setShowRetention(true)} />

      <Card className="p-0 overflow-hidden">
        <div className="flex border-b border-gray-100">
          {(["people", "ambience", "review", "media"] as Tab[]).map((t) => (
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
                <div className="grid md:grid-cols-3 gap-3">
                  {participants.map((p) => (
                    <div key={p.person_id} className="border border-gray-200 rounded-lg p-3 hover:border-indigo-400">
                      <button onClick={() => setSelectedPerson(p)} className="text-left w-full">
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
                  ))}
                </div>
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
                  {photos.map((p) => {
                    // MEDIA tab shows the PDPA-blurred copy; every other view
                    // shows the unmodified original.
                    const src = tab === "media" ? p.media_path || p.original_path : p.original_path;
                    return (
                      <div key={p.id} className="border border-gray-200 rounded-lg overflow-hidden">
                        <img src={fileUrl(src)} loading="lazy" className="w-full aspect-video object-cover bg-gray-50" />
                        <div className="p-2">
                          <div className="text-xs text-gray-500 truncate">{p.filename}</div>
                          {p.drive_upload_status === "failed" && (
                            <div className="text-[10px] text-red-600 mt-0.5" title={p.drive_error || ""}>
                              Drive upload failed
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      </Card>

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
