import { useState } from "react";

/** Phase O — ONE export selection, used by both "Download ZIP" and
 *  "Upload to Google Drive". ORIGINAL and thumbnails are never exportable,
 *  and there is no REVIEW option. */
export interface ExportSelectionValue {
  media: boolean;
  sorted: boolean;
  ambience: boolean;
  personIds: string[] | null; // null = every matched participant
}

export const DEFAULT_EXPORT_SELECTION: ExportSelectionValue = {
  media: true,
  sorted: true,
  ambience: false,
  personIds: null,
};

export function selectionIsEmpty(sel: ExportSelectionValue): boolean {
  if (!(sel.media || sel.sorted || sel.ambience)) return true;
  return sel.sorted && !sel.media && !sel.ambience && sel.personIds !== null && sel.personIds.length === 0;
}

export function selectionQuery(sel: ExportSelectionValue): string {
  const cats = [sel.media && "media", sel.sorted && "sorted", sel.ambience && "ambience"].filter(Boolean).join(",");
  const params = new URLSearchParams({ select: cats });
  if (sel.sorted && sel.personIds !== null) params.set("person_ids", sel.personIds.join(","));
  return params.toString();
}

export interface ExportParticipant {
  person_id: string;
  label: string;
}

export default function ExportSelectionPanel({
  value,
  onChange,
  participants,
}: {
  value: ExportSelectionValue;
  onChange: (next: ExportSelectionValue) => void;
  participants: ExportParticipant[] | null;
}) {
  const [showPeople, setShowPeople] = useState(false);
  const set = (patch: Partial<ExportSelectionValue>) => onChange({ ...value, ...patch });
  const chosen = new Set(value.personIds ?? participants?.map((p) => p.person_id) ?? []);

  function togglePerson(id: string) {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    const all = participants?.every((p) => next.has(p.person_id));
    set({ personIds: all ? null : [...next] });
  }

  return (
    <div className="text-sm" aria-label="Export selection">
      <div className="font-medium text-gray-900 mb-1">Export / delivery</div>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={value.media} onChange={(e) => set({ media: e.target.checked })} /> MEDIA
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={value.sorted} onChange={(e) => set({ sorted: e.target.checked })} /> People (SORTED)
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={value.ambience} onChange={(e) => set({ ambience: e.target.checked })} /> AMBIENCE
        </label>
        <button type="button" className="text-xs text-indigo-600 hover:underline"
          onClick={() => onChange({ media: true, sorted: true, ambience: true, personIds: null })}>
          Select all
        </button>
        <button type="button" className="text-xs text-gray-500 hover:underline"
          onClick={() => onChange({ media: false, sorted: false, ambience: false, personIds: null })}>
          Clear all
        </button>
      </div>
      {value.sorted && participants && participants.length > 0 && (
        <div className="mt-1">
          <button type="button" className="text-xs text-gray-600 hover:underline" onClick={() => setShowPeople((s) => !s)}>
            {value.personIds === null ? `All ${participants.length} participants` : `${value.personIds.length} of ${participants.length} participants`}
            {showPeople ? " ▲" : " ▼"}
          </button>
          {showPeople && (
            <div className="mt-1 max-h-40 overflow-y-auto border border-gray-200 rounded-lg p-2 grid sm:grid-cols-2 gap-1">
              <label className="flex items-center gap-1.5 col-span-full text-xs font-medium">
                <input type="checkbox" checked={value.personIds === null} onChange={(e) => set({ personIds: e.target.checked ? null : [] })} />
                All participants
              </label>
              {participants.map((p) => (
                <label key={p.person_id} className="flex items-center gap-1.5 text-xs truncate">
                  <input type="checkbox" checked={chosen.has(p.person_id)} onChange={() => togglePerson(p.person_id)} />
                  {p.label}
                </label>
              ))}
            </div>
          )}
        </div>
      )}
      {selectionIsEmpty(value) && <p className="text-xs text-amber-700 mt-1">Select at least one output.</p>}
      <p className="text-xs text-gray-400 mt-1">ORIGINAL photos and thumbnails are never exported.</p>
    </div>
  );
}
