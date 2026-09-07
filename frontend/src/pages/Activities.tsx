import { useEffect, useState } from "react";
import { apiDelete, apiGet, apiPostJson, apiPutJson, ApiError } from "../api/client";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Spinner } from "../components/ui";
import { listCamerasWithPermission, type CameraDevice } from "../api/cameras";

interface Activity {
  id: string;
  name: string;
  archived: boolean;
  created_at: string;
  is_current: boolean;
  checked_in_count: number;
}

export default function Activities() {
  const [activities, setActivities] = useState<Activity[] | null>(null);
  const [currentId, setCurrentId] = useState("");
  const [newName, setNewName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const [cameras, setCameras] = useState<CameraDevice[] | null>(null);
  const [cameraError, setCameraError] = useState("");
  // One camera choice per activity - this is what makes several stations
  // possible at once: each row is opened as its own window on its own camera.
  const [rowCamera, setRowCamera] = useState<Record<string, string>>({});
  // Mode per station too: a Registration desk may want to ask PDPA consent
  // while a Food table, where everyone consented at registration, does not.
  const [rowMode, setRowMode] = useState<Record<string, string>>({});
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  // The activity awaiting delete confirmation, and whether the user chose to
  // destroy its check-ins too. Defaults to keeping them every time the dialog
  // opens, so the destructive option is never one careless Enter away.
  const [deleting, setDeleting] = useState<Activity | null>(null);
  const [alsoDeleteAttendance, setAlsoDeleteAttendance] = useState(false);

  useEffect(() => {
    load();
  }, []);

  async function load() {
    const data = await apiGet("/api/activities");
    setActivities(data.activities);
    setCurrentId(data.current_activity_id);
  }

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  const add = () =>
    run(async () => {
      await apiPostJson("/api/activities", { name: newName });
      setNewName("");
    });

  const makeDefault = (id: string) =>
    run(() => apiPutJson("/api/activities/current", { activity_id: id }));

  const setArchived = (id: string, archived: boolean) =>
    run(() => apiPutJson(`/api/activities/${id}`, { archived }));

  const rename = () =>
    run(async () => {
      if (!renaming) return;
      await apiPutJson(`/api/activities/${renaming.id}`, { name: renaming.name });
      setRenaming(null);
    });

  function askDelete(a: Activity) {
    setAlsoDeleteAttendance(false);   // always reopen on the safe choice
    setDeleting(a);
  }

  const confirmDelete = () =>
    run(async () => {
      if (!deleting) return;
      await apiDelete(
        `/api/activities/${deleting.id}?delete_attendance=${alsoDeleteAttendance}`,
      );
      setDeleting(null);
    });

  // Camera labels stay hidden until permission is granted once, so this is a
  // button rather than something that runs on page load.
  async function detectCameras() {
    setCameraError("");
    try {
      setCameras(await listCamerasWithPermission());
    } catch (err) {
      setCameraError(err instanceof Error ? err.message : "Could not access the camera.");
    }
  }

  function stationUrl(activityId: string): string {
    const p = new URLSearchParams();
    p.set("activity", activityId);
    const cam = rowCamera[activityId];
    if (cam) p.set("camera", cam);
    p.set("mode", rowMode[activityId] ?? "always");
    return `${window.location.origin}/recognition?${p.toString()}`;
  }

  function openStation(a: Activity) {
    window.open(stationUrl(a.id), `station_${a.id}`, "noopener");
    setOpened((prev) => ({ ...prev, [a.id]: true }));
  }

  if (!activities) return <Spinner label="Loading activities..." />;

  const live = activities.filter((a) => !a.archived);
  const archived = activities.filter((a) => a.archived);
  const openCount = live.filter((a) => opened[a.id]).length;

  // Two stations pointed at the same camera would fight over the device, so
  // warn rather than let it fail confusingly at getUserMedia time.
  const camCounts: Record<string, number> = {};
  for (const a of live) {
    const c = rowCamera[a.id];
    if (c) camCounts[c] = (camCounts[c] ?? 0) + 1;
  }
  const clashing = Object.values(camCounts).some((n) => n > 1);

  return (
    <div>
      <PageHeader
        title="Activities"
        subtitle="Stations within your event. Open one window per activity, each on its own camera — they all run at the same time, and a person can check in once per activity."
      />

      <Card className="bg-indigo-50 border-indigo-100 mb-4">
        <div className="font-semibold text-indigo-900">Run several activities at once</div>
        <div className="text-sm text-gray-700 mt-1">
          Pick a camera and a mode for each activity below, then press <strong>Open station</strong>. Each
          opens its own window pinned to that activity, camera and mode, and they all check people in
          simultaneously. You can also open two windows on the <em>same</em> activity with different cameras.
          Mode is per station, so a registration desk can ask PDPA consent while a food table does not.
        </div>
        {openCount > 0 && (
          <div className="text-sm text-indigo-700 mt-2">
            {openCount} station window{openCount > 1 ? "s" : ""} opened from this page.
          </div>
        )}
      </Card>

      {error && <Card className="border-red-200 mb-4"><div className="text-sm text-red-700">{error}</div></Card>}

      <Card className="mb-4">
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <label className="block text-sm text-gray-600 mb-1">Add an activity</label>
            <Input
              value={newName}
              placeholder="Food, Gadget, Registration..."
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && newName.trim()) add(); }}
            />
          </div>
          <Button onClick={add} disabled={busy || !newName.trim()}>Add</Button>
          {cameras === null && (
            <Button variant="secondary" onClick={detectCameras}>Detect cameras</Button>
          )}
        </div>
        {cameraError && <p className="text-xs text-red-600 mt-2">{cameraError}</p>}
        {cameras !== null && cameras.length === 1 && (
          <p className="text-xs text-amber-600 mt-2">
            Only one camera is connected, so every station would share it. Connect more cameras to run
            stations on different ones.
          </p>
        )}
        {clashing && (
          <p className="text-xs text-amber-600 mt-2">
            Two activities are set to the same camera. A camera can only feed one station at a time —
            give them different cameras.
          </p>
        )}
      </Card>

      <Card className="p-0 overflow-hidden">
        {live.length === 0 ? (
          <EmptyState>No activities yet. Add one above to start checking people in per station.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
                <tr>
                  <th className="text-left px-4 py-3">Activity</th>
                  <th className="text-left px-4 py-3">Checked In</th>
                  <th className="text-left px-4 py-3">Station camera</th>
                  <th className="text-left px-4 py-3">Station mode</th>
                  <th className="text-right px-4 py-3">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {live.map((a) => (
                  <tr key={a.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3">
                      {renaming?.id === a.id ? (
                        <div className="flex gap-2">
                          <Input
                            value={renaming.name}
                            onChange={(e) => setRenaming({ id: a.id, name: e.target.value })}
                            onKeyDown={(e) => { if (e.key === "Enter") rename(); }}
                            className="flex-1"
                          />
                          <Button onClick={rename} disabled={busy}>Save</Button>
                          <Button variant="secondary" onClick={() => setRenaming(null)}>Cancel</Button>
                        </div>
                      ) : (
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-gray-900">{a.name}</span>
                          {opened[a.id] && <Badge tone="good">● Station open</Badge>}
                          {a.id === currentId && <Badge>Kiosk default</Badge>}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-gray-500">{a.checked_in_count}</td>
                    <td className="px-4 py-3">
                      {cameras === null ? (
                        <span className="text-xs text-gray-400">Press “Detect cameras”</span>
                      ) : (
                        <select
                          value={rowCamera[a.id] ?? ""}
                          onChange={(e) => setRowCamera((p) => ({ ...p, [a.id]: e.target.value }))}
                          className="rounded-lg border border-gray-200 px-2 py-1.5 text-sm max-w-[15rem]"
                        >
                          <option value="">Default camera</option>
                          {cameras.map((c) => (
                            <option key={c.deviceId} value={c.deviceId}>{c.label}</option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <select
                        value={rowMode[a.id] ?? "always"}
                        onChange={(e) => setRowMode((p) => ({ ...p, [a.id]: e.target.value }))}
                        className="rounded-lg border border-gray-200 px-2 py-1.5 text-sm"
                      >
                        <option value="always">Always on — no prompt</option>
                        <option value="tap">Tap to scan — ask consent</option>
                      </select>
                    </td>
                    <td className="px-4 py-3 text-right space-x-2 whitespace-nowrap">
                      <Button onClick={() => openStation(a)} disabled={busy}>
                        {opened[a.id] ? "Reopen station" : "Open station"}
                      </Button>
                      <button onClick={() => navigator.clipboard?.writeText(stationUrl(a.id))}
                        className="text-gray-600 text-sm hover:underline">Copy link</button>
                      {a.id !== currentId && (
                        <button onClick={() => makeDefault(a.id)} disabled={busy}
                          className="text-indigo-600 text-sm hover:underline">Set as default</button>
                      )}
                      <button onClick={() => setRenaming({ id: a.id, name: a.name })} disabled={busy}
                        className="text-gray-600 text-sm hover:underline">Rename</button>
                      <button onClick={() => setArchived(a.id, true)} disabled={busy}
                        className="text-gray-600 text-sm hover:underline">Archive</button>
                      <button onClick={() => askDelete(a)} disabled={busy}
                        className="text-red-600 text-sm hover:underline">Delete</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="text-xs text-gray-400 mt-3">
        <strong>Kiosk default</strong> is only used by a kiosk opened without a station link — every station
        window carries its own activity, so the default does not limit how many run at once. Recognition is
        shared by one backend, so each extra station takes a share of it.
      </p>

      {archived.length > 0 && (
        <Card className="mt-4">
          <h2 className="font-semibold text-gray-900 mb-1">Archived</h2>
          <p className="text-xs text-gray-400 mb-3">
            Hidden from stations. Their check-in history is kept and still counts in reports.
          </p>
          <div className="space-y-2">
            {archived.map((a) => (
              <div key={a.id} className="flex items-center justify-between text-sm">
                <span className="text-gray-600">{a.name} · {a.checked_in_count} checked in</span>
                <button onClick={() => setArchived(a.id, false)} disabled={busy}
                  className="text-indigo-600 hover:underline">Restore</button>
              </div>
            ))}
          </div>
        </Card>
      )}

      {deleting && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl w-full max-w-md p-6">
            <h2 className="font-semibold text-lg mb-1">Delete “{deleting.name}”?</h2>

            {deleting.checked_in_count === 0 ? (
              <p className="text-sm text-gray-600 mb-5">
                Nobody has been checked in to this activity, so nothing else is affected.
              </p>
            ) : (
              <>
                <p className="text-sm text-gray-600 mb-3">
                  This activity has <strong>{deleting.checked_in_count}</strong> check-in
                  {deleting.checked_in_count === 1 ? "" : "s"}. Choose what happens to them.
                </p>
                <div className="space-y-2 mb-5">
                  <label className="flex gap-3 p-3 rounded-lg border border-gray-200 cursor-pointer hover:bg-gray-50">
                    <input
                      type="radio"
                      className="mt-0.5"
                      checked={!alsoDeleteAttendance}
                      onChange={() => setAlsoDeleteAttendance(false)}
                    />
                    <span className="text-sm">
                      <span className="font-medium text-gray-900">Keep the check-in records</span>
                      <span className="block text-gray-500">
                        They still count as attendance and appear under “No activity” in Reports.
                      </span>
                    </span>
                  </label>
                  <label className="flex gap-3 p-3 rounded-lg border border-gray-200 cursor-pointer hover:bg-gray-50">
                    <input
                      type="radio"
                      className="mt-0.5"
                      checked={alsoDeleteAttendance}
                      onChange={() => setAlsoDeleteAttendance(true)}
                    />
                    <span className="text-sm">
                      <span className="font-medium text-red-700">Delete the check-ins too</span>
                      <span className="block text-gray-500">
                        Permanently removes all {deleting.checked_in_count} record
                        {deleting.checked_in_count === 1 ? "" : "s"}. This cannot be undone.
                      </span>
                    </span>
                  </label>
                </div>
              </>
            )}

            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setDeleting(null)} disabled={busy}>
                Cancel
              </Button>
              <button
                onClick={confirmDelete}
                disabled={busy}
                className="px-4 py-2 rounded-lg bg-red-600 text-white text-sm font-medium hover:bg-red-700 disabled:opacity-50"
              >
                {busy ? "Deleting..." : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
