import { useEffect, useId, useRef, useState } from "react";

/** Editable combobox for a participant's spoken language.
 *
 *  Pick one of the common languages (saved as its code: ไทย -> "th"), or type
 *  any other language tag ("de-DE", "pt-BR"). The list is a shortcut, never a
 *  restriction: whatever the backend accepts can be typed. `value` is what
 *  will be saved ("" = not set); typing reports the resolved value on every
 *  change, nothing is saved until the parent's Save. */

export interface LanguageOption {
  code: string;
  label: string;
  /** Extra search words (English / Thai names). */
  aliases: string[];
}

export const LANGUAGE_OPTIONS: LanguageOption[] = [
  { code: "th", label: "ไทย", aliases: ["thai", "ภาษาไทย"] },
  { code: "en", label: "English", aliases: ["อังกฤษ", "ภาษาอังกฤษ"] },
  { code: "zh", label: "中文 (จีน)", aliases: ["chinese", "จีน", "ภาษาจีน", "mandarin"] },
  { code: "ja", label: "日本語 (ญี่ปุ่น)", aliases: ["japanese", "ญี่ปุ่น", "ภาษาญี่ปุ่น"] },
  { code: "ko", label: "한국어 (เกาหลี)", aliases: ["korean", "เกาหลี", "ภาษาเกาหลี"] },
  { code: "fr", label: "Français (ฝรั่งเศส)", aliases: ["french", "francais", "ฝรั่งเศส", "ภาษาฝรั่งเศส"] },
];

export const NO_LANGUAGE_LABEL = "ไม่ระบุภาษา";
export const LANGUAGE_ERROR = "Language must be a language tag such as th, en or en-US.";
const MAX_LANGUAGE = 35;
// Same shape the backend accepts (services/person_address.normalize_language).
const LANGUAGE_RE = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$/;

const norm = (s: string) => s.trim().toLowerCase().replace(/_/g, "-");

/** True when `text` has the shape of a language tag the backend accepts. */
export function isLanguageTag(text: string): boolean {
  const tag = text.trim().replace(/_/g, "-");
  return tag.length <= MAX_LANGUAGE && LANGUAGE_RE.test(tag);
}

/** What typed text means: a known option's code when the text is that
 *  option's code, label or alias (any case); otherwise the text itself,
 *  trimmed, for the backend to normalise or reject. "" = not set. */
export function resolveLanguage(text: string): string {
  const t = norm(text);
  if (!t || t === norm(NO_LANGUAGE_LABEL)) return "";
  const hit = LANGUAGE_OPTIONS.find((o) => o.code === t || norm(o.label) === t || o.aliases.some((a) => norm(a) === t));
  return hit ? hit.code : text.trim();
}

/** How a saved value is shown: the option label for a known code, the code
 *  itself for anything else (never blank just because it is not listed). */
export function displayLanguage(value: string | null | undefined): string {
  if (!value) return "";
  const hit = LANGUAGE_OPTIONS.find((o) => o.code === value.toLowerCase());
  return hit ? hit.label : value;
}

/** Options whose code, label or alias contains the query. */
export function filterLanguages(query: string): LanguageOption[] {
  const q = norm(query);
  if (!q) return LANGUAGE_OPTIONS;
  return LANGUAGE_OPTIONS.filter((o) => o.code.includes(q) || norm(o.label).includes(q) || o.aliases.some((a) => norm(a).includes(q)));
}

type Row = { kind: "none" } | { kind: "option"; option: LanguageOption } | { kind: "custom"; code: string };

