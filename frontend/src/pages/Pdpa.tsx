import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";
import { Badge, Card, EmptyState, PageHeader, Spinner } from "../components/ui";

interface StatusRow {
  person_id: string;
  participant_id: string;
  first_name: string;
  last_name: string;
  status: "consented" | "declined" | "pending";
  last_updated: string | null;
}

function StatusBadge({ status }: { status: string }) {
  if (status === "consented") return <Badge tone="good">CONSENTED</Badge>;
  if (status === "declined") return <Badge tone="bad">NOT CONSENTED</Badge>;
  return <Badge tone="default">PENDING</Badge>;
}

export default function Pdpa() {
  const [rows, setRows] = useState<StatusRow[] | null>(null);

  useEffect(() => {
    apiGet("/api/pdpa/status").then(setRows);
  }, []);

  return (
    <div>
      <PageHeader title="PDPA" subtitle="Each participant's current consent status, from their latest choice at the kiosk." />

      <Card className="p-0 overflow-hidden">
        {!rows ? (
          <Spinner />
        ) : rows.length === 0 ? (
          <EmptyState>No participants registered yet.</EmptyState>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-3">Participant</th>
                <th className="text-left px-4 py-3">Current Status</th>
                <th className="text-left px-4 py-3">Last Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {rows.map((r) => (
                <tr key={r.person_id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <Link to={`/pdpa/${r.person_id}`} className="text-indigo-600 hover:underline font-medium">
                      {r.first_name} {r.last_name}
                    </Link>
                    <div className="text-xs text-gray-400">{r.participant_id}</div>
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={r.status} />
                  </td>
                  <td className="px-4 py-3 text-gray-500">{r.last_updated ? new Date(r.last_updated).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
