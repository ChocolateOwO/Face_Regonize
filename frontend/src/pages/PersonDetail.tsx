import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiDelete, apiGet, fileUrl } from "../api/client";
import { Badge, Button, Card, EmptyState, PageHeader, Spinner } from "../components/ui";

interface PersonDetailData {
  id: string;
  participant_id: string;
  first_name: string;
  last_name: string;
  email: string | null;
  image_path: string;
  image_source: string;
  original_image_url: string | null;
  det_score: number;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  detection_count: number;
  first_detected: string | null;
  last_detected: string | null;
  attendance_history: { id: string; upload_id: string; confidence: number; detected_at: string }[];
}

export default function PersonDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [data, setData] = useState<PersonDetailData | null>(null);

  useEffect(() => {
    if (id) apiGet(`/api/people/${id}`).then(setData);
  }, [id]);

  async function handleDelete() {
    if (
      !data ||
      !confirm(`Are you sure you want to delete ${data.first_name} ${data.last_name}? This data will be gone forever and cannot be recovered.`)
    )
      return;
    await apiDelete(`/api/people/${data.id}`);
    navigate("/people");
  }

  if (!data) return <Spinner label="Loading participant..." />;

  return (
    <div>
      <PageHeader
        title={`${data.first_name} ${data.last_name}`}
        subtitle={`Participant ID: ${data.participant_id}`}
        action={
          <div className="flex gap-2">
            <Link to="/people">
              <Button variant="secondary">Back to People</Button>
            </Link>
            <Button variant="danger" onClick={handleDelete}>
              Delete
            </Button>
          </div>
        }
      />

      <div className="grid md:grid-cols-3 gap-4">
        <Card>
          <img src={fileUrl(data.image_path, data.updated_at)} className="w-full aspect-square object-cover rounded-lg mb-3" />
          {data.is_demo && <Badge>Demo Data</Badge>}
          <div className="text-sm text-gray-600 space-y-1 mt-3">
            <div>
              <strong>Email:</strong> {data.email || "—"}
            </div>
            <div>
              <strong>Registered:</strong> {new Date(data.created_at).toLocaleString()}
            </div>
            <div>
              <strong>Detection confidence at enrollment:</strong> {(data.det_score * 100).toFixed(0)}%
            </div>
            <div className="pt-2 border-t border-gray-100 mt-2">
              <strong>Image Source:</strong> {data.image_source === "manual" ? "Manual Upload" : data.image_source === "google_drive" ? "Google Drive" : "URL"}
              {data.original_image_url && (
                <div>
                  <a href={data.original_image_url} target="_blank" rel="noreferrer" className="text-indigo-600 hover:underline text-xs">
                    Open source URL
                  </a>
                </div>
              )}
            </div>
          </div>
        </Card>

        <Card className="md:col-span-2">
          <h2 className="font-semibold text-gray-900 mb-3">Attendance Summary</h2>
          <div className="grid grid-cols-3 gap-3 mb-5">
            <div>
              <div className="text-2xl font-bold">{data.detection_count}</div>
              <div className="text-xs text-gray-500">Detections</div>
            </div>
            <div>
              <div className="text-sm font-medium">{data.first_detected ? new Date(data.first_detected).toLocaleString() : "—"}</div>
              <div className="text-xs text-gray-500">First Detected</div>
            </div>
            <div>
              <div className="text-sm font-medium">{data.last_detected ? new Date(data.last_detected).toLocaleString() : "—"}</div>
              <div className="text-xs text-gray-500">Last Detected</div>
            </div>
          </div>

          <h3 className="font-medium text-gray-900 mb-2 text-sm">Attendance History</h3>
          {data.attendance_history.length === 0 ? (
            <EmptyState>Not detected at the event yet.</EmptyState>
          ) : (
            <div className="divide-y divide-gray-100">
              {data.attendance_history.map((h) => (
                <div key={h.id} className="flex items-center justify-between py-2 text-sm">
                  <span>{new Date(h.detected_at).toLocaleString()}</span>
                  <span className="text-gray-500">{(h.confidence * 100).toFixed(0)}% confidence</span>
                  <Link to={`/uploads/${h.upload_id}`} className="text-indigo-600 hover:underline">
                    View source image
                  </Link>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
