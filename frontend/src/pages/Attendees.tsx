import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet, fileUrl } from "../api/client";
import { Badge, Card, EmptyState, PageHeader, Spinner } from "../components/ui";

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

export default function Attendees() {
  const [attendees, setAttendees] = useState<Attendee[] | null>(null);

  useEffect(() => {
    apiGet("/api/attendees").then(setAttendees);
  }, []);

  if (!attendees) return <Spinner label="Loading attendees..." />;

  const inEvent = attendees.filter((a) => a.status === "in_event").length;

  return (
    <div>
      <PageHeader title="Attendees" subtitle={`${inEvent} of ${attendees.length} registered participants detected at the event.`} />

      <Card className="p-0 overflow-hidden">
        {attendees.length === 0 ? (
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
      </Card>
    </div>
  );
}
