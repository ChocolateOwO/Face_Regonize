import { useState } from "react";
import { API_BASE, ApiError } from "../api/client";
import { Button } from "./ui";

type FileWriter = { write: (chunk: Uint8Array) => Promise<void>; close: () => Promise<void>; abort: () => Promise<void> };
type SavePicker = (options: { suggestedName: string }) => Promise<{ createWritable: () => Promise<FileWriter> }>;

export default function PhotoBatchDownload({ id, ready, status, stopping = false }: {
  id: string; ready?: boolean; status: string; stopping?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function download() {
    setBusy(true);
    setError("");
    let writer: FileWriter | undefined;
    const controller = new AbortController();
    try {
      const filename = `event-photos-${id}.zip`;
      // Chromium on localhost/HTTPS can write chunks directly to the chosen
      // file. Preserve the user gesture by opening the picker before fetching.
      const picker = (window as unknown as { showSaveFilePicker?: SavePicker }).showSaveFilePicker;
      const target = picker ? await picker.call(window, { suggestedName: filename }) : undefined;
      const token = localStorage.getItem("token");
      const response = await fetch(`${API_BASE}/api/photo-batches/${id}/download`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {}, signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event("auth:unauthorized"));
        const data = await response.json().catch(() => null);
        throw new ApiError(response.status, data?.detail || response.statusText);
      }
      if (target && response.body) {
        writer = await target.createWritable();
        const reader = response.body.getReader();
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            await writer.write(value);
          }
          await writer.close();
          writer = undefined;
        } finally {
          reader.releaseLock();
        }
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      controller.abort();
      await writer?.abort().catch(() => undefined);
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setError(err instanceof ApiError ? err.message : "Download failed. Please retry.");
      }
    } finally {
      setBusy(false);
    }
  }

  const blocked = stopping || ["pending", "processing", "stopping", "deleting", "cancelled"].includes(status);
  return <span className="inline-flex flex-col items-start gap-1"
    title="Download local SORTED, REVIEW and MEDIA. Available regardless of Google Drive upload status.">
    <Button variant="secondary" disabled={!ready || blocked || busy} onClick={download}>
      {busy ? "Preparing download..." : status === "processing" || status === "pending" ? "Processing..." : "Download ZIP"}
    </Button>
    {error && <span role="alert" className="text-xs text-red-600 max-w-xs text-left">{error}</span>}
  </span>;
}
