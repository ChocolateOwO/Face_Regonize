"""Application update: detection, preflight, and initiation.

This module never performs an update. It decides whether one is available,
proves the machine is in a fit state to attempt it, and hands off to a
detached updater process (tools/updater/updater.py, copied to the runtime
directory and launched from there). Everything after that point belongs to
the updater, because the backend itself is stopped mid-update.

Only a PUBLISHED GitHub Release counts as an available update. A pushed tag
is development, and is deliberately never offered to users, so there is no
fallback to the /tags endpoint anywhere here.

PUBLIC REPOSITORY MODE: the repository is public, so both release detection
and git fetch run anonymously. No token, credential helper, or askpass is
required or used for git. GITHUB_TOKEN remains supported but strictly
optional, and only ever as an Authorization header on API calls to raise the
unauthenticated rate limit — nothing in the required path depends on it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests
from sqlmodel import Session, select

from app.config import (
    APP_VERSION,
    BACKUP_DIR,
    DATABASE_PATH,
    GITHUB_REPO,
    GITHUB_TOKEN,
    PROJECT_ROOT,
    UPDATE_LOCK_FILE,
    UPDATE_STATE_FILE,
    UPDATER_RUNTIME_DIR,
)
from app.database.db import engine
from app.models.models import PhotoBatch, Setting

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
_LAST_CHECK_KEY = "update_last_checked"
_LATEST_CACHE_KEY = "update_latest_cache"

# Minimum free space before an update is allowed: backup + fetch + node_modules.
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


class UpdateError(Exception):
    """A preflight or initiation failure with an admin-readable message."""


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------

def redact(text: str) -> str:
    """Strip anything token-shaped before it reaches a log, a state file, or
    an API response. Applied to every subprocess error we surface."""
    if not text:
        return ""
    out = str(text)
    if GITHUB_TOKEN:
        out = out.replace(GITHUB_TOKEN, "***")
    # Catch tokens that arrived from somewhere other than our own config.
    out = re.sub(r"gh[pousr]_[A-Za-z0-9]{20,}", "***", out)
    out = re.sub(r"github_pat_[A-Za-z0-9_]{20,}", "***", out)
    # https://user:secret@host -> https://***@host
    out = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1***@", out)
    return out


# --------------------------------------------------------------------------
# Version handling
# --------------------------------------------------------------------------

def _parse_version(value: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). Non-numeric parts are ignored so a
    malformed tag sorts low rather than raising."""
    cleaned = (value or "").strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def current_version() -> str:
    return APP_VERSION


def is_newer(candidate: str, baseline: str) -> bool:
    return _parse_version(candidate) > _parse_version(baseline)


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------

def is_configured() -> bool:
    """The repository is public, so GITHUB_REPO alone is sufficient — a token
    is never required for either detection or git fetch."""
    return bool(GITHUB_REPO)


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        # Optional, and only ever for the API rate limit — never for git.
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _api(path: str, timeout: int = 15) -> requests.Response:
    return requests.get(f"{API_ROOT}{path}", headers=_headers(), timeout=timeout)


def _get_setting(session: Session, key: str) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else None


def _set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        row = Setting(key=key, value=value)
    session.add(row)
    session.commit()


def check_for_update(force: bool = False) -> dict:
    """Ask GitHub for the newest PUBLISHED release. Result is cached in the
    existing Setting store so a restart doesn't lose the last known answer."""
    result = {
        "current_version": APP_VERSION,
        "latest_version": None,
        "update_available": False,
        "release_notes": "",
        "release_url": "",
        "release_name": "",
        "last_checked": None,
        "github_configured": is_configured(),
        "error": "",
    }

    if not is_configured():
        result["error"] = "Updates are not configured — set GITHUB_REPO in .env (e.g. owner/Reconize)."
        with Session(engine) as session:
            result["last_checked"] = _get_setting(session, _LAST_CHECK_KEY)
        return result

    with Session(engine) as session:
        cached_raw = _get_setting(session, _LATEST_CACHE_KEY)
        if cached_raw and not force:
            try:
                cached = json.loads(cached_raw)
                cached["current_version"] = APP_VERSION
                cached["update_available"] = bool(
                    cached.get("latest_version") and is_newer(cached["latest_version"], APP_VERSION)
                )
                cached["github_configured"] = True
                cached["last_checked"] = _get_setting(session, _LAST_CHECK_KEY)
                return cached
            except ValueError:
                pass

    try:
        resp = _api(f"/repos/{GITHUB_REPO}/releases/latest")
    except requests.RequestException as e:
        result["error"] = redact(f"Could not reach GitHub: {e}")
        return result

    if resp.status_code == 404:
        # Either no published release yet, or the repo name is wrong. Neither
        # is an application failure — there is simply nothing to offer.
        result["error"] = (
            f"No published release found for '{GITHUB_REPO}'. "
            "Check GITHUB_REPO, and that the repository is public and has a published release."
        )
        _remember(result)
        return result
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        # Anonymous API access is 60 requests/hour per IP. Being unable to
        # check is a temporary, non-destructive condition, never a failure.
        result["error"] = (
            "GitHub's API rate limit was reached. The application is unaffected; "
            "the next check will run automatically later."
        )
        return result
    if resp.status_code in (401, 403):
        result["error"] = (
            "GitHub refused the request. If the repository is private, this build expects a public one."
        )
        return result
    if resp.status_code != 200:
        result["error"] = redact(f"Could not check for updates — GitHub returned HTTP {resp.status_code}.")
        return result

    data = resp.json()
    tag = str(data.get("tag_name", ""))
    result["latest_version"] = tag.lstrip("vV")
    result["release_notes"] = data.get("body") or ""
    result["release_url"] = data.get("html_url") or ""
    result["release_name"] = data.get("name") or tag
    result["update_available"] = bool(tag) and is_newer(tag, APP_VERSION)
    _remember(result)
    return result


