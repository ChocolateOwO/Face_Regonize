"""Reads a Google Sheet through its public CSV export endpoint.

Deliberately not the Sheets API. The app's Google OAuth uses the `drive.file`
scope, which only ever reaches files this app created or the user explicitly
picked, so it cannot open an arbitrary sheet whose link was pasted in. Rather
than add a broader scope (re-consent, a restricted scope review) this reuses
the same model already used for participant photos: the user shares the sheet
as "anyone with the link can view", and it is read anonymously.

That means the sheet's sharing setting is the access control. A sheet that is
not link-viewable returns Google's HTML sign-in page instead of CSV, which is
detected and reported as a sharing problem rather than a parse failure.
"""
from __future__ import annotations

import re

import requests

SHEET_URL_RE = re.compile(r"docs\.google\.com/spreadsheets/d/(?:e/)?([a-zA-Z0-9-_]+)")
GID_RE = re.compile(r"[#&?]gid=([0-9]+)")

# Two endpoints can return a sheet as CSV, and they do NOT accept the same
# sheets. /export answers 400 with an HTML page for a sheet that is merely
# link-shared (it wants an authenticated session, or a "publish to web"
# sheet), while /gviz/tq serves exactly that case anonymously. gviz is
# therefore tried first, with /export kept as the fallback because it is the
# higher-fidelity export for sheets that do allow it.
GVIZ_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid={gid}"
EXPORT_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


class GoogleSheetError(RuntimeError):
    """Anything that stops us reading the sheet, phrased for the operator."""


def parse_sheet_url(url: str) -> tuple[str, str]:
    """Return (spreadsheet_id, gid). gid defaults to "0" — the first tab.

    Accepts the ordinary /edit link, a /view link, a published link, and a
    bare spreadsheet id, because people paste all of those.
    """
    url = (url or "").strip()
    if not url:
        raise GoogleSheetError("Paste a Google Sheet link first.")

    match = SHEET_URL_RE.search(url)
    if match:
        sheet_id = match.group(1)
    elif re.fullmatch(r"[a-zA-Z0-9-_]{20,}", url):
        sheet_id = url  # someone pasted just the id
    else:
        raise GoogleSheetError(
            "That does not look like a Google Sheet link. It should look like "
            "https://docs.google.com/spreadsheets/d/…"
        )

    gid_match = GID_RE.search(url)
    return sheet_id, (gid_match.group(1) if gid_match else "0")


def _try_csv_endpoint(endpoint: str, timeout: int) -> tuple[bytes | None, str]:
    """Fetch one candidate endpoint. Returns (csv_bytes, "") on success, or
    (None, reason) so the caller can fall through to the next one."""
    try:
        resp = requests.get(endpoint, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        return None, f"Could not reach Google Sheets ({e})."

    # An unshared sheet is answered with a sign-in / error PAGE, not an error
    # status, so the status code alone does not tell us whether this worked.
    content_type = resp.headers.get("Content-Type", "")
    looks_like_html = "text/html" in content_type or resp.content[:15].lstrip().lower().startswith(b"<!doctype")

    if resp.status_code == 404:
        return None, "not_found"
    if resp.status_code in (401, 403) or looks_like_html:
        return None, "not_shared"
    if resp.status_code != 200:
        return None, f"Google Sheets returned HTTP {resp.status_code}."
    if not resp.content.strip():
        return None, "empty"
    return resp.content, ""


def fetch_sheet_csv(url: str, timeout: int = 30) -> bytes:
    """Download one tab of a shared sheet as CSV bytes."""
    sheet_id, gid = parse_sheet_url(url)

    reasons = []
    for endpoint in (
        GVIZ_URL.format(sheet_id=sheet_id, gid=gid),
        EXPORT_URL.format(sheet_id=sheet_id, gid=gid),
    ):
        content, reason = _try_csv_endpoint(endpoint, timeout)
        if content is not None:
            return content
        reasons.append(reason)

    if "not_found" in reasons:
        raise GoogleSheetError("That sheet was not found. Check the link is correct and the sheet still exists.")
    if "empty" in reasons:
        raise GoogleSheetError("That sheet tab is empty.")
    if "not_shared" in reasons:
        raise GoogleSheetError(
            "This sheet is not shared publicly. Open it in Google Sheets, choose "
            "Share → General access → “Anyone with the link” → Viewer, then try again."
        )
    raise GoogleSheetError(reasons[0])


def sheet_display_name(url: str) -> str:
    """A short, stable label for the ImportJob record."""
    sheet_id, gid = parse_sheet_url(url)
    return f"Google Sheet {sheet_id[:12]}… (tab {gid})"
