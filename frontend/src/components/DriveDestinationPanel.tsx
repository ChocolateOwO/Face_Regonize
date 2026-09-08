import { useState } from "react";
import { apiPostJson, ApiError } from "../api/client";
import { Button, Input } from "./ui";

interface Resolved {
  folder_id: string;
  folder_name: string;
}

export default function DriveDestinationPanel({
  batchId,
  status,
  currentStage,
  driveError,
  processedFolderUrl,
}: {
  batchId: string;
  status: string;
  currentStage: string;
  driveError: string | null;
  processedFolderUrl: string | null;
}) {
  const [folderUrl, setFolderUrl] = useState("");
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState("");

  async function checkFolder() {
    setCheckError("");
    setResolved(null);
    if (!folderUrl.trim()) return setCheckError("Paste a Google Drive folder URL.");
    setChecking(true);
    try {
      const res = await apiPostJson(`/api/photo-batches/${batchId}/drive-destination/validate`, {
        folder_url: folderUrl.trim(),
      });
      setResolved({ folder_id: res.folder_id, folder_name: res.folder_name });
    } catch (err) {
      setCheckError(err instanceof ApiError ? err.message : "Could not check that folder.");
    } finally {
      setChecking(false);
    }
  }

  async function upload() {
    setStartError("");
    setStarting(true);
    try {
      await apiPostJson(`/api/photo-batches/${batchId}/drive-upload`, {
        folder_url: folderUrl.trim(),
        upload_people: true,
        upload_media: true,
      });
    } catch (err) {
      setStartError(err instanceof ApiError ? err.message : "Could not start the upload.");
    } finally {
      setStarting(false);
    }
  }

  // "syncing_drive" is the legacy automatic-mirror status — shown the same
  // way as the new "uploading" so an older batch still renders sensibly.
  if (status === "uploading" || status === "syncing_drive") {
    return (
      <div>
        <h2 className="font-semibold text-gray-900 mb-1">Google Drive</h2>
        <p className="text-sm text-indigo-700">Uploading…</p>
        {currentStage && <p className="text-xs text-gray-500 mt-1">{currentStage}</p>}
      </div>
    );
  }

  if (status === "upload_failed") {
    return (
      <div>
        <h2 className="font-semibold text-gray-900 mb-1">Google Drive</h2>
        <p className="text-sm text-red-700 font-medium">Upload failed</p>
        {driveError && <p className="text-xs text-red-600 mt-1">{driveError}</p>}
        {startError && <p className="text-xs text-red-600 mt-1">{startError}</p>}
        {resolved ? (
          <Button className="mt-2" variant="secondary" disabled={starting} onClick={upload}>
            {starting ? "Retrying…" : "Retry Upload"}
          </Button>
        ) : (
          <DestinationForm
            folderUrl={folderUrl}
            setFolderUrl={setFolderUrl}
            checking={checking}
            checkError={checkError}
            onCheck={checkFolder}
            resolved={resolved}
            starting={starting}
            onUpload={upload}
          />
        )}
      </div>
    );
  }

  return (
    <div>
      <h2 className="font-semibold text-gray-900 mb-1">Google Drive</h2>
      {processedFolderUrl ? (
        <p className="text-sm text-green-700 mb-2">
          ✓ Uploaded —{" "}
          <a href={processedFolderUrl} target="_blank" rel="noreferrer" className="underline">
            Open in Google Drive →
          </a>
        </p>
      ) : (
        <p className="text-sm text-gray-500 mb-2">Not uploaded</p>
      )}
      <DestinationForm
        folderUrl={folderUrl}
        setFolderUrl={setFolderUrl}
        checking={checking}
        checkError={checkError}
        onCheck={checkFolder}
        resolved={resolved}
        starting={starting}
        onUpload={upload}
      />
      {startError && <p className="text-xs text-red-600 mt-1">{startError}</p>}
    </div>
  );
}

function DestinationForm({
  folderUrl,
  setFolderUrl,
  checking,
  checkError,
  onCheck,
  resolved,
  starting,
  onUpload,
}: {
  folderUrl: string;
  setFolderUrl: (v: string) => void;
  checking: boolean;
  checkError: string;
  onCheck: () => void;
  resolved: Resolved | null;
  starting: boolean;
  onUpload: () => void;
}) {
  return (
    <div className="space-y-2">
      <label className="block text-xs text-gray-600">Destination Folder</label>
      <Input placeholder="Paste Google Drive folder URL" value={folderUrl} onChange={(e) => setFolderUrl(e.target.value)} />
      {checkError && <p className="text-xs text-red-600">{checkError}</p>}
      {!resolved && (
        <Button variant="secondary" disabled={checking} onClick={onCheck}>
          {checking ? "Checking…" : "Check Folder"}
        </Button>
      )}
      {resolved && (
        <>
          <p className="text-sm text-green-700">✓ {resolved.folder_name}</p>
          <Button disabled={starting} onClick={onUpload}>
            {starting ? "Starting…" : "Upload to Google Drive"}
          </Button>
        </>
      )}
    </div>
  );
}
