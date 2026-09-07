import { useEffect, useState } from "react";
import { apiDelete, apiGet, downloadFile } from "../api/client";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Pagination, Spinner } from "../components/ui";

interface HistoryEntry {
  id: string;
  upload_id: string;
  upload_filename: string | null;
  person_id: string | null;
  name: string | null;
  participant_id: string | null;
  confidence: number;
  status: string;
  detected_at: string;
}

const DEFAULT_PAGE_SIZE = 50;

export default function History() {
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [total, setTotal] = useState(0);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  async function load() {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (status) params.set("status", status);
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    const data = await apiGet(`/api/history?${params.toString()}`);
    // Deleting the last row on the last page (or a filter change shrinking
    // the result set) can leave `page` pointing past the end — step back to
    // the new last page instead of showing a stuck, falsely-empty result.
    const lastPage = Math.max(1, Math.ceil(data.total / pageSize));
    if (page > lastPage) {
      setPage(lastPage);
      return;
    }
    setEntries(data.items);
    setTotal(data.total);
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, status, page, pageSize]);

  // A new search/filter/page-size makes the old page number meaningless —
  // land back on page 1 rather than showing (say) page 6 of a 2-page result.
  // Bundled into the same handler as the filter change itself (rather than a
  // separate effect watching q/status/pageSize) so React batches both state
  // updates into one render, and the load effect above fires exactly once
  // with the final values instead of once with the stale page first.
  function updateQ(value: string) {
    setQ(value);
    setPage(1);
  }
  function updateStatus(value: string) {
    setStatus(value);
    setPage(1);
  }
  function updatePageSize(size: number) {
    setPageSize(size);
    setPage(1);
  }

  async function handleDelete(id: string) {
    if (!confirm("Are you sure you want to delete this recognition history entry? This data will be gone forever and cannot be recovered.")) return;
    await apiDelete(`/api/history/${id}`);
    load(); // re-fetch this page — deleting the last row on a page should not leave a gap
  }

  async function handleDeleteAll() {
    // Deletes every recognition history record, not just what the current
    // search/status filter happens to be showing — worded explicitly so
    // that's never a surprise.
    if (
      !confirm(
        "Are you sure you want to delete ALL recognition history records, regardless of the current search/filter? This data will be gone forever and cannot be recovered."
      )
    )
      return;
    await apiDelete("/api/history");
    setQ("");
    setStatus("");
    setPage(1);
    load();
  }

  return (
    <div>
      <PageHeader
        title="Recognition History"
        subtitle="Search and filter every face recognition attempt."
        action={
          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => downloadFile("/api/export/history?format=csv", "recognition_history.csv")}>
              Export CSV
            </Button>
            <Button variant="danger" onClick={handleDeleteAll} disabled={total === 0}>
              Delete All
            </Button>
          </div>
        }
      />

      <Card className="mb-4 flex gap-3 flex-wrap">
        <Input placeholder="Search by name, ID, or filename..." value={q} onChange={(e) => updateQ(e.target.value)} className="flex-1 min-w-[200px]" />
        <select
          value={status}
          onChange={(e) => updateStatus(e.target.value)}
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
        >
          <option value="">All statuses</option>
          <option value="matched">Matched</option>
          <option value="unknown">Unknown</option>
        </select>
      </Card>

      <Card className="p-0 overflow-hidden">
        {!entries ? (
          <Spinner />
        ) : entries.length === 0 ? (
          <EmptyState>No recognition history found.</EmptyState>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-3">Date / Time</th>
                <th className="text-left px-4 py-3">Person</th>
                <th className="text-left px-4 py-3">Confidence</th>
                <th className="text-left px-4 py-3">Status</th>
                <th className="text-left px-4 py-3">Source Image</th>
                <th className="text-right px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {entries.map((e) => (
                <tr key={e.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-gray-500">{new Date(e.detected_at).toLocaleString()}</td>
                  <td className="px-4 py-3 font-medium text-gray-900">{e.name || "Unknown"}</td>
                  <td className="px-4 py-3 text-gray-500">{(e.confidence * 100).toFixed(0)}%</td>
                  <td className="px-4 py-3">
                    <Badge tone={e.status === "matched" ? "good" : "warn"}>{e.status}</Badge>
                  </td>
                  <td className="px-4 py-3 text-gray-500">{e.upload_filename}</td>
                  <td className="px-4 py-3 text-right">
                    <button onClick={() => handleDelete(e.id)} className="text-red-600 hover:text-red-800 text-xs font-medium">
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {entries && (
          <Pagination page={page} pageSize={pageSize} total={total} onPageChange={setPage} onPageSizeChange={updatePageSize} />
        )}
      </Card>
    </div>
  );
}