export default function LanguageComboBox({
  value,
  onChange,
  invalid = false,
  describedBy,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  invalid?: boolean;
  describedBy?: string;
  disabled?: boolean;
}) {
  const [text, setText] = useState(displayLanguage(value));
  const [open, setOpen] = useState(false);
  const [filtering, setFiltering] = useState(false);
  const [active, setActive] = useState(-1);
  const [upward, setUpward] = useState(false);
  const [alignRight, setAlignRight] = useState(false);
  const focused = useRef(false);
  const wrapper = useRef<HTMLDivElement | null>(null);
  const baseId = useId();
  const listId = `${baseId}-list`;

  // A value set from outside (load, save, reload) is shown as its label/code,
  // but never while the user is typing in the box.
  useEffect(() => {
    if (!focused.current) setText(displayLanguage(value));
  }, [value]);

  const query = filtering ? text : "";
  const matches = filterLanguages(query);
  const typed = text.trim();
  const exact = typed !== "" && LANGUAGE_OPTIONS.some((o) => resolveLanguage(typed) === o.code);
  const rows: Row[] = [];
  if (!filtering || !typed || norm(NO_LANGUAGE_LABEL).includes(norm(typed))) rows.push({ kind: "none" });
  for (const option of matches) rows.push({ kind: "option", option });
  if (filtering && typed && !exact && isLanguageTag(typed)) rows.push({ kind: "custom", code: typed });

  function openList(showAll: boolean) {
    if (disabled) return;
    if (showAll) setFiltering(false);
    // Open upward when the page has no room below, so the list is never cut off.
    const box = wrapper.current?.getBoundingClientRect?.();
    if (box && typeof window !== "undefined") {
      setUpward(window.innerHeight - box.bottom < 300 && box.top > 300);
      // The list may be wider than the box (up to 18rem); keep it on screen.
      setAlignRight(box.left + 288 > window.innerWidth && box.right > 288);
    }
    setOpen(true);
  }

  function choose(row: Row) {
    const next = row.kind === "none" ? "" : row.kind === "option" ? row.option.code : row.code;
    onChange(next);
    setText(row.kind === "custom" ? row.code : displayLanguage(next));
    setFiltering(false);
    setOpen(false);
    setActive(-1);
  }

  function onKeyDown(e: { key: string; preventDefault: () => void }) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        openList(!filtering);
        setActive(e.key === "ArrowDown" ? 0 : rows.length - 1);
        return;
      }
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (rows.length ? (i + step + rows.length) % rows.length : -1));
    } else if (e.key === "Home" && open) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End" && open) {
      e.preventDefault();
      setActive(rows.length - 1);
    } else if (e.key === "Enter") {
      if (open && active >= 0 && active < rows.length) {
        e.preventDefault();
        choose(rows[active]);
      } else {
        setOpen(false);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
        setActive(-1);
      }
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  }

  const rowLabel = (row: Row) =>
    row.kind === "none" ? NO_LANGUAGE_LABEL : row.kind === "option" ? row.option.label : `ใช้ "${row.code}" (พิมพ์เอง)`;
  const rowCode = (row: Row) => (row.kind === "none" ? "" : row.kind === "option" ? row.option.code : row.code);
  const selected = (row: Row) => rowCode(row).toLowerCase() === (value || "").toLowerCase();

  return (
    <div ref={wrapper} className="relative flex-1 min-w-[6rem]" style={{ colorScheme: "light" }}>
      <div
        className={`flex items-center w-full bg-white text-gray-900 border rounded-lg text-sm focus-within:ring-2 focus-within:ring-indigo-500 ${
          invalid ? "border-red-500" : "border-gray-300"
        }`}
      >
        <input
          type="text"
          role="combobox"
          aria-label="Language"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-invalid={invalid || undefined}
          aria-describedby={describedBy}
          aria-activedescendant={open && active >= 0 ? `${baseId}-opt-${active}` : undefined}
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
          value={text}
          maxLength={MAX_LANGUAGE + 10}
          placeholder="เลือกหรือพิมพ์ เช่น th, en, de-DE"
          className="flex-1 min-w-0 px-3 py-2 bg-transparent rounded-lg focus:outline-none placeholder:text-gray-400"
          onFocus={() => {
            focused.current = true;
            openList(true);
          }}
          onClick={() => openList(!filtering)}
          onBlur={() => {
            focused.current = false;
            setOpen(false);
            setFiltering(false);
            setActive(-1);
            setText(displayLanguage(value));
          }}
          onChange={(e) => {
            const next = e.target.value;
            setText(next);
            setFiltering(true);
            setActive(-1);
            if (!open) openList(false);
            onChange(resolveLanguage(next));
          }}
          onKeyDown={onKeyDown}
        />
        {text !== "" && !disabled && (
          <button
            type="button"
            aria-label="Clear language"
            tabIndex={-1}
            className="px-2 py-2 text-gray-400 hover:text-gray-700"
            onPointerDown={(e) => e.preventDefault()}
            onClick={() => choose({ kind: "none" })}
          >
            ×
          </button>
        )}
        <button
          type="button"
          aria-label="Show languages"
          tabIndex={-1}
          disabled={disabled}
          className="px-2 py-2 text-gray-500 hover:text-gray-800"
          onPointerDown={(e) => e.preventDefault()}
          onClick={() => (open ? setOpen(false) : openList(true))}
        >
          ▾
        </button>
      </div>
      {open && (
        <ul
          id={listId}
          role="listbox"
          aria-label="Languages"
          className={`absolute z-50 min-w-full w-max max-w-[18rem] max-h-64 overflow-auto bg-white text-gray-900 border border-gray-200 rounded-lg shadow-lg py-1 ${
            upward ? "bottom-full mb-1" : "top-full mt-1"
          } ${alignRight ? "right-0" : "left-0"}`}
        >
          {rows.map((row, i) => (
            <li
              key={`${row.kind}-${rowCode(row)}`}
              id={`${baseId}-opt-${i}`}
              role="option"
              aria-selected={selected(row)}
              className={`flex items-center justify-between gap-2 px-3 py-2 text-sm cursor-pointer select-none ${
                i === active ? "bg-indigo-50 text-indigo-900" : "hover:bg-gray-50"
              } ${row.kind === "none" ? "text-gray-500" : ""}`}
              // Keep focus in the input so a tap/click selects instead of blurring first.
              onPointerDown={(e) => e.preventDefault()}
              onMouseDown={(e) => e.preventDefault()}
              onPointerEnter={() => setActive(i)}
              onClick={() => choose(row)}
            >
              <span className="whitespace-nowrap">{rowLabel(row)}</span>
              {row.kind === "option" && <span className="text-xs text-gray-400 tabular-nums">{row.option.code}</span>}
            </li>
          ))}
          {rows.length === 0 && (
            <li className="px-3 py-2 text-sm text-gray-500" role="presentation">
              ไม่พบในรายการ — พิมพ์รหัสภาษา เช่น de-DE
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
