import { useEffect, useState } from "react";
import { apiPutJson } from "../api/client";
import LanguageComboBox, { LANGUAGE_ERROR, isLanguageTag } from "./LanguageComboBox";
import { Button, Input } from "./ui";

const MAX_TITLE = 40;
const LANGUAGE_ERROR_ID = "voice-language-error";

/** Title/position and the language a voice/robot client should speak to this
 *  participant. Admin-entered only; empty clears a field. Saved through its
 *  own endpoint so nothing else about the person changes. Nothing is saved
 *  until Save is pressed. */
export default function VoiceProfileEditor({
  personId,
  title,
  language,
  onSaved,
}: {
  personId: string;
  title: string | null;
  language: string | null;
  onSaved: (title: string | null, language: string | null) => void;
}) {
  const [draftTitle, setDraftTitle] = useState(title ?? "");
  const [draftLanguage, setDraftLanguage] = useState(language ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [languageError, setLanguageError] = useState("");

  useEffect(() => {
    setDraftTitle(title ?? "");
    setDraftLanguage(language ?? "");
  }, [title, language]);

  const changed =
    draftTitle.trim() !== (title ?? "") || draftLanguage.trim().toLowerCase() !== (language ?? "").toLowerCase();

  async function save() {
    setError("");
    setLanguageError("");
    // Same rule as the backend, checked first so a bad value is never sent.
    if (draftLanguage.trim() && !isLanguageTag(draftLanguage)) {
      setLanguageError(LANGUAGE_ERROR);
      return;
    }
    setSaving(true);
    try {
      const out = await apiPutJson(`/api/people/${personId}/voice-profile`, { title: draftTitle, language: draftLanguage });
      onSaved(out.title ?? null, out.language ?? null);
    } catch (e) {
      const message = e instanceof Error ? e.message : "Could not save.";
      if (message.startsWith("Language")) setLanguageError(message);
      else setError(message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="pt-2 border-t border-gray-100 mt-2">
      <label className="block">
        <strong>คำนำหน้า/ตำแหน่ง / Title:</strong>
        <Input
          aria-label="Title"
          className="mt-1"
          value={draftTitle}
          maxLength={MAX_TITLE}
          placeholder="e.g. ดร., อาจารย์, CEO (empty = none)"
          onChange={(e) => setDraftTitle(e.target.value)}
        />
      </label>
      <div className="block mt-2">
        <strong>ภาษา / Language:</strong>
        <div className="flex flex-wrap gap-2 mt-1 items-start">
          <LanguageComboBox
            value={draftLanguage}
            invalid={languageError !== ""}
            describedBy={languageError ? LANGUAGE_ERROR_ID : undefined}
            onChange={(next) => {
              setDraftLanguage(next);
              setLanguageError("");
            }}
          />
          <Button variant="secondary" disabled={saving || !changed} onClick={save}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </div>
        {languageError && (
          <div id={LANGUAGE_ERROR_ID} role="alert" className="text-xs text-red-600 mt-1">
            {languageError}
          </div>
        )}
      </div>
      <div className="text-xs text-gray-500 mt-1">
        Used by the robot/voice greeting. Never guessed automatically. Language: pick from the list or type a code (e.g. de-DE).
      </div>
      {error && <div className="text-xs text-red-600 mt-1">{error}</div>}
    </div>
  );
}
