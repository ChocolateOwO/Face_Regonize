import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiDelete, apiGet, apiPostForm, ApiError, fileUrl } from "../api/client";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Spinner } from "../components/ui";

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

export default function People() {
  const [people, setPeople] = useState<Person[] | null>(null);
  const [search, setSearch] = useState("");
  const [showAdd, setShowAdd] = useState(false);

  async function load() {
    const q = search ? `?q=${encodeURIComponent(search)}` : "";
    setPeople(await apiGet(`/api/people${q}`));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  async function handleDelete(id: string, name: string) {
    if (!confirm(`Are you sure you want to delete participant "${name}"? This data will be gone forever and cannot be recovered.`))
      return;
    await apiDelete(`/api/people/${id}`);
    load();
  }

  async function handleDeleteAll() {
    if (!people || people.length === 0) return;
    if (
      !confirm(
        `Are you sure you want to delete ALL ${people.length} participants? This also deletes their attendance records, recognition history, and PDPA consent history. This data will be gone forever and cannot be recovered.`
      )
    )
      return;
    await apiDelete("/api/people");
    load();
  }

  return (
    <div>
      <PageHeader
        title="People"
        subtitle="Manage registered participants."
        action={
          <div className="flex gap-2">
            <Button variant="danger" onClick={handleDeleteAll} disabled={!people || people.length === 0}>
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
          <EmptyState>No participants yet. Click "Add Person" or use Import Participants.</EmptyState>
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

          <Input placeholder="Participant ID (e.g. P001)" value={participantId} onChange={(e) => setParticipantId(e.target.value)} />
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
