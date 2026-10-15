import { useEffect, useState } from "react";
import { apiPutJson } from "../api/client";
import { Button, Input } from "./ui";

const MAX_CALL_AS = 40;

/** How a voice/robot client should address this participant (e.g. "พี่บอส").
 *  Saved through its own endpoint, so nothing else about the person changes.
 *  Empty clears it and the client falls back to the name. */
export default function CallAsEditor({
  personId,
  value,
  onSaved,
}: {
  personId: string;
  value: string | null;
  onSaved: (callAs: string | null) => void;
}) {
  const [draft, setDraft] = useState(value ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setDraft(value ?? "");
  }, [value]);

  const changed = draft.trim() !== (value ?? "");

  async function save() {
    setSaving(true);
    setError("");
    try {
      const out = await apiPutJson(`/api/people/${personId}/call-as`, { call_as: draft });
      onSaved(out.call_as ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="pt-2 border-t border-gray-100 mt-2">
      <label className="block">
        <strong>เรียกว่า / Call as:</strong>
        <div className="flex gap-2 mt-1">
          <Input
            aria-label="Call as"
            value={draft}
            maxLength={MAX_CALL_AS}
            placeholder="e.g. พี่บอส (empty = use name)"
            onChange={(e) => setDraft(e.target.value)}
          />
          <Button variant="secondary" disabled={saving || !changed} onClick={save}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </div>
      </label>
      <div className="text-xs text-gray-500 mt-1">Used by the robot/voice greeting. Never guessed automatically.</div>
      {error && <div className="text-xs text-red-600 mt-1">{error}</div>}
    </div>
  );
}
