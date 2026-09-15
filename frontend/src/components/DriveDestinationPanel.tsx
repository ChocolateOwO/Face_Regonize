import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPostJson, apiPostJsonWithHeaders, ApiError } from "../api/client";
import { type ExportSelectionValue, selectionIsEmpty } from "./ExportSelectionPanel";
import { Button } from "./ui";

interface Resolved {
  folder_id: string;
  folder_name: string;
  folder_url: string;
}

interface DriveAccount {
  display_name: string;
  email: string;
  photo_url: string | null;
  // Drive's own permission id: stable and non-secret, never a token.
  account_id: string;
}

interface AccountState {
  connected: boolean;
  account: DriveAccount | null;
  reason: "not_connected" | "reauthorization_required" | "verification_unavailable" | null;
}

interface PickerConfig {
  enabled: boolean;
  connected: boolean;
  missing: string[];
  client_id: string | null;
  api_key: string | null;
  app_id: string | null;
}

const FOLDER_MIME = "application/vnd.google-apps.folder";
// Opening the Picker means a token mint plus Google's own loader; past this the
// controls come back rather than leaving the page stuck on "Refreshing…".
const PICKER_OPEN_TIMEOUT_MS = 15000;
const REFRESH_FAILED = "Google Drive folders could not be refreshed. Try again.";
const ACCOUNT_CHANGED = "The connected Google account changed. Select the destination folder again.";

// Google's Picker loader. Loaded once, on first use only.
let pickerLoad: Promise<void> | null = null;
function loadPickerApi(): Promise<void> {
  if (pickerLoad) return pickerLoad;
  pickerLoad = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://apis.google.com/js/api.js";
    script.async = true;
    script.onload = () => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const gapi = (window as any).gapi;
      if (!gapi) return reject(new Error("Google API did not load."));
      gapi.load("picker", { callback: () => resolve(), onerror: () => reject(new Error("Google Picker did not load.")) });
    };
    script.onerror = () => reject(new Error("Could not reach Google to open the folder picker."));
    document.head.appendChild(script);
  }).catch((e) => {
    pickerLoad = null;
    throw e;
  });
  return pickerLoad;
}

