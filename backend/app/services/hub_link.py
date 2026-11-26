"""The one trusted Mice Hub address (MICE_HUB_URL in .env).

Everything Reconize asks of the Hub - its camera list and MJPEG streams
(services/hub_camera.py) and GPU leases (services/hub_scheduler.py) - goes to
this ONE configured base URL. It is deliberately not a general URL fetcher:

* only plain http on this PC's loopback (127.0.0.1, ::1 or localhost),
  with an explicit port - the Hub's scheduler only serves loopback callers,
  and its camera relay is meant for programs on the same PC;
* no user name/password in the URL, and no path/query/fragment: callers
  append only fixed Hub paths;
* redirects are never followed (callers pass allow_redirects=False).

Empty or invalid means "no Hub": every Hub feature is then off and the rest of
Reconize behaves exactly as before.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def parse(raw: str | None) -> tuple[str | None, str]:
    """(base_url or None, reason). Never raises."""
    value = (raw or "").strip()
    if not value:
        return None, "MICE_HUB_URL is not set"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None, "MICE_HUB_URL is not a valid URL"
    if parts.scheme != "http":
        return None, "MICE_HUB_URL must start with http://"
    if parts.username or parts.password:
        return None, "MICE_HUB_URL must not contain a user name or password"
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        return None, "MICE_HUB_URL must point at this PC (127.0.0.1, ::1 or localhost)"
    if not port:
        return None, "MICE_HUB_URL needs an explicit port, e.g. http://127.0.0.1:8642"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None, "MICE_HUB_URL must be only scheme, host and port"
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}", "configured"


def base_url() -> str | None:
    return parse(os.environ.get("MICE_HUB_URL"))[0]


def status() -> dict:
    base, reason = parse(os.environ.get("MICE_HUB_URL"))
    return {"configured": base is not None, "reason": reason}
