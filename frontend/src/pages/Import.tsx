import { useEffect, useRef, useState } from "react";
import { apiGet, apiPostForm, ApiError, downloadFile } from "../api/client";
import { Badge, Button, Card, PageHeader, Spinner } from "../components/ui";

interface ColumnCandidate {
  column: string | null;
  score: number;
  samples: string[];
  label?: string;
}

interface NeedsColumnSelectionResponse {
  status: "needs_column_selection";
  needs: ("name" | "photo")[];
  columns: string[];
  header_row: number;
  name_candidates: ColumnCandidate[] | null;
  photo_candidates: ColumnCandidate[] | null;
  resolved_name_column: string | null;
  resolved_photo_column: string | null;
}

interface PreviewRow {
  row_number: number;
  participant_id: string | null;
  name: string;
  has_photo: boolean;
  status: "ready" | "missing_photo" | "duplicate" | "duplicate_in_file" | "invalid";
  message: string;
}

interface PreviewOkResponse {
  status: "ok";
  import_id: string;
  name_column: string;
  photo_column: string | null;
  total_rows: number;
  ready_count: number;
  missing_photo_count: number;
  new_count: number;
  duplicate_count: number;
  invalid_count: number;
  preview: PreviewRow[];
}

type PreviewResponse = NeedsColumnSelectionResponse | PreviewOkResponse;

interface ImportRowStatus {
  id: string;
  row_number: number;
  participant_id: string | null;
  first_name: string | null;
  status: string;
  error_message: string | null;
}

interface ImportJobStatus {
  id: string;
  filename: string;
  total_rows: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  status: string;
  current_stage: string;
}

type Stage = "upload" | "columns" | "preview" | "importing" | "done";

