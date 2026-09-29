import { useEffect, useRef, useState } from "react";
import { apiGet, apiPostJson } from "../api/client";
import { type ScanSettings, scanSettingsError } from "../api/scanSettings";
import ScanSettingsFields from "./ScanSettingsFields";
import { Button } from "./ui";

const API = "/api/local-video-experiment/drive";
interface Account { display_name: string; email: string; account_id: string }
interface Limits { max_bytes: number; max_duration_seconds: number; max_files: number; disk_reserve_bytes: number; containers: string[] }
interface AccountState { connected: boolean; read_access: boolean; account: Account | null; reason?: string; message?: string; limits?: Limits }
interface DriveFile { file_id: string; filename: string; bytes: number | string; mime_type?: string; supported: boolean; error?: string; duration_seconds?: number; width?: number; height?: number; modified_time?: string }
interface Folder { folder_id: string; folder_name?: string; account: Account; files: DriveFile[]; message: string; outcome: "videos" | "unsupported" | "empty" | "no_files"; subfolder_count: number; limits: Limits }
const message = (error: unknown) => error instanceof Error ? error.message : "Drive request failed.";

export default function DriveVideoBatchPanel({ settings, onSettings, disabled, onStarted }: {
  settings: ScanSettings; onSettings: (value: ScanSettings) => void; disabled: boolean; onStarted: (state: unknown) => void;
}) {
  const [account, setAccount] = useState<AccountState | null>(null);
  const [link, setLink] = useState("");
  const [name, setName] = useState("");
  const [folder, setFolder] = useState<Folder | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lastAccount = useRef<string | null>(null);
  const connectionTimer = useRef<number | null>(null);
  const connectionPopup = useRef<Window | null>(null);
  function stopWatching() {
    if (connectionTimer.current != null) window.clearInterval(connectionTimer.current);
    connectionTimer.current = null;
  }
  useEffect(() => {
    const controller = new AbortController();
    void apiGet(API + "/status", controller.signal).then((state: AccountState) => {
      setAccount(state); lastAccount.current = state.account?.account_id ?? null;
    }).catch(err => { if (!controller.signal.aborted) setError(message(err)); });
    return () => { controller.abort(); stopWatching(); };
  }, []);
  async function refresh() {
    setBusy(true); setError("");
    try {
      const state = await apiGet(API + "/status?refresh=true") as AccountState;
      if (lastAccount.current !== (state.account?.account_id ?? null) || !state.read_access) { setFolder(null); setSelected([]); }
      lastAccount.current = state.account?.account_id ?? null; setAccount(state);
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }
  async function connect() {
    setError("");
    if (connectionPopup.current && !connectionPopup.current.closed) { connectionPopup.current.focus(); return; }
    stopWatching();
    const popup = window.open("about:blank", "reconize-video-drive-connect", "width=700,height=750");
    connectionPopup.current = popup;
    try {
      if (!popup) throw new Error("Allow this site's connection popup, then reconnect Google Drive.");
      // Same OAuth project and registered callback; only video start adds readonly.
      const result = await apiGet(API + "/oauth/start"); popup.location.href = result.auth_url;
      connectionTimer.current = window.setInterval(() => {
        if (popup.closed) { stopWatching(); void refresh(); return; }
        try {
          if (popup.location.origin !== window.location.origin) return;
          const query = new URLSearchParams(popup.location.search);
          if (query.get("drive_connected") === "1") { popup.close(); stopWatching(); void refresh(); }
          else if (query.has("drive_error")) {
            popup.close(); stopWatching(); setError("Google Drive consent did not complete. Reconnect and allow the read-only video permission.");
          }
        } catch { /* Google consent is on another origin. */ }
      }, 1000);
    } catch (err) { popup?.close(); setError(message(err)); }
  }
  async function list() {
    setBusy(true); setError("");
    try {
      const state = await apiPostJson(API + "/folder", { folder_link: link, account_id: account?.account?.account_id }) as Folder;
      setFolder(state); setSelected([]);
      setAccount(current => ({ ...current, connected: true, read_access: true, account: state.account }));
      lastAccount.current = state.account.account_id;
    } catch (err) { setFolder(null); setSelected([]); setError(message(err)); }
    finally { setBusy(false); }
  }
  async function start() {
    if (!folder || !selected.length || !account?.read_access || busy || disabled) return;
    const problem = scanSettingsError(settings);
    if (problem) { setError(problem); return; }
    setBusy(true); setError("");
    try {
      onStarted(await apiPostJson(API + "/batches", { name: name.trim(), folder_link: link, file_ids: selected,
        account_id: folder.account.account_id, scan_settings: { ...settings } }));
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }
  const locked = busy || disabled;
  const selectable = folder?.files.filter(file => file.supported) ?? [];
  const limits = folder?.limits ?? account?.limits;
  return <div>
    <h2 className="mb-2 font-semibold">Google Drive video batch</h2>
    <p className="mb-2 text-sm">Connected Google account: {account?.connected && account.account
      ? account.account.display_name + " (" + account.account.email + ")" : "Not connected or not verified"}</p>
    <div className="mb-3 flex flex-wrap gap-2">
      <Button disabled={locked} onClick={() => void connect()}>{account?.connected ? "Reconnect with read-only access" : "Connect Google Drive"}</Button>
      <Button disabled={locked} onClick={() => void refresh()}>Refresh account</Button>
    </div>
    {!account?.read_access && <p className="mb-3 text-sm text-amber-900">{account?.message ?? "Connect or reconnect once and allow read-only Drive access to list videos from folder links."}</p>}
    <p className="mb-3 text-xs text-gray-600">Video batches request drive.readonly to list and download files accessible to this account. Existing drive.file access is retained for other features; no full Drive write access is requested. After consent, this page refreshes automatically. If needed, close the popup and Refresh account.</p>
    <label className="block text-sm">Google Drive folder link
      <input aria-label="Google Drive folder link" className="my-1 block w-full rounded border p-2" value={link} disabled={locked}
        onChange={event => { setLink(event.target.value); setFolder(null); setSelected([]); setError(""); }} placeholder="https://drive.google.com/drive/folders/..." /></label>
    <Button disabled={locked || !link.trim() || !account?.read_access} onClick={() => void list()}>List videos</Button>
    <p className="my-2 text-xs">Direct children only. Subfolders are not scanned. Supported containers: MP4, MOV, AVI, MKV, WebM, M4V. Generic Drive MIME types are allowed; the actual container and codec are checked during processing. Some codecs may be unsupported.</p>
    {limits && <p className="my-2 text-xs">Maximum {limits.max_files} selected videos, each {(limits.max_bytes / 1024 ** 3).toFixed(0)} GiB, {limits.max_duration_seconds / 3600} hours, 4K pixels, 1–120 FPS. One download/scan at a time; {(limits.disk_reserve_bytes / 1024 ** 3).toFixed(0)} GiB free-space reserve. Original Drive files are never changed.</p>}
    {folder && <>
      <p className="my-2 text-sm" role="status">{folder.message}</p>
      {folder.folder_name && <p className="mb-2 text-sm font-medium">Folder: {folder.folder_name}</p>}
      <Button disabled={locked || !selectable.length} onClick={() => setSelected(selectable.map(file => file.file_id))}>Select all supported videos ({selectable.length})</Button>
      <p className="my-2 text-xs">{selected.length} selected. {folder.subfolder_count} subfolders not scanned.</p>
      <div className="my-2 max-h-72 overflow-auto"><table className="w-full text-left text-sm">
        <thead><tr><th>Select</th><th>Filename</th><th>Available details / validation</th></tr></thead>
        <tbody>{folder.files.map(file => <tr key={file.file_id} className="border-t">
          <td className="p-2"><input type="checkbox" aria-label={"Select " + file.filename} checked={selected.includes(file.file_id)} disabled={locked || !file.supported}
            onChange={event => setSelected(values => event.target.checked ? [...values, file.file_id] : values.filter(id => id !== file.file_id))} /></td>
          <td className="p-2 break-words">{file.filename}</td><td className="p-2">{Number(file.bytes) > 0 ? (Number(file.bytes) / 1024 / 1024).toFixed(1) + " MiB" : "Size unavailable"}
            {file.duration_seconds ? " | " + file.duration_seconds.toFixed(1) + " s" : ""}{file.width && file.height ? " | " + file.width + "×" + file.height : ""}
            {file.mime_type && <span className="block text-xs">Drive type: {file.mime_type}</span>}
            {file.modified_time && <span className="block text-xs">Modified: {file.modified_time}</span>}{file.error && <span className="block text-red-700">{file.error}</span>}</td>
        </tr>)}</tbody></table></div>
    </>}
    <label className="mt-3 block text-sm">Batch name<input aria-label="Video batch name" className="my-1 block w-full rounded border p-2" maxLength={120} value={name} disabled={locked} onChange={event => setName(event.target.value)} /></label>
    <ScanSettingsFields value={settings} onChange={onSettings} disabled={locked} video />
    <Button disabled={locked || !name.trim() || !selected.length || selected.length > (limits?.max_files ?? 32) || !folder || !account?.read_access || !!scanSettingsError(settings)} onClick={() => void start()}>
      {busy ? "Working…" : "Start selected videos as one batch"}</Button>
    <p className="mt-2 text-xs text-gray-600">Listing and selection do not download or recognize. Start freezes these settings for every source. Temporary videos are deleted after each source; counts and one preview per matched person remain.</p>
    {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
  </div>;
}
