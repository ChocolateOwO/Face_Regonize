import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiGet, apiPostJson } from "../api/client";
import { Badge, Button, Card, PageHeader, Spinner } from "../components/ui";

interface HistoryItem {
  choice: "consented" | "declined";
  source: "kiosk" | "admin";
  recorded_at: string;
}

interface DetailData {
  person_id: string;
  participant_id: string;
  first_name: string;
  last_name: string;
  status: "consented" | "declined" | "pending";
  last_updated: string | null;
  history: HistoryItem[];
}

function StatusBadge({ status }: { status: string }) {
  if (status === "consented") return <Badge tone="good">CONSENTED</Badge>;
  if (status === "declined") return <Badge tone="bad">NOT CONSENTED</Badge>;
  return <Badge tone="default">PENDING</Badge>;
}

export default function PdpaDetail() {
  const { id } = useParams();
  const [data, setData] = useState<DetailData | null>(null);
  const [saving, setSaving] = useState(false);

  function load() {
    if (id) apiGet(`/api/pdpa/status/${id}`).then(setData);
  }

  useEffect(load, [id]);

  async function setStatus(choice: "consented" | "declined") {
    if (!id) return;
    const label = choice === "consented" ? "CONSENTED" : "NOT CONSENTED";
    if (!confirm(`Set ${data?.first_name}'s consent status to ${label}? This will be recorded as an admin override.`)) return;
    setSaving(true);
    try {
      await apiPostJson("/api/pdpa/record", { person_ids: [id], choice, source: "admin" });
      load();
    } finally {
      setSaving(false);
    }
  }

  if (!data) return <Spinner label="Loading consent history..." />;

  return (
    <div>
      <PageHeader
        title={`${data.first_name} ${data.last_name}`}
        subtitle={data.participant_id}
        action={
          <Link to="/pdpa">
            <Button variant="secondary">Back to PDPA</Button>
          </Link>
        }
      />

      <div className="max-w-xl space-y-4">
        <Card>
          <div className="text-sm text-gray-500 mb-2">Current Status</div>
          <StatusBadge status={data.status} />
          {data.last_updated && <div className="text-xs text-gray-400 mt-2">Last updated {new Date(data.last_updated).toLocaleString()}</div>}

          <div className="mt-4 pt-4 border-t border-gray-100">
            <div className="text-xs text-gray-500 mb-2">Admin override</div>
            <div className="flex gap-2">
              <button
                disabled={saving}
                onClick={() => setStatus("consented")}
                className="px-4 py-2 rounded-lg text-sm font-medium border border-green-200 text-green-700 hover:bg-green-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Set CONSENTED
              </button>
              <button
                disabled={saving}
                onClick={() => setStatus("declined")}
                className="px-4 py-2 rounded-lg text-sm font-medium border border-red-200 text-red-700 hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Set NOT CONSENTED
              </button>
            </div>
            <p className="text-xs text-gray-400 mt-2">Recorded in history as an admin-set entry, distinct from the participant's own kiosk choices.</p>
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold text-gray-900 mb-3">History</h2>
          {data.history.length === 0 ? (
            <p className="text-sm text-gray-400">No consent actions recorded yet — status is pending until this participant is recognized at the kiosk.</p>
          ) : (
            <div className="divide-y divide-gray-100">
              {data.history.map((h, i) => (
                <div key={i} className="py-2.5 flex items-center justify-between text-sm">
                  <span className="text-gray-500">
                    {new Date(h.recorded_at).toLocaleString()}
                    <span className="text-xs text-gray-400 ml-2">via {h.source}</span>
                  </span>
                  <StatusBadge status={h.choice} />
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