export default function Import() {
  const [stage, setStage] = useState<Stage>("upload");
  const [needsSelection, setNeedsSelection] = useState<NeedsColumnSelectionResponse | null>(null);
  const [chosenName, setChosenName] = useState<string | null>(null);
  const [chosenPhoto, setChosenPhoto] = useState<string | null | undefined>(undefined); // undefined = not chosen yet, null = "no photo column"
  const [preview, setPreview] = useState<PreviewOkResponse | null>(null);
  const [duplicateStrategy, setDuplicateStrategy] = useState("skip");
  const [job, setJob] = useState<ImportJobStatus | null>(null);
  const [rows, setRows] = useState<ImportRowStatus[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const pollRef = useRef<number | null>(null);
  const fileRef = useRef<File | null>(null); // kept so we can resend it once the user resolves an ambiguous column

  async function submitPreview(nameColumn?: string, photoColumn?: string | null) {
    if (!fileRef.current) return;
    setError("");
    setLoading(true);
    try {
      const form = new FormData();
      form.append("file", fileRef.current);
      if (nameColumn) form.append("name_column", nameColumn);
      if (photoColumn === null) {
        form.append("photo_column_none", "true");
      } else if (photoColumn) {
        form.append("photo_column", photoColumn);
      }
      const data: PreviewResponse = await apiPostForm("/api/import/preview", form);
      if (data.status === "needs_column_selection") {
        setNeedsSelection(data);
        setChosenName(data.resolved_name_column);
        setChosenPhoto(data.resolved_photo_column);
        setStage("columns");
      } else {
        setPreview(data);
        setStage("preview");
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not read the file.");
      setStage("upload");
    } finally {
      setLoading(false);
    }
  }

  async function handleFile(file: File) {
    fileRef.current = file;
    await submitPreview();
  }

  function confirmColumnChoice() {
    if (!needsSelection) return;
    const needName = needsSelection.needs.includes("name");
    const needPhoto = needsSelection.needs.includes("photo");
    if (needName && !chosenName) return;
    if (needPhoto && chosenPhoto === undefined) return;
    submitPreview(chosenName ?? undefined, needPhoto ? chosenPhoto : undefined);
  }

  async function startImport() {
    if (!preview) return;
    const form = new FormData();
    form.append("duplicate_strategy", duplicateStrategy);
    await apiPostForm(`/api/import/${preview.import_id}/confirm`, form);
    setStage("importing");
  }

  useEffect(() => {
    if (stage !== "importing" || !preview) return;
    async function poll() {
      const data = await apiGet(`/api/imports/${preview!.import_id}`);
      setJob(data.job);
      setRows(data.rows);
      if (data.job.status === "completed" || data.job.status === "failed") {
        setStage("done");
        if (pollRef.current) window.clearInterval(pollRef.current);
      }
    }
    poll();
    pollRef.current = window.setInterval(poll, 1000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage]);

  function reset() {
    setStage("upload");
    setNeedsSelection(null);
    setChosenName(null);
    setChosenPhoto(undefined);
    setPreview(null);
    setJob(null);
    setRows([]);
    setError("");
    fileRef.current = null;
  }

  const progressPct = job ? Math.round(((job.success_count + job.failed_count + job.skipped_count) / job.total_rows) * 100) : 0;

  return (
    <div>
      <PageHeader
        title="Import Participants"
        subtitle="Upload any CSV or Excel file — the system finds the Name and Photo columns itself. No fixed template required."
        action={
          <Button variant="secondary" onClick={() => downloadFile("/api/import/template", "participant_import_example.xlsx")}>
            Download Example
          </Button>
        }
      />

      {stage === "upload" && (
        <Card>
          <div
            onClick={() => document.getElementById("import-file")?.click()}
            className="border-2 border-dashed border-gray-300 rounded-lg p-16 text-center cursor-pointer hover:border-indigo-400 text-gray-500"
          >
            {loading ? "Reading spreadsheet..." : "Click to upload a CSV or Excel file"}
          </div>
          <input
            id="import-file"
            type="file"
            accept=".csv,.xlsx,.xls"
            className="hidden"
            onChange={(e) => e.target.files?.[0] && handleFile(e.target.files[0])}
          />
          <p className="text-xs text-gray-400 mt-3">
            Any column layout works — the system looks for a column that contains participant names and a column that contains
            photos (URLs, Google Drive links, or image filenames). Every other column is ignored.
          </p>
          {error && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2 mt-3">{error}</div>}
        </Card>
      )}

      {stage === "columns" && needsSelection && (
        <Card>
          <h2 className="font-semibold text-gray-900 mb-1">Which column is which?</h2>
          <p className="text-sm text-gray-500 mb-5">
            The system couldn't confidently tell on its own — pick the right column below.
          </p>

          {needsSelection.needs.includes("name") && needsSelection.name_candidates && (
            <div className="mb-6">
              <div className="text-sm font-medium text-gray-700 mb-2">Which column contains the participant's name?</div>
              <div className="space-y-2">
                {needsSelection.name_candidates
                  .filter((c) => c.column !== null)
                  .map((c) => (
                    <button
                      key={c.column}
                      onClick={() => setChosenName(c.column)}
                      className={`w-full text-left px-4 py-2.5 rounded-lg border text-sm transition-colors ${
                        chosenName === c.column ? "border-indigo-500 bg-indigo-50" : "border-gray-200 hover:border-gray-300"
                      }`}
                    >
                      <div className="font-medium text-gray-900">{c.column}</div>
                      <div className="text-xs text-gray-500 truncate">e.g. {c.samples.join(", ")}</div>
                    </button>
                  ))}
              </div>
            </div>
          )}

          {needsSelection.needs.includes("photo") && needsSelection.photo_candidates && (
            <div className="mb-6">
              <div className="text-sm font-medium text-gray-700 mb-2">Which column contains the participant's photo?</div>
              <div className="space-y-2">
                {needsSelection.photo_candidates.map((c) => (
                  <button
                    key={c.column ?? "__none__"}
                    onClick={() => setChosenPhoto(c.column)}
                    className={`w-full text-left px-4 py-2.5 rounded-lg border text-sm transition-colors ${
                      chosenPhoto === c.column ? "border-indigo-500 bg-indigo-50" : "border-gray-200 hover:border-gray-300"
                    }`}
                  >
                    <div className="font-medium text-gray-900">{c.column ?? c.label}</div>
                    {c.column && <div className="text-xs text-gray-500 truncate">e.g. {c.samples.join(", ")}</div>}
                  </button>
                ))}
              </div>
            </div>
          )}

          {error && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2 mb-3">{error}</div>}

          <div className="flex gap-2">
            <Button variant="secondary" onClick={reset}>
              Cancel
            </Button>
            <Button
              onClick={confirmColumnChoice}
              disabled={
                loading ||
                (needsSelection.needs.includes("name") && !chosenName) ||
                (needsSelection.needs.includes("photo") && chosenPhoto === undefined)
              }
            >
              Continue
            </Button>
          </div>
        </Card>
      )}

      {stage === "preview" && preview && (
        <>
          <Card className="mb-4">
            <h2 className="font-semibold text-gray-900 mb-3">Import Summary</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <div className="text-2xl font-bold text-gray-900">{preview.total_rows}</div>
                <div className="text-xs text-gray-500">Total rows detected</div>
              </div>
              <div>
                <div className="text-2xl font-bold text-green-600">{preview.new_count}</div>
                <div className="text-xs text-gray-500">New participants</div>
              </div>
              <div>
                <div className="text-2xl font-bold text-amber-600">{preview.duplicate_count}</div>
                <div className="text-xs text-gray-500">Duplicates skipped</div>
              </div>
              <div>
                <div className="text-2xl font-bold text-red-600">{preview.invalid_count}</div>
                <div className="text-xs text-gray-500">Invalid records</div>
              </div>
            </div>
            {preview.duplicate_count > 0 && (
              <p className="text-xs text-gray-400 mt-3">
                Duplicate participants were not added — the existing participant's data and photo are unchanged.
              </p>
            )}
          </Card>

          <Card className="p-0 overflow-hidden mb-4">
            <div className="px-4 py-2.5 bg-gray-50 border-b border-gray-100 text-xs text-gray-500">
              Detected: Name = <span className="font-medium text-gray-700">{preview.name_column}</span>
              {preview.photo_column && (
                <>
                  {" "}
                  · Photo = <span className="font-medium text-gray-700">{preview.photo_column}</span>
                </>
              )}
              {" "}
              — every other column was ignored.
            </div>
            <div className="max-h-96 overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-gray-500 text-xs uppercase sticky top-0">
                  <tr>
                    <th className="text-left px-4 py-2">ID</th>
                    <th className="text-left px-4 py-2">Name</th>
                    <th className="text-left px-4 py-2">Photo</th>
                    <th className="text-left px-4 py-2">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {preview.preview.map((r) => (
                    <tr key={r.row_number} className={r.status === "duplicate" || r.status === "duplicate_in_file" || r.status === "invalid" ? "opacity-60" : ""}>
                      <td className="px-4 py-2 font-mono text-gray-500">{r.participant_id ?? "—"}</td>
                      <td className="px-4 py-2">{r.name || <span className="text-gray-400 italic">(blank)</span>}</td>
                      <td className="px-4 py-2">{r.has_photo ? "✓" : "✕"}</td>
                      <td className="px-4 py-2">
                        {r.status === "ready" && <Badge tone="good">NEW - READY</Badge>}
                        {r.status === "missing_photo" && <Badge tone="warn">Missing Photo</Badge>}
                        {r.status === "duplicate" && <Badge tone="bad">DUPLICATE - SKIP</Badge>}
                        {r.status === "duplicate_in_file" && <Badge tone="bad">DUPLICATE IN FILE - SKIP</Badge>}
                        {r.status === "invalid" && <Badge tone="bad">Invalid — {r.message}</Badge>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-2 text-sm">
              <span className="text-gray-600">If a Participant ID already exists:</span>
              <select
                value={duplicateStrategy}
                onChange={(e) => setDuplicateStrategy(e.target.value)}
                className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm"
              >
                <option value="skip">Skip</option>
                <option value="update">Update Existing</option>
              </select>
            </div>
            <div className="flex gap-2">
              <Button variant="secondary" onClick={reset}>
                Cancel
              </Button>
              <Button onClick={startImport} disabled={preview.new_count === 0}>
                Import {preview.new_count} Participant(s)
              </Button>
            </div>
          </Card>
        </>
      )}

      {stage === "importing" && (
        <Card>
          <h2 className="font-semibold text-gray-900 mb-3">Importing Participants...</h2>
          <div className="w-full bg-gray-100 rounded-full h-3 mb-2">
            <div className="bg-indigo-600 h-3 rounded-full transition-all" style={{ width: `${progressPct}%` }} />
          </div>
          <div className="text-sm text-gray-600 mb-4">
            {job ? `${job.success_count + job.failed_count + job.skipped_count} / ${job.total_rows} participants` : "Starting..."}
          </div>
          <div className="flex gap-4 text-sm mb-3">
            <span className="text-green-600">✓ {job?.success_count ?? 0} imported</span>
            <span className="text-amber-600">⚠ {job?.skipped_count ?? 0} skipped</span>
            <span className="text-red-600">✕ {job?.failed_count ?? 0} failed</span>
          </div>
          {job?.current_stage && <div className="text-xs text-gray-400">{job.current_stage}</div>}
          <Spinner />
        </Card>
      )}

      {stage === "done" && job && (
        <Card>
          <h2 className="font-semibold text-gray-900 mb-4 text-lg">Import Complete</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-5">
            <div>
              <div className="text-2xl font-bold">{job.total_rows}</div>
              <div className="text-xs text-gray-500">Total Rows</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-green-600">{job.success_count}</div>
              <div className="text-xs text-gray-500">Imported</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-amber-600">{job.skipped_count}</div>
              <div className="text-xs text-gray-500">Skipped</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-red-600">{job.failed_count}</div>
              <div className="text-xs text-gray-500">Failed</div>
            </div>
          </div>

          {rows.some((r) => r.status === "error" || r.status === "skipped") && (
            <div className="mb-4">
              <h3 className="font-medium text-sm mb-2">Issues</h3>
              <div className="divide-y divide-gray-100 max-h-64 overflow-y-auto border border-gray-100 rounded-lg">
                {rows
                  .filter((r) => r.status === "error" || r.status === "skipped")
                  .map((r) => (
                    <div key={r.id} className="px-3 py-2 text-sm flex justify-between gap-3">
                      <span>
                        {r.participant_id} — {r.first_name}
                      </span>
                      <span className="text-gray-500 text-right">{r.error_message}</span>
                    </div>
                  ))}
              </div>
            </div>
          )}

          <div className="flex gap-2">
            <Button
              variant="secondary"
              onClick={() => downloadFile(`/api/imports/${preview?.import_id}/errors.csv`, "import_errors.csv")}
            >
              Download Error Report
            </Button>
            <Button onClick={reset}>Import Another File</Button>
          </div>
        </Card>
      )}
    </div>
  );
}
