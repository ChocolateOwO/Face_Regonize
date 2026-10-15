import { API_BASE, apiPostJson } from "../api/client";

// Stored images (/api/files/...) are shown with plain <img>/<a> tags, which
// cannot send the Authorization header. The backend instead accepts an
// HttpOnly cookie that only authorizes reading files. Login sets it; this
// re-issues it once per page load for a tab whose session started earlier,
// before any protected page (and its images) renders.
let pending: Promise<void> | null = null;

export function ensureFileSession(): Promise<void> {
  // A failure must never block the app: the page still renders, only stored
  // images would be unavailable. A 401 already signs the user out (client.ts).
  pending ??= apiPostJson("/api/auth/file-session", {}).then(
    () => undefined,
    () => undefined,
  );
  return pending;
}

export function endFileSession(): void {
  pending = null;
  void fetch(`${API_BASE}/api/auth/file-session`, { method: "DELETE" }).catch(() => undefined);
}
