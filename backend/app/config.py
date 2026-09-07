from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


DATABASE_PATH = Path(_env("DATABASE_URL", str(PROJECT_ROOT / "database" / "app.db")))
STORAGE_PATH = Path(_env("STORAGE_PATH", str(PROJECT_ROOT / "storage")))
PEOPLE_DIR = STORAGE_PATH / "people"
EVENTS_DIR = STORAGE_PATH / "events"
THUMBNAILS_DIR = STORAGE_PATH / "thumbnails"
PHOTO_BATCHES_DIR = STORAGE_PATH / "photo_batches"  # isolated from EVENTS_DIR — kiosk uploads never mix with photographer batches
# Browser-recorded clips from distributed camera nodes (MediaRecorder output).
# Isolated from EVENTS_DIR (kiosk scan stills) and from the ffmpeg cameras'
# own recordings/ directory (that one lives outside STORAGE_PATH entirely) —
# three different recording mechanisms, three different homes.
NODE_RECORDINGS_DIR = STORAGE_PATH / "node_recordings"

for d in (DATABASE_PATH.parent, PEOPLE_DIR, EVENTS_DIR, THUMBNAILS_DIR, PHOTO_BATCHES_DIR, NODE_RECORDINGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Service-account credentials for reading a photographer's shared Google
# Drive folder (distinct from the existing public-file-only Drive download,
# which needs no credentials at all).
GOOGLE_SERVICE_ACCOUNT_KEY_PATH = _env("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", "")

# Admin OAuth client for the Event Photo Processing WRITE path (creating
# folders/uploading files under the admin's own Google account, which has
# real Drive storage quota — the service account above does not).
GOOGLE_DRIVE_CLIENT_ID = _env("GOOGLE_DRIVE_CLIENT_ID", "")
GOOGLE_DRIVE_CLIENT_SECRET = _env("GOOGLE_DRIVE_CLIENT_SECRET", "")
BACKEND_BASE_URL = _env("BACKEND_BASE_URL", "http://localhost:8000")
FRONTEND_URL = _env("FRONTEND_URL", "http://localhost:5173")
OAUTH_REDIRECT_URI = f"{BACKEND_BASE_URL}/api/photo-batches/drive-oauth/callback"

# How often the retention background loop checks for expired photo batches.
RETENTION_CHECK_INTERVAL_SECONDS = int(_env("RETENTION_CHECK_INTERVAL_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Application updates (GitHub releases)
# ---------------------------------------------------------------------------

# The running version. Single source of truth; a release is tagged "v" + this.
VERSION_FILE = PROJECT_ROOT / "VERSION"


def _read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


APP_VERSION = _read_version()

# "owner/repo" of the PUBLIC repository this installation updates from.
# Empty means updates are simply not configured on this machine.
GITHUB_REPO = _env("GITHUB_REPO", "")

# OPTIONAL. The repository is public, so update detection and git fetch both
# work anonymously and no token is needed. A token is only ever used to raise
# the GitHub API rate limit (60 requests/hour per IP unauthenticated, which is
# far more than an hourly check needs) on machines that share an outbound IP
# with many others. It is never used for git operations, and its absence must
# never cause an error. Still redacted from every log and API response.
GITHUB_TOKEN = _env("GITHUB_TOKEN", "")
UPDATE_CHECK_INTERVAL_MINUTES = int(_env("UPDATE_CHECK_INTERVAL_MINUTES", "60"))

# Verified database snapshots taken immediately before an update.
BACKUP_DIR = PROJECT_ROOT / "backups"

# Runtime update state lives OUTSIDE the repository, so a `git checkout` during
# an update can never overwrite or delete the very files that record what the
# update is doing (and how to recover it). The tracked updater source is in
# tools/updater/; this is only where the running copy and its state go.
_local_appdata = os.environ.get("LOCALAPPDATA")
RUNTIME_DIR = Path(_local_appdata) / "Reconize" if _local_appdata else Path.home() / ".reconize"
UPDATER_RUNTIME_DIR = RUNTIME_DIR / "updater"
UPDATE_STATE_FILE = UPDATER_RUNTIME_DIR / "update_state.json"
UPDATE_LOG_FILE = UPDATER_RUNTIME_DIR / "update.log"
UPDATE_LOCK_FILE = UPDATER_RUNTIME_DIR / "update.lock"

JWT_SECRET = _env("JWT_SECRET", "dev-secret-change-me-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(_env("JWT_EXPIRE_MINUTES", "480"))

ADMIN_USERNAME = _env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "admin123")

DEFAULT_FACE_MATCH_THRESHOLD = float(_env("FACE_RECOGNITION_THRESHOLD", "0.45"))
DEFAULT_FACE_DETECTION_CONFIDENCE = float(_env("FACE_DETECTION_THRESHOLD", "0.5"))

MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "20"))
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_IMPORT_TYPES = {".csv", ".xlsx", ".xls"}
