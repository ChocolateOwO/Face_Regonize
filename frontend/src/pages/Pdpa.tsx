import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, ConsentBadge, ConsentSourceLabel, EmptyState, Input, PageHeader, Pagination, Spinner, Tabs } from "../components/ui";
import { useCachedGet } from "../hooks/useCachedGet";

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

interface StatusPage {
  items: StatusRow[];
  total: number;
  page: number;
  page_size: number;
  counts: Record<Filter, number>;
}

export default function Pdpa() {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  // Phase I2 — filtered and paged on the server; the tab counts come back
  // with the page and always cover everyone.
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (filter !== "all") params.set("status", filter);
  if (query.trim()) params.set("q", query.trim());
  const { data, refresh: load } = useCachedGet<StatusPage>(`/api/pdpa/status?${params}`);

  useEffect(() => setPage(1), [filter, query]);

  const counts = data?.counts ?? { all: 0, consented: 0, declined: 0, pending: 0 };
  const tabs: { key: Filter; label: string; tone: string; count: number }[] = [
    { key: "all", label: "All", tone: "text-gray-700", count: counts.all },
    { key: "consented", label: "Consented", tone: "text-green-700", count: counts.consented },
    { key: "declined", label: "Not consented", tone: "text-red-700", count: counts.declined },
    { key: "pending", label: "Pending", tone: "text-gray-500", count: counts.pending },
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
          <Tabs tabs={tabs} active={filter} onChange={setFilter} />
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
        {!data ? (
          <Spinner />
        ) : counts.all === 0 ? (
          <EmptyState>No participants registered yet.</EmptyState>
        ) : data.items.length === 0 ? (
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
                {data.items.map((r) => (
                  <tr key={r.person_id} className="hover:bg-gray-50">
                    <td className="px-4 py-3">
                      {/* The participant page is the one place a person's
                          details and their consent live; PDPA is the
                          overview/report that points into it. */}
                      <Link to={`/people/${r.person_id}`} className="text-indigo-600 hover:underline font-medium">
                        {r.first_name} {r.last_name}
                      </Link>
                      <div className="text-xs text-gray-400">{r.participant_id}</div>
                    </td>
                    <td className="px-4 py-3">
                      <ConsentBadge status={r.status} />
                    </td>
                    <td className="px-4 py-3 text-xs">
                      <ConsentSourceLabel source={r.source} />
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
        {data && data.total > 0 && (
          <Pagination
            page={page}
            pageSize={pageSize}
            total={data.total}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
          />
        )}
      </Card>

      <p className="text-xs text-gray-400 mt-3">
        A consent answer imported from the registration form is only the earliest record. Anyone can still
        change it at the kiosk, and the latest answer is always the one shown here.
      </p>
    </div>
  );
}
