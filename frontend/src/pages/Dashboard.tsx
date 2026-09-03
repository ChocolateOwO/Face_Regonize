import { useEffect, useState } from "react";
import { apiGet, fileUrl } from "../api/client";
import { Badge, Card, EmptyState, PageHeader, Spinner, StatCard } from "../components/ui";

interface DashboardData {
  registered_total: number;
  detected_total: number;
  detected_today: number;
  uploaded_images_total: number;
  recognition_attempts_total: number;
  unknown_faces_total: number;
  failed_uploads_total: number;
  recent_activity: {
    person_name: string;
    person_image: string | null;
    person_image_version: string | null;
    detected_at: string;
    confidence: number;
    event_image: string | null;
    status: string;
  }[];
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);

  useEffect(() => {
    apiGet("/api/reports/dashboard").then(setData);
  }, []);

  if (!data) return <Spinner label="Loading dashboard..." />;

  return (
    <div>
      <PageHeader title="Dashboard" subtitle="Live overview of your event, pulled straight from the database." />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <StatCard label="Registered Participants" value={data.registered_total} />
        <StatCard label="Detected Today" value={data.detected_today} tone="good" />
        <StatCard label="Event Images Uploaded" value={data.uploaded_images_total} />
        <StatCard label="Unknown Faces" value={data.unknown_faces_total} tone="warn" />
        <StatCard label="Total Detected" value={data.detected_total} />
        <StatCard label="Recognition Attempts" value={data.recognition_attempts_total} />
        <StatCard label="Failed Uploads" value={data.failed_uploads_total} tone={data.failed_uploads_total > 0 ? "bad" : "default"} />
      </div>

      <Card>
        <h2 className="font-semibold text-gray-900 mb-4">Recent Activity</h2>
        {data.recent_activity.length === 0 ? (
          <EmptyState>No recognition activity yet. Try the Face Recognition page.</EmptyState>
        ) : (
          <div className="divide-y divide-gray-100">
            {data.recent_activity.map((a, i) => (
              <div key={i} className="flex items-center gap-3 py-3">
                {a.person_image ? (
                  <img src={fileUrl(a.person_image, a.person_image_version ?? undefined)} className="w-10 h-10 rounded-full object-cover" />
                ) : (
                  <div className="w-10 h-10 rounded-full bg-gray-200 flex items-center justify-center text-gray-400">?</div>
                )}
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-gray-900">{a.person_name}</div>
                  <div className="text-xs text-gray-500">{new Date(a.detected_at).toLocaleString()}</div>
                </div>
                <div className="text-sm text-gray-500">{(a.confidence * 100).toFixed(0)}%</div>
                <Badge tone={a.status === "matched" ? "good" : "warn"}>{a.status}</Badge>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
