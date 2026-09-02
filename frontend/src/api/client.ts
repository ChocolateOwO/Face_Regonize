export const API_BASE = "http://127.0.0.1:8000";

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
  return handle(res);
}

export async function apiPutJson(path: string, body: unknown) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  return handle(res);
}

export async function apiPostForm(path: string, form: FormData) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { ...authHeaders() },
    body: form,
  });
  return handle(res);
}

export async function apiPutForm(path: string, form: FormData) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { ...authHeaders() },
    body: form,
  });
  return handle(res);
}

export async function apiDelete(path: string) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    headers: { ...authHeaders() },
  });
  return handle(res);
}

export function fileUrl(relativePath: string | null | undefined): string {
  if (!relativePath) return "";
  return `${API_BASE}/api/files/${relativePath.replace(/\\/g, "/")}`;
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
