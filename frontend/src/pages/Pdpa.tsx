import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";
import { Badge, Card, EmptyState, Input, PageHeader, Spinner } from "../components/ui";

interface StatusRow {
  person_id: string;
  participant_id: string;
  first_name: string;
  last_name: string;
  status: "consented" | "declined" | "pending";
  last_updated: string | null;
  // "registration" | "kiosk" | "admin", or null when nobody has answered yet.
  source: string | null;
}

type Filter = "all" | "consented" | "declined" | "pending";

function StatusBadge({ status }: { status: string }) {
  if (status === "consented") return <Badge tone="good">CONSENTED</Badge>;
  if (status === "declined") return <Badge tone="bad">NOT CONSENTED</Badge>;
  return <Badge tone="default">PENDING</Badge>;
}

/** Where the current answer came from — a form answer is not the same evidence
 *  as someone tapping the kiosk themselves, so the page never conflates them. */
function SourceLabel({ source }: { source: string | null }) {
  if (source === "registration") return <span className="text-gray-600">Registration form</span>;
  if (source === "kiosk") return <span className="text-gray-600">Kiosk</span>;
  if (source === "admin") return <span className="text-gray-600">Set by admin</span>;
  return <span className="text-gray-400">No answer yet</span>;
}

export default function Pdpa() {
  const [rows, setRows] = useState<StatusRow[] | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");

  function load() {
    apiGet("/api/pdpa/status").then(setRows);
  }

  useEffect(() => {
    load();
  }, []);

  const counts = useMemo(() => {
    const c = { all: 0, consented: 0, declined: 0, pending: 0 };
    for (const r of rows ?? []) {
      c.all += 1;
      c[r.status] += 1;
    }
    return c;
  }, [rows]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (rows ?? []).filter((r) => {
      if (filter !== "all" && r.status !== filter) return false;
      if (!q) return true;
      return (
        `${r.first_name} ${r.last_name}`.toLowerCase().includes(q) ||
        r.participant_id.toLowerCase().includes(q)
      );
    });
  }, [rows, filter, query]);

  const tabs: { key: Filter; label: string; tone: string }[] = [
    { key: "all", label: "All", tone: "text-gray-700" },
    { key: "consented", label: "Consented", tone: "text-green-700" },
    { key: "declined", label: "Not consented", tone: "text-red-700" },
    { key: "pending", label: "Pending", tone: "text-gray-500" },
  ];

  return (
    <div>
      <PageHeader
        title="PDPA"
        subtitle="Each participant's current consent status — their latest answer, whether it came from the registration form or from the kiosk."
        action={
          <button
            onClick={load}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50"
          >
            Refresh
          </button>
        }
      />

      <Card className="mb-4">
        <div className="flex flex-wrap items-center gap-2">
          {tabs.map((t) => (
            <button
              key={t.key}
              onClick={() => setFilter(t.key)}
              className={
                "px-3 py-1.5 rounded-lg text-sm border transition-colors " +
                (filter === t.key
                  ? "border-indigo-300 bg-indigo-50 text-indigo-800 font-medium"
                  : "border-gray-200 hover:bg-gray-50 " + t.tone)
              }
            >
              {t.label}
              <span className="ml-1.5 tabular-nums text-gray-400">{counts[t.key]}</span>
            </button>
          ))}
          <div className="flex-1 min-w-[12rem]">
            <Input
              value={query}
              placeholder="Search name or participant ID..."
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        </div>
      </Card>

      <Card className="p-0 overflow-hidden">
        {!rows ? (
          <Spinner />
        ) : rows.length === 0 ? (
          <EmptyState>No participants registered yet.</EmptyState>
        ) : visible.length === 0 ? (
          <EmptyState>No participants match this filter.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
                <tr>
                  <th className="text-left px-4 py-3">Participant</th>
                  <th className="text-left px-4 py-3">Current Status</th>
                  <th className="text-left px-4 py-3">Answered Via</th>
                  <th className="text-left px-4 py-3">Last Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {visible.map((r) => (
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
                    <td className="px-4 py-3 text-xs">
                      <SourceLabel source={r.source} />
                    </td>
                    <td className="px-4 py-3 text-gray-500">
                      {r.last_updated ? new Date(r.last_updated).toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="text-xs text-gray-400 mt-3">
        A consent answer imported from the registration form is only the earliest record. Anyone can still
        change it at the kiosk, and the latest answer is always the one shown here.
      </p>
    </div>
  );
}