export default function DriveDestinationPanel({
  batchId,
  status,
  currentStage,
  driveError,
  processedFolderUrl,
  selection,
}: {
  batchId: string;
  status: string;
  currentStage: string;
  driveError: string | null;
  processedFolderUrl: string | null;
  // Phase O — the same selection the Download button uses; absent = People + MEDIA as before.
  selection?: ExportSelectionValue;
}) {
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState("");
  const [picker, setPicker] = useState<PickerConfig | null>(null);
  // One Picker at a time: busy covers the whole build (account check, token,
  // loader, construction), so Select / Change / Refresh are all inert until the
  // new dialog is on screen.
  const [pickerBusy, setPickerBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [account, setAccount] = useState<AccountState | null>(null);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const pickerRef = useRef<any>(null);
  const pickerSession = useRef(0);      // every build gets a number; older ones are stale
  const handledSession = useRef(-1);    // one result per session, so repeats are ignored
  const busyRef = useRef(false);        // guards against a second build starting mid-flight
  const pickerAccountId = useRef<string | null>(null);   // account the open Picker acts as
  const selectedAccountId = useRef<string | null>(null); // account the destination was chosen under
  const knownAccountId = useRef<string | null>(null);

  useEffect(() => {
    apiGet("/api/photo-batches/drive-picker/config")
      .then((c) => setPicker(c as PickerConfig))
      .catch(() => setPicker(null));
  }, []);

  // Read once when the panel opens, and again only on a real event (connect,
  // Picker, upload, OAuth error) — never from the batch-progress polling.
  const loadAccount = useCallback(async (force = false): Promise<AccountState> => {
    try {
      const state = await apiGet(`/api/photo-batches/drive-oauth/status${force ? "?refresh=1" : ""}`) as AccountState;
      setAccount(state);
      return state;
    } catch {
      const failed: AccountState = { connected: false, account: null, reason: "verification_unavailable" };
      setAccount(failed);
      return failed;
    }
  }, []);

  useEffect(() => {
    void loadAccount();
  }, [loadAccount]);

  // Hide Google's dialog and drop the instance. Called before building a new
  // one and when the panel goes away, so a replaced Picker can never linger.
  const disposePicker = useCallback(() => {
    const current = pickerRef.current;
    pickerRef.current = null;
    if (!current) return;
    try {
      current.setVisible(false);
    } catch {
      /* already gone */
    }
    try {
      current.dispose();
    } catch {
      /* older Picker builds have no dispose */
    }
  }, []);

  useEffect(() => () => disposePicker(), [disposePicker]);

  useEffect(() => {
    const id = account?.account?.account_id ?? null;
    if (id === null) return;
    if (knownAccountId.current === null) {
      knownAccountId.current = id;
      return;
    }
    if (knownAccountId.current !== id) {
      // A folder grant belongs to the account it was given to; it means nothing
      // for a different one. Start the destination over.
      knownAccountId.current = id;
      pickerSession.current += 1; // anything the old Picker reports is now stale
      disposePicker();
      setResolved(null);
      setCheckError(ACCOUNT_CHANGED);
    }
  }, [account, disposePicker]);

  async function validateFolder(folderId: string) {
    setCheckError("");
    setChecking(true);
    try {
      // The backend re-validates every destination — a Picker selection is
      // never trusted on its own (real folder, not trashed, canAddChildren).
      const res = await apiPostJson(`/api/photo-batches/${batchId}/drive-destination/validate`, { folder_id: folderId });
      if (res.requires_picker_grant) {
        // Only reachable if the grant disappeared between picking and checking.
        setCheckError("Google has not granted Reconize access to that folder. Select it again.");
        return;
      }
      selectedAccountId.current = pickerAccountId.current;
      setResolved({ folder_id: res.folder_id, folder_name: res.folder_name, folder_url: res.folder_url });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Could not check that folder.";
      setCheckError(message);
      if (/expired|not connected|reconnect/i.test(message)) void loadAccount(true);
    } finally {
      setChecking(false);
    }
  }

  async function acceptPicked(session: number, folderId: string) {
    // The Picker acted as one account; accept its answer only while that is
    // still the connected account.
    const state = await loadAccount(true);
    if (session !== pickerSession.current) return;
    if (!state.connected || !state.account || state.account.account_id !== pickerAccountId.current) {
      setResolved(null);
      setCheckError(ACCOUNT_CHANGED);
      return;
    }
    await validateFolder(folderId);
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function onPickerEvent(session: number, google: any, data: any) {
    // A replaced Picker (Refresh) and a second callback for the same pick both
    // land here; neither may touch the destination.
    if (session !== pickerSession.current || handledSession.current === session) return;
    if (data.action === google.picker.Action.PICKED && data.docs?.[0]?.id) {
      handledSession.current = session;
      disposePicker();
      void acceptPicked(session, data.docs[0].id as string);
    } else if (data.action === google.picker.Action.CANCEL) {
      handledSession.current = session;
      disposePicker();
    }
  }

  /** Builds a brand-new Picker — fresh account check, fresh token, fresh views —
   *  and replaces whatever was on screen. `Refresh folders` is the same path, so
   *  a folder shared with the account a moment ago simply shows up. */
  async function openPicker(isRefresh = false) {
    if (!picker?.enabled || busyRef.current) return;
    busyRef.current = true;
    setPickerBusy(true);
    setRefreshing(isRefresh);
    setCheckError("");
    const session = ++pickerSession.current;
    let finished = false;
    const restore = () => {
      busyRef.current = false;
      setPickerBusy(false);
      setRefreshing(false);
    };
    const timer = window.setTimeout(() => {
      if (finished || session !== pickerSession.current) return;
      finished = true;
      disposePicker();
      setCheckError(REFRESH_FAILED);
      restore();
    }, PICKER_OPEN_TIMEOUT_MS);

    try {
      const state = await loadAccount(true);
      if (finished || session !== pickerSession.current) return;
      if (!state.connected || !state.account) {
        setCheckError(state.reason === "reauthorization_required"
          ? "The Google Drive connection expired. Reconnect Google Drive, then select the folder again."
          : "Could not verify the connected Google account. Try again in a moment.");
        return;
      }
      pickerAccountId.current = state.account.account_id;

      await loadPickerApi();
      // Short-lived token, held only in this function's scope — never stored.
      const { access_token } = await apiPostJsonWithHeaders("/api/photo-batches/drive-picker/token", {}, {
        "X-Reconize-Picker": "1",
      });
      if (finished || session !== pickerSession.current) return;

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const google = (window as any).google;
      // Folder-only, list mode. Three views so every place a writable folder can
      // live is reachable: the account's own My Drive, folders other people
      // shared with it, and Shared Drives it belongs to.
      const folderView = () => new google.picker.DocsView(google.picker.ViewId.FOLDERS)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setMimeTypes(FOLDER_MIME)
        .setMode(google.picker.DocsViewMode.LIST);
      const myDrive = folderView().setOwnedByMe(true);
      const sharedWithMe = folderView().setOwnedByMe(false);
      const sharedDrives = folderView().setEnableDrives(true);

      disposePicker(); // the stale dialog goes before the new one arrives
      const built = new google.picker.PickerBuilder()
        .addView(myDrive)
        .addView(sharedWithMe)
        .addView(sharedDrives)
        .enableFeature(google.picker.Feature.SUPPORT_DRIVES)
        .setOAuthToken(access_token)
        .setDeveloperKey(picker.api_key)
        .setAppId(picker.app_id)
        .setTitle("Select the destination folder")
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        .setCallback((data: any) => onPickerEvent(session, google, data))
        .build();
      pickerRef.current = built;
      built.setVisible(true);
    } catch (err) {
      if (!finished) {
        disposePicker();
        setCheckError(isRefresh
          ? REFRESH_FAILED
          : err instanceof ApiError ? err.message : err instanceof Error ? err.message : "Could not open the folder picker.");
      }
    } finally {
      if (!finished) {
        finished = true;
        window.clearTimeout(timer);
        restore();
      }
    }
  }

  async function upload() {
    if (!resolved) return;
    if (selection && selectionIsEmpty(selection)) return setStartError("Select at least one output to upload.");
    setStartError("");
    setStarting(true);
    try {
      // The upload runs as the connected account — confirm it is still the one
      // on screen, and the one this folder was selected under, before anything
      // is written to Drive.
      const state = await loadAccount(true);
      if (!state.connected || !state.account) {
        setStartError(state.reason === "reauthorization_required"
          ? "The Google Drive connection expired. Reconnect Google Drive, then upload again."
          : "Could not verify the connected Google account. Try again in a moment.");
        return;
      }
      if (selectedAccountId.current && state.account.account_id !== selectedAccountId.current) {
        setResolved(null);
        setStartError(ACCOUNT_CHANGED);
        return;
      }
      await apiPostJson(`/api/photo-batches/${batchId}/drive-upload`, {
        folder_id: resolved.folder_id,
        upload_people: selection ? selection.sorted : true,
        upload_media: selection ? selection.media : true,
        upload_ambience: selection ? selection.ambience : false,
        person_ids: selection && selection.sorted ? selection.personIds : null,
      });
    } catch (err) {
      setStartError(err instanceof ApiError ? err.message : "Could not start the upload.");
    } finally {
      setStarting(false);
    }
  }

  async function connectDrive() {
    // The same consent flow the Event Photos page uses — one Drive
    // authorization system, not a second one.
    try {
      const res = await apiGet("/api/photo-batches/drive-oauth/start");
      window.location.href = res.auth_url;
    } catch (err) {
      setCheckError(err instanceof ApiError ? err.message : "Could not start the Google Drive connection.");
    }
  }

  const form = (
    <DestinationForm
      account={account}
      onConnect={connectDrive}
      onRetryAccount={() => void loadAccount(true)}
      picker={picker}
      checking={checking}
      pickerBusy={pickerBusy}
      refreshing={refreshing}
      checkError={checkError}
      onSelectFolder={() => void openPicker(false)}
      onRefreshFolders={() => void openPicker(true)}
      resolved={resolved}
      starting={starting}
      onUpload={upload}
    />
  );

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
        {form}
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
      {form}
      {startError && <p className="text-xs text-red-600 mt-1">{startError}</p>}
    </div>
  );
}

function DestinationForm({
  account,
  onConnect,
  onRetryAccount,
  picker,
  checking,
  pickerBusy,
  refreshing,
  checkError,
  onSelectFolder,
  onRefreshFolders,
  resolved,
  starting,
  onUpload,
}: {
  account: AccountState | null;
  onConnect: () => void;
  onRetryAccount: () => void;
  picker: PickerConfig | null;
  checking: boolean;
  pickerBusy: boolean;
  refreshing: boolean;
  checkError: string;
  onSelectFolder: () => void;
  onRefreshFolders: () => void;
  resolved: Resolved | null;
  starting: boolean;
  onUpload: () => void;
}) {
  const busy = pickerBusy || checking;
  const refreshButton = picker?.enabled ? (
    <Button variant="secondary" disabled={busy} onClick={onRefreshFolders}>
      {refreshing ? "Refreshing…" : "Refresh folders"}
    </Button>
  ) : null;

  return (
    <div className="space-y-2">
      <DriveAccountCard account={account} onConnect={onConnect} onRetry={onRetryAccount} />
      <label className="block text-xs text-gray-600">Destination folder (an existing Google Drive folder you can edit)</label>
      {picker?.enabled && !resolved && (
        <div className="flex flex-wrap gap-2">
          <Button disabled={busy} onClick={onSelectFolder}>
            {pickerBusy && !refreshing ? "Opening Google Picker…" : checking ? "Checking…" : "Select folder from Google Drive"}
          </Button>
          {refreshButton}
        </div>
      )}
      {picker && !picker.enabled && (
        <p data-testid="picker-unavailable" className="text-xs text-amber-700">
          {!picker.connected
            ? "Connect Google Drive first to select a destination folder."
            : `Google Picker is not configured — missing: ${picker.missing.join(", ")}. A destination folder cannot be selected until an administrator finishes that setup.`}
        </p>
      )}
      {checkError && <p role="alert" className="text-xs text-red-600">{checkError}</p>}
      {resolved && (
        <div data-testid="selected-destination" className="space-y-1">
          <p className="text-sm text-green-700">✓ {resolved.folder_name}</p>
          <p className="text-xs text-gray-500">
            Source: Google Picker
            {account?.account?.email && <> · {account.account.email}</>}
            {resolved.folder_url && (
              <>
                {" · "}
                <a href={resolved.folder_url} target="_blank" rel="noreferrer" className="underline">
                  Open in Google Drive
                </a>
              </>
            )}
          </p>
          <p className="text-xs text-gray-500">Files are uploaded directly into this folder.</p>
          <div className="flex flex-wrap gap-2">
            <Button disabled={starting || busy} onClick={onUpload}>
              {starting ? "Starting…" : "Upload to Google Drive"}
            </Button>
            <Button variant="secondary" disabled={starting || busy} onClick={onSelectFolder}>
              Change folder
            </Button>
            {refreshButton}
          </div>
        </div>
      )}
    </div>
  );
}

function DriveAccountCard({ account, onConnect, onRetry }: {
  account: AccountState | null;
  onConnect: () => void;
  onRetry: () => void;
}) {
  // Compact and stacked, so it reads the same on a phone as on a desktop.
  const connected = account?.connected && account.account ? account.account : null;
  const reason = account?.reason ?? null;
  return (
    <div data-testid="drive-account"
      className="flex flex-wrap items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2">
      {connected?.photo_url && (
        <img src={connected.photo_url} alt="" className="h-8 w-8 rounded-full" referrerPolicy="no-referrer" />
      )}
      <div className="min-w-0 flex-1">
        <div className="text-[11px] uppercase tracking-wide text-gray-500">Google Drive account</div>
        {account === null ? (
          <div className="text-sm text-gray-500">Checking…</div>
        ) : connected ? (
          <>
            <div className="text-xs text-green-700">Connected</div>
            {connected.display_name && (
              <div className="truncate text-sm font-medium text-gray-900">{connected.display_name}</div>
            )}
            <div className="truncate text-xs text-gray-600" data-testid="drive-account-email">{connected.email}</div>
          </>
        ) : (
          <div className="text-sm text-amber-700">
            {reason === "reauthorization_required" ? "Connection expired"
              : reason === "verification_unavailable" ? "Could not verify the connected account"
              : "Not connected"}
          </div>
        )}
      </div>
      {account !== null && !connected && (
        <Button variant="secondary" onClick={reason === "verification_unavailable" ? onRetry : onConnect}>
          {reason === "reauthorization_required" ? "Reconnect Google Drive"
            : reason === "verification_unavailable" ? "Try again"
            : "Connect Google Drive"}
        </Button>
      )}
    </div>
  );
}
