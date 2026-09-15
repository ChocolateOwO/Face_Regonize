import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiDelete, apiGet, apiPostJson, fileUrl } from "../api/client";
import {
  Badge,
  Button,
  Card,
  ConsentBadge,
  ConsentSourceLabel,
  EmptyState,
  PageHeader,
  Spinner,
} from "../components/ui";
import { useLiveEvents } from "../hooks/useLiveEvents";

interface ConsentHistoryItem {
  choice: "consented" | "declined";
  source: "kiosk" | "admin" | "registration";
  recorded_at: string;
}

interface ConsentData {
  person_id: string;
  status: "consented" | "declined" | "pending";
  last_updated: string | null;
  source: string | null;
  history: ConsentHistoryItem[];
}

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
  // Consent lives on this page now — a participant and their PDPA answer are
  // one thing, and having to visit a second page to see whether someone's
  // photos get masked was the confusing part. The endpoints are the existing
  // ones, unchanged: ConsentRecord stays append-only and the semantics of an
  // admin override are exactly what the PDPA page already recorded.
  const [consent, setConsent] = useState<ConsentData | null>(null);
  const [savingConsent, setSavingConsent] = useState(false);

  function load() {
    if (id) apiGet(`/api/people/${id}`).then(setData);
  }

  function loadConsent() {
    if (id) apiGet(`/api/pdpa/status/${id}`).then(setConsent);
  }

  useEffect(() => {
    load();
    loadConsent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  async function setConsentChoice(choice: "consented" | "declined") {
    if (!id || !data) return;
    const label = choice === "consented" ? "CONSENTED" : "NOT CONSENTED";
    if (!confirm(`Set ${data.first_name}'s consent status to ${label}? This will be recorded as an admin override.`)) return;
    setSavingConsent(true);
    try {
      await apiPostJson("/api/pdpa/record", { person_ids: [id], choice, source: "admin" });
      loadConsent();
    } finally {
      setSavingConsent(false);
    }
  }

  // Refetch when THIS specific person is detected again anywhere (kiosk,
  // CCTV, mobile) — their Attendance Summary/history changes live, without
  // reloading the page. Other people's events are ignored: they cannot
  // change what this page shows.
  const dataRef = useRef(data);
  dataRef.current = data;
  useLiveEvents((event) => {
    if (dataRef.current && event.participant_id === dataRef.current.participant_id) load();
  });

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
          <div className="flex items-center gap-2">
            {consent && <ConsentBadge status={consent.status} />}
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

      <Card className="mt-4">
        <div className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
          <h2 className="font-semibold text-gray-900">PDPA Consent</h2>
          <Link to="/pdpa" className="text-xs text-indigo-600 hover:underline">
            All participants' consent →
          </Link>
        </div>

        {!consent ? (
          <Spinner label="Loading consent..." />
        ) : (
          <div className="grid md:grid-cols-2 gap-6">
            <div>
              <div className="flex items-center gap-3 flex-wrap">
                <ConsentBadge status={consent.status} />
                <span className="text-xs">
                  <ConsentSourceLabel source={consent.source} />
                </span>
              </div>
              {consent.last_updated && (
                <div className="text-xs text-gray-400 mt-2">
                  Last updated {new Date(consent.last_updated).toLocaleString()}
                </div>
              )}
              {consent.status === "declined" && (
                <p className="text-xs text-red-700 bg-red-50 border border-red-100 rounded-lg px-3 py-2 mt-3">
                  This participant's face is masked in every delivered event photo.
                </p>
              )}

              <div className="mt-4 pt-4 border-t border-gray-100">
                <div className="text-xs text-gray-500 mb-2">Admin override</div>
                <div className="flex gap-2">
                  <button
                    disabled={savingConsent}
                    onClick={() => setConsentChoice("consented")}
                    className="px-4 py-2 rounded-lg text-sm font-medium border border-green-200 text-green-700 hover:bg-green-50 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Set CONSENTED
                  </button>
                  <button
                    disabled={savingConsent}
                    onClick={() => setConsentChoice("declined")}
                    className="px-4 py-2 rounded-lg text-sm font-medium border border-red-200 text-red-700 hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Set NOT CONSENTED
                  </button>
                </div>
                <p className="text-xs text-gray-400 mt-2">
                  Recorded in history as an admin-set entry, distinct from the participant's own kiosk choices.
                  Photos already delivered keep the consent that applied when they were processed.
                </p>
              </div>
            </div>

            <div>
              <h3 className="font-medium text-gray-900 mb-2 text-sm">Consent History</h3>
              {consent.history.length === 0 ? (
                <p className="text-sm text-gray-400">
                  No consent actions recorded yet — status is pending until this participant answers at
                  registration or the kiosk.
                </p>
              ) : (
                <div className="divide-y divide-gray-100">
                  {consent.history.map((h, i) => (
                    <div key={i} className="py-2.5 flex items-center justify-between text-sm">
                      <span className="text-gray-500">
                        {new Date(h.recorded_at).toLocaleString()}
                        <span className="text-xs text-gray-400 ml-2">via {h.source}</span>
                      </span>
                      <ConsentBadge status={h.choice} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
