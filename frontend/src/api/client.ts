// Same-origin on purpose. A hardcoded host works only on the machine running
// the server: a browser on another PC would resolve 127.0.0.1 to ITSELF and
// find nothing. Empty means every call goes to whatever host served the page,
// so http://192.168.1.3:8000 just works from any machine on the network - and
// same-origin means no CORS is involved at all.
//
// In dev the Vite server proxies /api to the backend (see vite.config.ts), so
// this is correct in both modes.
export const API_BASE = "";

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem("token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function handle(res: Response) {
  if (res.status === 401) {
    // Let React (AuthContext) own the logout + redirect via client-side
    // routing. A hard location.href reload here previously caused an
    // infinite refresh loop: it only cleared "token", not "username", so
    // ProtectedRoute (which just checks "username") kept sending the
    // browser back to a page that immediately 401'd again.
    window.dispatchEvent(new Event("auth:unauthorized"));
    throw new ApiError(401, "Not authenticated");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || JSON.stringify(data);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return res.json();
  return res.blob();
}

export async function apiGet(path: string) {
  const res = await fetch(`${API_BASE}${path}`, { headers: { ...authHeaders() } });
  return handle(res);
}

export async function apiPostJson(path: string, body: unknown) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  const data = await handle(res);
  clearCachedGets();
  return data;
}

export async function apiPutJson(path: string, body: unknown) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  const data = await handle(res);
  clearCachedGets();
  return data;
}

export async function apiPostForm(path: string, form: FormData) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { ...authHeaders() },
    body: form,
  });
  const data = await handle(res);
  clearCachedGets();
  return data;
}

export async function apiPutForm(path: string, form: FormData) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { ...authHeaders() },
    body: form,
  });
  const data = await handle(res);
  clearCachedGets();
  return data;
}

export async function apiDelete(path: string) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    headers: { ...authHeaders() },
  });
  const data = await handle(res);
  clearCachedGets();
  return data;
}

// `version` is optional on purpose: only participant profile photos need it.
// They live at a fixed path (people/{id}/profile.jpg) that is overwritten in
// place when the photo is replaced, so the URL alone never changes and the
// browser can keep showing the previous occupant's face - including from its
// in-memory cache, which no Cache-Control header reaches. Passing the
// participant's updated_at makes the URL change whenever the photo does.
// Every other caller stores files under unique names and is unaffected.
export function fileUrl(relativePath: string | null | undefined, version?: string): string {
  if (!relativePath) return "";
  const url = `${API_BASE}/api/files/${relativePath.replace(/\\/g, "/")}`;
  return version ? `${url}?v=${encodeURIComponent(version)}` : url;
}

export async function downloadFile(path: string, filename: string) {
  const res = await fetch(`${API_BASE}${path}`, { headers: { ...authHeaders() } });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
import { clearCachedGets } from "../hooks/cachedGetStore";
