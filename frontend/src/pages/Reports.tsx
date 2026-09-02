import { useEffect, useState } from "react";
import { apiGet, downloadFile } from "../api/client";
import { Button, Card, EmptyState, PageHeader, Spinner, StatCard } from "../components/ui";

interface ReportsData {
  total_participants: number;
  total_detected_participants: number;
  attendance_percentage: number;
  total_detections: number;
  unknown_faces: number;
  most_frequent: { person_id: string; name: string; count: number }[];
  detection_timeline: { date: string; count: number }[];
}

export default function Reports() {
  const [data, setData] = useState<ReportsData | null>(null);

  useEffect(() => {
    apiGet("/api/reports").then(setData);
  }, []);

  if (!data) return <Spinner label="Loading reports..." />;

  const maxTimelineCount = Math.max(1, ...data.detection_timeline.map((t) => t.count));
  const maxFreqCount = Math.max(1, ...data.most_frequent.map((f) => f.count));

  return (
    <div>
      <PageHeader
        title="Reports"
        subtitle="Event statistics computed from real attendance and detection data."
        action={
          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => downloadFile("/api/export/people?format=csv", "participants.csv")}>
              Export Participants
            </Button>
            <Button variant="secondary" onClick={() => downloadFile("/api/export/attendance?format=csv", "attendance.csv")}>
              Export Attendance
            </Button>
          </div>
        }
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <StatCard label="Total Participants" value={data.total_participants} />
        <StatCard label="Attendance %" value={`${data.attendance_percentage}%`} tone="good" />
        <StatCard label="Total Detections" value={data.total_detections} />
        <StatCard label="Unknown Faces" value={data.unknown_faces} tone="warn" />
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <Card>
          <h2 className="font-semibold text-gray-900 mb-4">Most Frequently Detected</h2>
          {data.most_frequent.length === 0 ? (
            <EmptyState>No detections yet.</EmptyState>
          ) : (
            <div className="space-y-3">
              {data.most_frequent.map((f) => (
                <div key={f.person_id}>
                  <div className="flex justify-between text-sm mb-1">
                    <span className="text-gray-700">{f.name}</span>
                    <span className="text-gray-500">{f.count}</span>
                  </div>
                  <div className="w-full bg-gray-100 rounded-full h-2">
                    <div className="bg-indigo-600 h-2 rounded-full" style={{ width: `${(f.count / maxFreqCount) * 100}%` }} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card>
          <h2 className="font-semibold text-gray-900 mb-4">Detection Timeline</h2>
          {data.detection_timeline.length === 0 ? (
            <EmptyState>No detections yet.</EmptyState>
          ) : (
            <div className="space-y-3">
              {data.detection_timeline.map((t) => (
                <div key={t.date}>
                  <div className="flex justify-between text-sm mb-1">
                    <span className="text-gray-700">{t.date}</span>
                    <span className="text-gray-500">{t.count}</span>
                  </div>
                  <div className="w-full bg-gray-100 rounded-full h-2">
                    <div className="bg-emerald-500 h-2 rounded-full" style={{ width: `${(t.count / maxTimelineCount) * 100}%` }} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
