import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiDelete, apiGet, apiPostForm, ApiError, fileUrl } from "../api/client";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Pagination, Spinner } from "../components/ui";
import { useLiveEvents } from "../hooks/useLiveEvents";
import { useCachedGet } from "../hooks/useCachedGet";

interface Person {
  id: string;
  participant_id: string;
  first_name: string;
  last_name: string;
  email: string | null;
  image_path: string;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  detection_count: number;
  last_detected: string | null;
}

interface PageOf<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export default function People() {
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [showAdd, setShowAdd] = useState(false);

  // Phase I1 — a server-side page (search, count, offset and limit all run in
  // the database) instead of the whole participant table.
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (search) params.set("q", search);
  const { data, refresh: load } = useCachedGet<PageOf<Person>>(`/api/people?${params}`);
  const people = data?.items ?? null;
  const total = data?.total ?? 0;

  // A new search is a new result set — page 7 of the old one would be empty.
  useEffect(() => setPage(1), [search]);
  // Deleting the last row of the last page must not strand the view on a blank page.
  useEffect(() => {
    if (data && data.items.length === 0 && page > 1) setPage((p) => p - 1);
  }, [data, page]);

  // Every kiosk scan, CCTV match, and mobile scan changes detection_count /
  // last_detected for someone on this list — refetch when that happens
  // instead of requiring a manual reload. Debounced: several cameras can
  // report the same person within milliseconds of each other, and this
  // should coalesce into one refetch, not one per event.
  const refreshTimerRef = useRef<number | null>(null);
  useLiveEvents(() => {
    if (refreshTimerRef.current) window.clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = window.setTimeout(load, 400);
  });

  async function handleDelete(id: string, name: string) {
    if (!confirm(`Are you sure you want to delete participant "${name}"? This data will be gone forever and cannot be recovered.`))
      return;
    await apiDelete(`/api/people/${id}`);
    load();
  }

  async function handleDeleteAll() {
    if (!total) return;
    if (
      !confirm(
        `Are you sure you want to delete ALL ${total} participants? This also deletes their attendance records, recognition history, and PDPA consent history. This data will be gone forever and cannot be recovered.`
      )
    )
      return;
    await apiDelete("/api/people");
    setPage(1);
    load();
  }

  return (
    <div>
      <PageHeader
        title="People"
        subtitle="Manage registered participants."
        action={
          <div className="flex gap-2">
            <Button variant="danger" onClick={handleDeleteAll} disabled={!total}>
              Delete All
            </Button>
            <Button onClick={() => setShowAdd(true)}>+ Add Person</Button>
          </div>
        }
      />

      <Card className="mb-4">
        <Input placeholder="Search by name, ID, or email..." value={search} onChange={(e) => setSearch(e.target.value)} />
      </Card>

      <Card className="p-0 overflow-hidden">
        {!people ? (
          <Spinner />
        ) : people.length === 0 ? (
          <EmptyState>{search ? "No participants match this search." : 'No participants yet. Click "Add Person" or use Import Participants.'}</EmptyState>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-3">Participant</th>
                <th className="text-left px-4 py-3">ID</th>
                <th className="text-left px-4 py-3">Email</th>
                <th className="text-left px-4 py-3">Detections</th>
                <th className="text-left px-4 py-3">Last Detected</th>
                <th className="text-right px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {people.map((p) => (
                <tr key={p.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <Link to={`/people/${p.id}`} className="flex items-center gap-3">
                      <img src={fileUrl(p.image_path, p.updated_at)} className="w-9 h-9 rounded-full object-cover" />
                      <div>
                        <div className="font-medium text-gray-900">
                          {p.first_name} {p.last_name}
                        </div>
                        {p.is_demo && <Badge>Demo Data</Badge>}
                      </div>
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-gray-500">{p.participant_id}</td>
                  <td className="px-4 py-3 text-gray-500">{p.email || "—"}</td>
                  <td className="px-4 py-3 text-gray-500">{p.detection_count}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {p.last_detected ? new Date(p.last_detected).toLocaleString() : "Not detected"}
                  </td>
                  <td className="px-4 py-3 text-right space-x-2">
                    <Link to={`/people/${p.id}`} className="text-indigo-600 text-sm hover:underline">
                      View
                    </Link>
                    <button
                      onClick={() => handleDelete(p.id, `${p.first_name} ${p.last_name}`)}
                      className="text-red-600 text-sm hover:underline"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {people && total > 0 && (
          <Pagination
            page={page}
            pageSize={pageSize}
            total={total}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
          />
        )}
      </Card>

      {showAdd && (
        <AddPersonModal
          onClose={() => setShowAdd(false)}
          onCreated={() => {
            setShowAdd(false);
            load();
          }}
        />
      )}
    </div>
  );
}

function AddPersonModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [participantId, setParticipantId] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string>("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  // Phase L1 — prefill the next never-reused number. A read-only preview: the
  // number is only consumed if this person is actually registered, and the
  // field stays editable for a custom ID. Never overwrites what was typed.
  useEffect(() => {
    let active = true;
    apiGet("/api/people/next-id")
      .then((r) => {
        if (active && r?.next_id) setParticipantId((current) => current || r.next_id);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  function handleFile(f: File) {
    setFile(f);
    setPreview(URL.createObjectURL(f));
  }

  async function submit() {
    setError("");
    if (!file) return setError("Please upload a reference face photo.");
    setLoading(true);
    try {
      const form = new FormData();
      form.append("participant_id", participantId);
      form.append("first_name", firstName);
      form.append("last_name", lastName);
      if (email) form.append("email", email);
      form.append("photo", file);
      await apiPostForm("/api/people", form);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to register participant.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl w-full max-w-md p-6 max-h-[90vh] overflow-y-auto">
        <h2 className="font-semibold text-lg mb-4">Add Person</h2>

        <div className="space-y-3">
          <div
            onClick={() => document.getElementById("add-person-file")?.click()}
            className="border-2 border-dashed border-gray-300 rounded-lg p-4 text-center cursor-pointer hover:border-indigo-400"
          >
            {preview ? (
              <img src={preview} className="max-h-40 mx-auto rounded" />
            ) : (
              <div className="text-sm text-gray-500">Click to upload a reference face photo</div>
            )}
          </div>
          <input
            id="add-person-file"
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => e.target.files?.[0] && handleFile(e.target.files[0])}
          />

          <div>
            <Input placeholder="Participant ID (e.g. P001)" value={participantId} onChange={(e) => setParticipantId(e.target.value)} />
            <p className="text-xs text-gray-400 mt-1">Suggested next number — you can change it.</p>
          </div>
          <Input placeholder="First Name" value={firstName} onChange={(e) => setFirstName(e.target.value)} />
          <Input placeholder="Last Name" value={lastName} onChange={(e) => setLastName(e.target.value)} />
          <Input placeholder="Email (optional)" value={email} onChange={(e) => setEmail(e.target.value)} />

          {error && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</div>}
        </div>

        <div className="flex justify-end gap-2 mt-5">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={loading || !participantId || !firstName}>
            {loading ? "Registering..." : "Register"}
          </Button>
        </div>
      </div>
    </div>
  );
}
