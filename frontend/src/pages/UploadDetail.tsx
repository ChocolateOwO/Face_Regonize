import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiGet, fileUrl } from "../api/client";
import { Badge, Button, Card, PageHeader, Spinner } from "../components/ui";

interface Detection {
  id: string;
  bbox: number[];
  confidence: number;
  status: string;
  detected_at: string;
  person_id: string | null;
  name: string | null;
  participant_id: string | null;
}

interface UploadDetailData {
  id: string;
  filename: string;
  image_path: string;
  uploaded_at: string;
  processing_status: string;
  processing_duration_ms: number | null;
  threshold_used: number;
  faces_total: number;
  faces_matched: number;
  faces_unknown: number;
  detections: Detection[];
}

export default function UploadDetail() {
  const { id } = useParams();
  const [data, setData] = useState<UploadDetailData | null>(null);
  const [naturalSize, setNaturalSize] = useState({ w: 1, h: 1 });

  useEffect(() => {
    if (id) apiGet(`/api/uploads/${id}`).then(setData);
  }, [id]);

  if (!data) return <Spinner label="Loading upload..." />;

  return (
    <div>
      <PageHeader
        title={data.filename}
        subtitle={`Uploaded ${new Date(data.uploaded_at).toLocaleString()}`}
        action={
          <Link to="/uploads">
            <Button variant="secondary">Back to Upload History</Button>
          </Link>
        }
      />

      <div className="grid md:grid-cols-3 gap-4">
        <Card className="md:col-span-2">
          <div className="relative inline-block max-w-full">
            <img
              src={fileUrl(data.image_path)}
              onLoad={(e) => setNaturalSize({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
              className="max-w-full rounded-lg block"
            />
            {data.detections.map((d) => {
              const [x1, y1, x2, y2] = d.bbox;
              const left = (x1 / naturalSize.w) * 100;
              const top = (y1 / naturalSize.h) * 100;
              const width = ((x2 - x1) / naturalSize.w) * 100;
              const height = ((y2 - y1) / naturalSize.h) * 100;
              const good = d.status === "matched";
              return (
                <div
                  key={d.id}
                  className={`absolute border-2 ${good ? "border-green-500" : "border-red-500"}`}
                  style={{ left: `${left}%`, top: `${top}%`, width: `${width}%`, height: `${height}%` }}
                >
                  <div className={`absolute -top-6 left-0 text-xs font-semibold px-1.5 py-0.5 rounded text-white whitespace-nowrap ${good ? "bg-green-600" : "bg-red-600"}`}>
                    {good ? `${d.name} (${(d.confidence * 100).toFixed(0)}%)` : "Unknown"}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold text-gray-900 mb-3">Detection Results</h2>
          <div className="text-sm text-gray-600 space-y-1 mb-4">
            <div>Total faces: {data.faces_total}</div>
            <div>Matches: {data.faces_matched}</div>
            <div>Unknown: {data.faces_unknown}</div>
            <div>Threshold used: {(data.threshold_used * 100).toFixed(0)}%</div>
            <div>Processing time: {data.processing_duration_ms?.toFixed(0)}ms</div>
          </div>
          <div className="divide-y divide-gray-100">
            {data.detections.map((d) => (
              <div key={d.id} className="py-2 text-sm flex items-center justify-between">
                <span>
                  {d.person_id ? (
                    <Link to={`/people/${d.person_id}`} className="text-indigo-600 hover:underline">
                      {d.name}
                    </Link>
                  ) : (
                    "Unknown"
                  )}
                </span>
                <Badge tone={d.status === "matched" ? "good" : "warn"}>{(d.confidence * 100).toFixed(0)}%</Badge>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
