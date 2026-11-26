/**
 * Language handoff from the Mice Hub: every link the Hub opens carries
 * ?lang=th|en|zh|ja. A supported value is remembered for this browser
 * (localStorage) and survives navigation and refresh; an unsupported or
 * missing value keeps the remembered choice, or the default (English).
 *
 * What it changes today: <html lang>, and the Help link opens the guide in
 * that language (the Help package is the app's only localized resource:
 * /help/index.html in English, /help/guide-<th|ja|zh>.html). The app's own
 * screens are English only - there is no UI translation yet.
 */

export const LANGUAGES = ["th", "en", "zh", "ja"] as const;
export type Language = (typeof LANGUAGES)[number];
export const DEFAULT_LANGUAGE: Language = "en";
export const LANGUAGE_KEY = "reconize_lang";

type Store = Pick<Storage, "getItem" | "setItem">;

function storage(): Store | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function isLanguage(value: unknown): value is Language {
  return typeof value === "string" && (LANGUAGES as readonly string[]).includes(value);
}

/** The language for this page view: ?lang= when supported (and then saved),
 * otherwise the saved one, otherwise the default. Never throws. */
export function resolveLanguage(search: string, store: Store | null = storage()): Language {
  let wanted: string | null = null;
  try {
    wanted = new URLSearchParams(search).get("lang");
  } catch {
    wanted = null;
  }
  const value = wanted?.trim().toLowerCase();
  if (isLanguage(value)) {
    try {
      store?.setItem(LANGUAGE_KEY, value);
    } catch {
      /* private mode: the choice simply is not remembered */
    }
    return value;
  }
  let saved: string | null = null;
  try {
    saved = store?.getItem(LANGUAGE_KEY) ?? null;
  } catch {
    saved = null;
  }
  return isLanguage(saved) ? saved : DEFAULT_LANGUAGE;
}

export function helpUrl(language: Language): string {
  return language === "en" ? "/help/index.html" : `/help/guide-${language}.html`;
}

/** Apply to the document (called on every navigation). */
export function applyLanguage(search: string, doc: Document | null = typeof document !== "undefined" ? document : null): Language {
  const language = resolveLanguage(search);
  if (doc) doc.documentElement.lang = language;
  return language;
}
