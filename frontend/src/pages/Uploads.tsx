import { useEffect, useState, type MouseEvent } from "react";
import { Link } from "react-router-dom";
import { apiDelete, apiGet, apiPostJson, fileUrl } from "../api/client";
import { Badge, Button, Card, EmptyState, PageHeader, Pagination, Spinner } from "../components/ui";

interface UploadItem {
  id: string;
  filename: string;
  image_path: string;
  thumbnail_path: string;
  uploaded_at: string;
  processing_status: string;
  faces_total: number;
  faces_matched: number;
  faces_unknown: number;
  processing_duration_ms: number | null;
}

const DEFAULT_PAGE_SIZE = 50;

export default function Uploads() {
  const [uploads, setUploads] = useState<UploadItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  async function load() {
    const data = await apiGet(`/api/uploads?page=${page}&page_size=${pageSize}`);
    // Deleting the last item on the last page can leave `page` pointing past
    // the end — step back to the new last page rather than showing a stuck,
    // falsely-empty grid.
    const lastPage = Math.max(1, Math.ceil(data.total / pageSize));
    if (page > lastPage) {
      setPage(lastPage);
      return;
    }
    setUploads(data.items);
    setTotal(data.total);
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize]);

  function updatePageSize(size: number) {
    setPageSize(size);
    setPage(1);
  }

  async function handleDelete(e: MouseEvent, id: string) {
    e.preventDefault(); // this card is wrapped in a <Link> — don't navigate
    e.stopPropagation();
    if (!confirm("Are you sure you want to delete this upload and its detected faces? This data will be gone forever and cannot be recovered.")) return;
    await apiDelete(`/api/uploads/${id}`);
    load(); // re-fetch this page — deleting the last card on a page should not leave a gap
  }

  async function handleDeleteAll() {
    if (!confirm("Are you sure you want to delete ALL uploads and their detected faces? This data will be gone forever and cannot be recovered.")) return;
    // Reuses the same bulk endpoint the Settings page's "Clear Recognition
    // History" action uses — it already deletes Uploads + their
    // FaceDetection children together, matching the single-upload delete's
    // cascade behavior above.
    await apiPostJson("/api/admin/clear-recognition-history", {});
    setPage(1);
    load();
  }

  if (!uploads) return <Spinner label="Loading upload history..." />;

  return (
    <div>
      <PageHeader
        title="Upload History"
        subtitle="Every event photo submitted for recognition."
        action={
          <Button variant="danger" onClick={handleDeleteAll} disabled={total === 0}>
            Delete All
          </Button>
        }
      />
      {uploads.length === 0 ? (
        <Card>
          <EmptyState>No photos uploaded yet. Try the Face Recognition page.</EmptyState>
        </Card>
      ) : (
        <>
          <div className="grid md:grid-cols-3 gap-4 mb-4">
            {uploads.map((u) => (
              <Link to={`/uploads/${u.id}`} key={u.id}>
                <Card className="relative hover:shadow-md transition-shadow">
                  <button
                    onClick={(e) => handleDelete(e, u.id)}
                    title="Delete this upload"
                    className="absolute top-2 right-2 z-10 w-7 h-7 flex items-center justify-center rounded-full bg-white/90 text-red-600 hover:bg-red-50 shadow border border-gray-200 text-sm"
                  >
                    🗑
                  </button>
                  <img src={fileUrl(u.thumbnail_path)} loading="lazy" className="w-full aspect-video object-cover rounded-lg mb-3" />
                  <div className="text-sm font-medium text-gray-900 truncate">{u.filename}</div>
                  <div className="text-xs text-gray-500 mb-2">{new Date(u.uploaded_at).toLocaleString()}</div>
                  <div className="flex gap-2 text-xs mb-2">
                    <Badge>{u.faces_total} Faces</Badge>
                    <Badge tone="good">{u.faces_matched} Matches</Badge>
                    {u.faces_unknown > 0 && <Badge tone="warn">{u.faces_unknown} Unknown</Badge>}
                  </div>
                  <Badge tone={u.processing_status === "completed" ? "good" : u.processing_status === "failed" ? "bad" : "default"}>
                    {u.processing_status}
                  </Badge>
                </Card>
              </Link>
            ))}
          </div>
          <Card className="p-0">
            <Pagination page={page} pageSize={pageSize} total={total} onPageChange={setPage} onPageSizeChange={updatePageSize} />
          </Card>
        </>
      )}
    </div>
  );
}
