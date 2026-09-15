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

/** `signal` lets a caller cancel a request that is no longer wanted — e.g. the
 *  photo grid when the user pages or switches tab again before the previous
 *  page arrived. Without it the stale response can land last and overwrite the
 *  current one, which is what made fast paging feel like it jumped around. */
export async function apiGet(path: string, signal?: AbortSignal) {
  const res = await fetch(`${API_BASE}${path}`, { headers: { ...authHeaders() }, signal });
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

/** POST with extra request headers — e.g. the marker header the Google Drive
 *  Picker token endpoint requires. Not cached and never stored. */
export async function apiPostJsonWithHeaders(path: string, body: unknown, headers: Record<string, string>) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(), ...headers },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  return handle(res);
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

/** Same as apiPostForm, but reports upload progress. fetch() cannot observe
 *  request-body progress at all, and a photo-batch upload can be gigabytes —
 *  an indeterminate spinner for minutes reads as "nothing is happening". */
export function apiPostFormWithProgress(
  path: string,
  form: FormData,
  onProgress: (percent: number) => void,
): Promise<any> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}${path}`);
    const token = localStorage.getItem("token");
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status === 401) {
        window.dispatchEvent(new Event("auth:unauthorized"));
        reject(new ApiError(401, "Not authenticated"));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        clearCachedGets();
        try {
          resolve(xhr.responseText ? JSON.parse(xhr.responseText) : null);
        } catch {
          resolve(null);
        }
        return;
      }
      let detail = xhr.statusText;
      try {
        const data = JSON.parse(xhr.responseText);
        detail = data.detail || JSON.stringify(data);
      } catch {
        /* not json */
      }
      reject(new ApiError(xhr.status, detail));
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error during upload."));
    xhr.send(form);
  });
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
