import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { fileUrl } from "../api/client";
import { Badge, Card, EmptyState, PageHeader, Pagination, Spinner } from "../components/ui";
import { useLiveEvents } from "../hooks/useLiveEvents";
import { useCachedGet } from "../hooks/useCachedGet";

interface Attendee {
  id: string;
  participant_id: string;
  first_name: string;
  last_name: string;
  image_path: string;
  updated_at: string;
  detection_count: number;
  first_detected: string | null;
  last_detected: string | null;
  status: "in_event" | "not_detected";
}

interface AttendeePage {
  items: Attendee[];
  total: number;
  page: number;
  page_size: number;
  in_event: number;
}

export default function Attendees() {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  // Phase I3 — one server-side page; the "detected" count comes from the
  // server over everyone, not from the rows currently on screen.
  const { data, refresh: load } = useCachedGet<AttendeePage>(`/api/attendees?page=${page}&page_size=${pageSize}`);

  useEffect(() => {
    if (data && data.items.length === 0 && page > 1) setPage((p) => p - 1);
  }, [data, page]);

  // This whole page IS an attendance summary — any detection anywhere
  // (kiosk, CCTV, mobile) can flip someone from "Not Detected" to
  // "In Event" or update their counts, so refetch on every event rather
  // than requiring a manual reload. Debounced against event bursts (several
  // cameras reporting the same person within milliseconds of each other).
  const refreshTimerRef = useRef<number | null>(null);
  useLiveEvents(() => {
    if (refreshTimerRef.current) window.clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = window.setTimeout(load, 400);
  });

  if (!data) return <Spinner label="Loading attendees..." />;
  const attendees = data.items;

  return (
    <div>
      <PageHeader title="Attendees" subtitle={`${data.in_event} of ${data.total} registered participants detected at the event.`} />

      <Card className="p-0 overflow-hidden">
        {data.total === 0 ? (
          <EmptyState>No participants registered yet.</EmptyState>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-3">Participant</th>
                <th className="text-left px-4 py-3">Status</th>
                <th className="text-left px-4 py-3">Detections</th>
                <th className="text-left px-4 py-3">First Detected</th>
                <th className="text-left px-4 py-3">Last Detected</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {attendees.map((a) => (
                <tr key={a.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <Link to={`/people/${a.id}`} className="flex items-center gap-3">
                      <img src={fileUrl(a.image_path, a.updated_at)} className="w-9 h-9 rounded-full object-cover" />
                      <div className="font-medium text-gray-900">
                        {a.first_name} {a.last_name}
                        <div className="text-xs text-gray-400">{a.participant_id}</div>
                      </div>
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    {a.status === "in_event" ? <Badge tone="good">● In Event</Badge> : <Badge>○ Not Detected</Badge>}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{a.detection_count}</td>
                  <td className="px-4 py-3 text-gray-500">{a.first_detected ? new Date(a.first_detected).toLocaleString() : "—"}</td>
                  <td className="px-4 py-3 text-gray-500">{a.last_detected ? new Date(a.last_detected).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data.total > 0 && (
          <Pagination
            page={page}
            pageSize={pageSize}
            total={data.total}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
          />
        )}
      </Card>
    </div>
  );
}
