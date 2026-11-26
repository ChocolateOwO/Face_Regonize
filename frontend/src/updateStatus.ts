// The "Update Status" badge in Settings → Application Updates.
// "Up to Date" is shown only after a successful check that found a release
// not newer than the installed version. A repository with no published
// release, and a check that failed (network, rate limit, refused, HTTP error),
// get neutral badges: nothing was compared. The error text itself is shown
// separately by the panel. Install eligibility is NOT decided here: "Update
// Now" still depends only on update_available.

export interface UpdateBadgeInput {
  github_configured: boolean;
  update_available: boolean;
  latest_version: string | null;
  error?: string;
  no_published_release?: boolean;
  check_failed?: boolean;
}

export interface UpdateBadge {
  label: string;
  tone: "good" | "warn" | "default";
}

export function updateStatusBadge(status: UpdateBadgeInput): UpdateBadge | null {
  if (!status.github_configured) return null;
  if (status.update_available) return { label: `● Update Available — v${status.latest_version}`, tone: "warn" };
  if (status.no_published_release) return { label: "● No published release", tone: "default" };
  // Only a successful comparison is "Up to Date": a failed check (or an older
  // backend's result carrying an error and no release) never is.
  if (status.check_failed || status.error || !status.latest_version) return { label: "● Check failed", tone: "default" };
  return { label: "● Up to Date", tone: "good" };
}
