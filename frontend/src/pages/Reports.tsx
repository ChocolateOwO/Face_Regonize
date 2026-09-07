import { Fragment, useState } from "react";
import { apiGet, downloadFile } from "../api/client";
import { Badge, Button, Card, EmptyState, PageHeader, Spinner, StatCard } from "../components/ui";
import { useCachedGet } from "../hooks/useCachedGet";

interface ReportsData {
  total_participants: number;
  total_detected_participants: number;
  attendance_percentage: number;
  total_detections: number;
  unknown_faces: number;
  most_frequent: { person_id: string; name: string; count: number }[];
  detection_timeline: { date: string; count: number }[];
}

interface ActivityRow {
  activity_id: string; // "" is the unassigned bucket
  name: string;
  archived: boolean;
  checked_in: number; // distinct people
  check_ins: number; // total records
  attendance_percentage: number;
  first_check_in: string | null;
  last_check_in: string | null;
}

interface ActivityReport {
  total_participants: number;
  total_check_ins: number;
  activities: ActivityRow[];
}

interface ActivityDetail {
  activity_id: string;
  name: string;
  archived: boolean;
  checked_in: number;
  check_ins: number;
  participants: {
    person_id: string;
    participant_id: string;
    name: string;
    check_ins: number;
    first_check_in: string;
    last_check_in: string;
  }[];
}

/** The unassigned bucket has no real id, so the API addresses it as "none". */
function apiActivityId(id: string): string {
  return id === "" ? "none" : id;
}

function fmt(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export default function Reports() {
  const { data } = useCachedGet<ReportsData>("/api/reports");
  const { data: byActivity } = useCachedGet<ActivityReport>("/api/reports/activities");
  // Which activity has its participant list expanded, and that list. Only one
  // is open at a time, so the page never loads more than it is showing.
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ActivityDetail | null>(null);

  function toggle(id: string) {
    if (openId === id) {
      setOpenId(null);
      setDetail(null);
      return;
    }
    setOpenId(id);
    setDetail(null);
    apiGet(`/api/reports/activities/${apiActivityId(id)}`).then(setDetail);
  }

  if (!data) {
    return (
      <div>
        <PageHeader title="Reports" subtitle="Event statistics computed from real attendance and detection data." />
        <Card><Spinner label="Loading reports..." /></Card>
      </div>
    );
  }

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

      <Card className="mb-4 p-0 overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-100 flex items-baseline justify-between gap-3 flex-wrap">
          <h2 className="font-semibold text-gray-900">Attendance by Activity</h2>
          <span className="text-xs text-gray-400">
            Checked In counts each person once. Check-ins counts every time they were seen.
          </span>
        </div>

        {!byActivity ? (
          <Spinner />
        ) : byActivity.activities.length === 0 ? (
          <EmptyState>
            No activities yet. Add one on the Activities page and check people in to see turnout here.
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
                <tr>
                  <th className="text-left px-4 py-2">Activity</th>
                  <th className="text-left px-4 py-2">Checked In</th>
                  <th className="text-left px-4 py-2">Check-ins</th>
                  <th className="text-left px-4 py-2">Turnout</th>
                  <th className="text-left px-4 py-2">Last Check-in</th>
                  <th className="text-right px-4 py-2">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {byActivity.activities.map((a) => (
                  <Fragment key={a.activity_id || "none"}>
                    <tr className="hover:bg-gray-50">
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2">
                          <span className={a.activity_id ? "font-medium text-gray-900" : "text-gray-500 italic"}>
                            {a.name}
                          </span>
                          {a.archived && <Badge>Archived</Badge>}
                        </div>
                      </td>
                      <td className="px-4 py-2 tabular-nums text-gray-900">{a.checked_in}</td>
                      <td className="px-4 py-2 tabular-nums text-gray-500">{a.check_ins}</td>
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2">
                          <div className="w-24 bg-gray-100 rounded-full h-2">
                            <div
                              className="bg-indigo-600 h-2 rounded-full"
                              style={{ width: `${Math.min(100, a.attendance_percentage)}%` }}
                            />
                          </div>
                          <span className="text-gray-500 tabular-nums text-xs">{a.attendance_percentage}%</span>
                        </div>
                      </td>
                      <td className="px-4 py-2 text-gray-500 whitespace-nowrap">{fmt(a.last_check_in)}</td>
                      <td className="px-4 py-2 text-right whitespace-nowrap space-x-3">
                        <button onClick={() => toggle(a.activity_id)} className="text-indigo-600 hover:underline">
                          {openId === a.activity_id ? "Hide" : "View people"}
                        </button>
                        <button
                          onClick={() =>
                            downloadFile(
                              `/api/export/attendance?format=csv&activity_id=${encodeURIComponent(apiActivityId(a.activity_id))}`,
                              `attendance_${a.name.replace(/[^a-zA-Z0-9]+/g, "_")}.csv`,
                            )
                          }
                          className="text-gray-600 hover:underline"
                        >
                          Export
                        </button>
                      </td>
                    </tr>

                    {openId === a.activity_id && (
                      <tr>
                        <td colSpan={6} className="px-4 py-3 bg-gray-50">
                          {!detail ? (
                            <Spinner />
                          ) : detail.participants.length === 0 ? (
                            <div className="text-sm text-gray-500">Nobody has checked in to this activity yet.</div>
                          ) : (
                            <div className="max-h-72 overflow-y-auto rounded-lg border border-gray-200 bg-white">
                              <table className="w-full text-sm">
                                <thead className="bg-gray-50 text-gray-500 text-xs uppercase sticky top-0">
                                  <tr>
                                    <th className="text-left px-3 py-2">Participant</th>
                                    <th className="text-left px-3 py-2">Check-ins</th>
                                    <th className="text-left px-3 py-2">First</th>
                                    <th className="text-left px-3 py-2">Last</th>
                                  </tr>
                                </thead>
                                <tbody className="divide-y divide-gray-100">
                                  {detail.participants.map((p) => (
                                    <tr key={p.person_id}>
                                      <td className="px-3 py-2">
                                        <span className="text-gray-900">{p.name}</span>
                                        <span className="text-xs text-gray-400 ml-2">{p.participant_id}</span>
                                      </td>
                                      <td className="px-3 py-2 tabular-nums text-gray-500">{p.check_ins}</td>
                                      <td className="px-3 py-2 text-gray-500 whitespace-nowrap">{fmt(p.first_check_in)}</td>
                                      <td className="px-3 py-2 text-gray-500 whitespace-nowrap">{fmt(p.last_check_in)}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

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