def _remember(result: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    result["last_checked"] = now
    with Session(engine) as session:
        _set_setting(session, _LAST_CHECK_KEY, now)
        _set_setting(session, _LATEST_CACHE_KEY, json.dumps(result))


def resolve_tag_sha(tag: str) -> str:
    """Resolve a tag to the exact commit SHA it points at, dereferencing an
    annotated tag object. Pinning the SHA up front is what lets the updater
    prove afterwards that it checked out precisely what was intended."""
    resp = _api(f"/repos/{GITHUB_REPO}/git/ref/tags/{tag}")
    if resp.status_code != 200:
        raise UpdateError(redact(f"Release tag '{tag}' not found on GitHub (HTTP {resp.status_code})."))
    obj = resp.json().get("object", {})
    sha, obj_type = obj.get("sha", ""), obj.get("type", "")
    if not sha:
        raise UpdateError(f"Could not resolve tag '{tag}' to a commit.")
    if obj_type == "tag":  # annotated tag -> dereference to its commit
        tag_resp = _api(f"/repos/{GITHUB_REPO}/git/tags/{sha}")
        if tag_resp.status_code != 200:
            raise UpdateError(redact(f"Could not dereference annotated tag '{tag}'."))
        sha = tag_resp.json().get("object", {}).get("sha", "")
        if not sha:
            raise UpdateError(f"Annotated tag '{tag}' does not point at a commit.")
    return sha


# --------------------------------------------------------------------------
# Git helpers (token never touches argv, the remote URL, or .git/config)
# --------------------------------------------------------------------------

def git_env() -> dict[str, str]:
    """Environment for git subprocesses.

    The repository is public, so git runs ANONYMOUSLY — no credential helper,
    no askpass, no token. GIT_TERMINAL_PROMPT=0 and an empty GIT_ASKPASS are
    both set defensively: if the remote ever does demand credentials (a repo
    made private, a wrong URL), git must fail immediately with a clear error
    instead of blocking forever on a prompt no one can see, since this runs
    headless inside a service.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"  # Git Credential Manager must not pop a window
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def git(*args: str, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or PROJECT_ROOT),
        env=git_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


_REMOTE_PATTERNS = [
    re.compile(r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$", re.I),
    re.compile(r"^https://[^@/]+@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$", re.I),
]
_SSH_PATTERNS = [
    re.compile(r"^git@github\.com:", re.I),
    re.compile(r"^ssh://", re.I),
]


def verify_remote() -> str:
    """Confirm origin exists, matches GITHUB_REPO, is HTTPS, and authenticates
    non-interactively. Never rewrites the remote — a mismatch is the admin's
    to fix, and silently repointing someone's origin would be worse than
    refusing."""
    result = git("remote", "get-url", "origin", timeout=20)
    if result.returncode != 0:
        raise UpdateError(
            "No 'origin' remote is configured. Add one pointing at your GitHub repository, e.g.\n"
            f"    git remote add origin https://github.com/{GITHUB_REPO or 'owner/repo'}.git"
        )
    url = result.stdout.strip()

    for pattern in _SSH_PATTERNS:
        if pattern.match(url):
            raise UpdateError(
                f"The 'origin' remote uses SSH ({redact(url)}), which this updater cannot authenticate with. "
                f"Switch it to HTTPS:\n    git remote set-url origin https://github.com/{GITHUB_REPO}.git"
            )

    matched = None
    for pattern in _REMOTE_PATTERNS:
        m = pattern.match(url)
        if m:
            matched = m.group("repo")
            break
    if not matched:
        raise UpdateError(
            f"The 'origin' remote ({redact(url)}) is not a recognised GitHub HTTPS URL. "
            f"Expected https://github.com/{GITHUB_REPO}.git"
        )
    if GITHUB_REPO and matched.lower() != GITHUB_REPO.lower():
        raise UpdateError(
            f"The 'origin' remote points at '{matched}' but GITHUB_REPO is '{GITHUB_REPO}'. "
            "Fix whichever is wrong — the updater will not change your remote for you."
        )

    # Anonymous reachability check. A public repository needs no credentials;
    # if this fails the repo is private, renamed, or the network is blocked.
    reachable = git("ls-remote", "--heads", "origin", timeout=60)
    if reachable.returncode != 0:
        raise UpdateError(
            "Could not reach the GitHub remote anonymously. The repository must be public and the URL correct. "
            f"Git said: {redact(reachable.stderr.strip())[:300]}"
        )
    return url


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def _updater_running() -> bool:
    from app.services import maintenance

    state = maintenance.read_state()
    if not state or str(state.get("status")) not in maintenance.IN_PROGRESS_STATUSES:
        return False
    return maintenance._pid_alive(int(state.get("updater_pid") or 0))


LOCK_MAX_AGE_SECONDS = 2 * 60 * 60  # an update that has run this long is not alive


def _clear_stale_lock() -> None:
    """A lock whose owner is gone must not block updates forever — but a live
    one must never be cleared.

    Staleness is judged from the pid recorded IN THE LOCK, not from the update
    state file: the lock is taken before the updater process exists and before
    any state is written, so a brand-new valid lock would otherwise look
    abandoned and let a second update straight through.
    """
    if not UPDATE_LOCK_FILE.exists():
        return

    from app.services import maintenance

    try:
        info = json.loads(UPDATE_LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}

    owner_pid = int(info.get("pid") or 0)
    if owner_pid and maintenance._pid_alive(owner_pid):
        return  # genuinely held

    # No readable pid: fall back to age so a corrupt lock still expires.
    if not owner_pid:
        try:
            started = datetime.fromisoformat(str(info.get("started_at", "")))
            if (datetime.now() - started).total_seconds() < LOCK_MAX_AGE_SECONDS:
                return
        except (TypeError, ValueError):
            pass

    try:
        UPDATE_LOCK_FILE.unlink()
        logger.warning("Cleared a stale update lock (owner pid %s is not running).", owner_pid or "unknown")
    except OSError:
        pass


def acquire_lock(pid: int) -> None:
    """Atomic create — two simultaneous install requests cannot both win."""
    UPDATER_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _clear_stale_lock()
    try:
        fd = os.open(str(UPDATE_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise UpdateError("An update is already running on this machine.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "started_at": datetime.now().isoformat(timespec="seconds")}, f)


def release_lock() -> None:
    try:
        UPDATE_LOCK_FILE.unlink()
    except OSError:
        pass


def _free_bytes(path: Path) -> int:
    usage = shutil.disk_usage(str(path))
    return usage.free


def preflight(target_tag: str | None = None, allow_downgrade: bool = False) -> dict:
    """Every check that must pass before anything is touched. Raises
    UpdateError with an admin-readable reason on the first failure."""
    checks: list[str] = []

    if _updater_running():
        raise UpdateError("An update is already running on this machine.")
    _clear_stale_lock()
    if UPDATE_LOCK_FILE.exists():
        raise UpdateError("An update lock is held. Wait for it to finish, or restart the backend if it is stuck.")
    checks.append("no update already running")

    if not is_configured():
        raise UpdateError("Updates are not configured — set GITHUB_REPO in .env (e.g. owner/Reconize).")
    checks.append("github repo configured")

    if shutil.which("git") is None:
        raise UpdateError("git is not installed or not on PATH; the updater needs it.")
    checks.append("git available")

    verify_remote()
    checks.append("origin remote verified and authenticated")

    info = check_for_update(force=True)
    if info.get("error"):
        raise UpdateError(info["error"])
    latest = info.get("latest_version")
    if not latest and not target_tag:
        raise UpdateError("There is no published GitHub Release to update to.")
    tag = target_tag or f"v{latest}"
    target_version = tag.lstrip("vV")
    checks.append(f"target release {tag} found")

    # Skipped for a deliberate rollback, which is by definition a downgrade.
    if not allow_downgrade and not is_newer(target_version, APP_VERSION):
        raise UpdateError(f"Already up to date (installed {APP_VERSION}, latest {latest}).")
    checks.append("target is newer than installed" if not allow_downgrade else "downgrade explicitly requested")

    sha = resolve_tag_sha(tag)
    checks.append(f"target resolves to {sha[:12]}")

    status = git("status", "--porcelain", timeout=60)
    if status.returncode != 0:
        raise UpdateError(redact(f"git status failed: {status.stderr.strip()}"))
    dirty = [line for line in status.stdout.splitlines() if line.strip() and not line.startswith("??")]
    if dirty:
        raise UpdateError(
            "There are uncommitted changes to tracked files in the application folder. "
            "The updater will not discard them. Commit or revert them first:\n  "
            + "\n  ".join(redact(line) for line in dirty[:10])
        )
    checks.append("working tree clean")

    with Session(engine) as session:
        busy = session.exec(
            select(PhotoBatch).where(PhotoBatch.status.in_(("pending", "processing")))  # type: ignore[attr-defined]
        ).first()
    if busy:
        raise UpdateError(
            f"Photo batch '{busy.label}' is still {busy.status}. Wait for it to finish before updating."
        )
    checks.append("no photo batch in progress")

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        probe = BACKUP_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise UpdateError(f"Backup directory is not writable ({BACKUP_DIR}): {e}")
    checks.append("backup directory writable")

    free = _free_bytes(PROJECT_ROOT)
    if free < MIN_FREE_BYTES:
        raise UpdateError(
            f"Not enough free disk space: {free / 1e9:.1f} GB available, "
            f"{MIN_FREE_BYTES / 1e9:.1f} GB required."
        )
    checks.append(f"disk space ok ({free / 1e9:.1f} GB free)")

    if not DATABASE_PATH.exists():
        raise UpdateError(f"Database not found at {DATABASE_PATH}.")
    checks.append("database present")

    return {"ok": True, "target_tag": tag, "target_sha": sha, "target_version": target_version, "checks": checks}


# --------------------------------------------------------------------------
# Initiation
# --------------------------------------------------------------------------

def start_update(target_tag: str | None = None, allow_downgrade: bool = False) -> dict:
    """Runs preflight, then hands the work to a detached updater process.
    Returns as soon as that process is launched — the backend must not be
    holding anything open when the updater stops it."""
    result = preflight(target_tag, allow_downgrade=allow_downgrade)
    tag, sha, version = result["target_tag"], result["target_sha"], result["target_version"]

    UPDATER_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    # Claim the lock HERE, before anything is spawned. Preflight only observed
    # that the lock was free; two requests arriving together would both pass
    # that check, and only this atomic create decides which one proceeds. The
    # updater takes ownership of the same file once it has its own pid.
    acquire_lock(os.getpid())

    # Run the updater from OUTSIDE the repository: `git checkout` rewrites
    # files in the repo, so an updater executing from tools/updater/ could be
    # replaced underneath itself mid-run.
    try:
        return _spawn_updater(tag, sha, version)
    except Exception:
        release_lock()  # never leave a lock behind for a launch that never happened
        raise


def _spawn_updater(tag: str, sha: str, version: str) -> dict:
    source = PROJECT_ROOT / "tools" / "updater" / "updater.py"
    if not source.exists():
        raise UpdateError(f"Updater source is missing at {source}.")
    runtime_copy = UPDATER_RUNTIME_DIR / "updater.py"
    shutil.copy2(source, runtime_copy)

    previous_sha = git("rev-parse", "HEAD", timeout=30).stdout.strip()

    payload = {
        "repo_root": str(PROJECT_ROOT),
        "python": sys.executable,
        "target_tag": tag,
        "target_sha": sha,
        "target_version": version,
        "from_version": APP_VERSION,
        "previous_sha": previous_sha,
        "database_path": str(DATABASE_PATH),
        "backup_dir": str(BACKUP_DIR),
        "runtime_dir": str(UPDATER_RUNTIME_DIR),
        "github_repo": GITHUB_REPO,
    }
    config_file = UPDATER_RUNTIME_DIR / "update_config.json"
    config_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # The updater needs no credentials: the repository is public and its git
    # calls run anonymously.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    proc = subprocess.Popen(
        [sys.executable, str(runtime_copy), str(config_file)],
        cwd=str(UPDATER_RUNTIME_DIR),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )

    from app.services import maintenance

    maintenance.invalidate_cache()
    logger.info("Update to %s started (updater pid %s)", tag, proc.pid)
    return {"started": True, "target_tag": tag, "target_version": version, "updater_pid": proc.pid}


def progress() -> dict:
    """Live update state, read from the runtime state file rather than the
    database — a rollback can restore a database snapshot from before the
    update began, which would erase any progress written there."""
    from app.services import maintenance

    state = maintenance.read_state()
    if not state:
        return {"status": "idle", "stage": "", "log_path": ""}
    state.pop("token", None)  # defensive: never echo anything secret-shaped
    if state.get("error"):
        state["error"] = redact(str(state["error"]))
    return state
