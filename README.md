# Reconize — Local Event Management & Face Recognition

A local-first web app for registering event participants from a single reference
photo, then identifying them in event photos using on-device face recognition.
No participant images or embeddings ever leave your machine.

## Architecture

```
frontend/   React + TypeScript + Tailwind CSS (Vite)
backend/    FastAPI (Python) — REST API, auth, face recognition, bulk import
database/   SQLite database file
storage/    Uploaded/downloaded images (people/, events/, thumbnails/)
```

Face detection and embedding run locally via [InsightFace](https://github.com/deepinsight/insightface)
(buffalo_l model, ONNX Runtime, NVIDIA GPU or CPU). One reference photo per participant is
enough — there is no per-person training step. Recognition compares embeddings
with cosine similarity against a configurable threshold (Settings page).

## Requirements

Current Main features include GPU/CPU recognition, HEIC enrollment, Activities,
PDPA, Event Photos, Drive integration, multi-camera CCTV, Local Agent stations,
recording, live feed and reports. No participant data, photos, recordings,
model cache or credentials are included in Git.

| Needed | Version | Install (Windows) |
|---|---|---|
| Windows 10/11 x64 | | |
| Git | any recent | `winget install --id Git.Git -e` |
| Python | **3.13 x64** (with the `py` launcher) | `winget install --id Python.Python.3.13 -e` |
| Node.js | **20.19+ or 22.12+** (tested 25.2.1, see `.node-version`) | `winget install --id OpenJS.NodeJS -e` |
| Browser | current Chrome or Edge | |
| Internet | first setup and first start (packages, ~300 MB face model) | |
| Optional: NVIDIA GPU | a driver where `nvidia-smi` works | NVIDIA driver; CUDA libraries come from pip |

Disk: about 6 GB for the GPU setup (CUDA wheels), about 2 GB for CPU, plus your
photos. Recognition quality is identical on CPU; it is only slower.

## Installation (fresh PC)

```powershell
git clone https://github.com/ChocolateOwO/Face_Regonize.git Reconize
cd Reconize
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1            # NVIDIA GPU
#   or: powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -Mode cpu
```

The setup script checks the prerequisites (and prints the install command for
any that is missing), creates `backend\.venv` from the pinned
`backend\requirements.lock.txt`, checks that InsightFace really runs (CUDA in
GPU mode), creates `.env` with a **new random signing secret and admin
password** (printed once — write it down), and runs `npm ci`. It never
overwrites an existing `.env`; run it again any time, finished steps are kept.

`.env` (not in Git) is the only place for this PC's settings; see
`.env.example`. `python scripts\init_env.py --check` reports whether the
public defaults are still in use.

## Running the Application

Double-click `start.bat` (or `Start Reconize.vbs`): it starts the backend on
http://127.0.0.1:8000 and the page on **http://localhost:5173**, and refuses
with a clear message if setup has not been run. `start-network.bat` serves the
built app on one port for other PCs on the network.

The first start downloads the face model into `%USERPROFILE%\.insightface`
and creates an empty database in `backend\database\app.db` (`DATABASE_URL` in
`.env` is relative to `backend\`; run the `backend\scripts` tools from `backend\`).

No demo face photos are distributed. Add own test participant images.

### Single-process alternative

Run `npm run build` in `frontend/`, then start only the backend — it serves
the built frontend from `frontend/dist/` automatically at `http://127.0.0.1:8000/`.

## First Sign-in and Passwords

Sign in as `admin` with the password the setup script printed. To change it
later (the `.env` value is only used for the very first start):

```powershell
cd backend
.venv\Scripts\python.exe scripts\set_admin_password.py              # asks twice
.venv\Scripts\python.exe scripts\set_admin_password.py --generate   # prints a new one once
```

An install made before this setup script may still use the public defaults
(`admin` / `admin123` and the default `JWT_SECRET`): change both before real
use. Changing `JWT_SECRET` signs everybody out once.

## Connecting the Mice Hub

The Hub (robot) signs in with its own least-privilege **machine account**,
never the admin. On the Reconize PC:

```powershell
cd backend
.venv\Scripts\python.exe scripts\manage_machine_user.py create --username robot-hub   # prints the password once
```

On the Hub, put that login in `main_python\faces_login.json` (git-ignored):
`{"reconize": {"username": "robot-hub", "password": "<printed password>"}}`,
and make sure its `config\partners.json` entry `reconize.api` is
`http://127.0.0.1:8000`. Check: the Hub watcher's http://127.0.0.1:8769/health
shows `loggedIn: true` and `live: true`. Interfaces the Hub uses:
`docs/robot-integration-api.md`.

Optional Hub features, enabled in `.env` (both need a restart):

- `MICE_HUB_URL=http://127.0.0.1:8642` (this PC only; anything else is
  refused): CCTV stations list the Hub's cameras as "Hub · <name>". Pick one
  instead of the local device when the Hub already holds that camera - Reconize
  then reads the Hub's MJPEG stream (relayed by its own backend) and never
  opens the camera itself. Preview, scanning (only while a Detection Session is
  active), recording and the camera's identity work as for a local camera.
- `MICE_HUB_GPU_LEASE=1` (with `MICE_HUB_URL`): background face work (Event
  Photo, video, import, enrollment) asks the Hub's GPU scheduler before each
  model call, so Voice and live work go first. Live recognition never waits for
  the Hub, and no Hub answer means no waiting. Measured trade-off: speech bursts
  finish ~1/3 faster under Event Photo load, background throughput drops ~40%
  while speech is active (`docs/launch-readiness-2026-11-26.md`).
- `RECONIZE_HYBRID_EMBED=1`: live frames with 3+ faces are embedded in fixed
  batches of 8 on a second session (+~0.55 GB GPU memory). Faster for crowds,
  slightly slower for robot-like frames on an RTX 4050 - off by default
  (`docs/experiments/2026-11-26-hybrid-embedding.md`).

## Updates

Settings → Application Updates checks this repository's **published GitHub
Releases** (`GITHUB_REPO` in `.env`, filled in by setup). Installing needs an
admin to review and confirm the exact version. Before anything changes, the
update verifies the release's `release-manifest.json` checksum and commit,
refuses while work is running (Event Photo, imports, recordings, video jobs)
or the checkout has uncommitted changes, and backs up and verifies the
database and `.env`. After the restart it checks health and version; on
failure it restores the previous version and database automatically.
Database, storage, `.env` and the face model are never replaced. Publishing a
release: `docs/RELEASING.md`.

## How to Add a Person

**People → + Add Person** — upload one clear photo containing exactly one
face, fill in Participant ID / name / email, click Register. The photo must
have exactly one detectable face or registration is rejected with a specific
error (no face / multiple faces).

## How to Import Participants (CSV/Excel)

**Import Participants** → download the template if needed → upload your
`.csv`/`.xlsx`/`.xls` file. Expected columns (flexible naming is auto-detected,
e.g. "ID", "Participant ID", "participant_id" all map the same):

| Participant ID | First Name | Last Name | Email | Image URL |
|---|---|---|---|---|

You'll see a preview with each row marked **Ready / Warning / Error** before
anything is imported. Choose how to handle existing Participant IDs (Skip or
Update), then click Import — a progress bar tracks download → face detection
→ embedding → save per row, and a summary with a downloadable error report
appears at the end.

## Configuring Google Drive Images

Only **publicly shared** files are supported without extra setup:

1. In Google Drive, right-click the file → Share → General access → **"Anyone
   with the link"** → Viewer.
2. Paste any of these URL formats into the Image URL column:
   - `https://drive.google.com/file/d/FILE_ID/view`
   - `https://drive.google.com/open?id=FILE_ID`
   - `https://drive.google.com/uc?id=FILE_ID`

Private files are **not** bypassed — you'll get a clear "unable to access"
error telling you to fix sharing permissions. Supporting private files would
require the full Google OAuth consent flow (`GOOGLE_DRIVE_CLIENT_ID`/`SECRET`
in `.env` are scaffolded for this) — out of scope for this local MVP.

## How to Run Face Recognition

**Face Recognition** → upload an event photo → Run Recognition. All faces in
the photo are detected, matched against every registered participant, and
shown with bounding boxes (green = matched with name + confidence, red =
unknown). Every result is saved — matched faces update that participant's
attendance record.

## How to View Attendance

**Attendees** shows every registered participant with an **● In Event / ○ Not
Detected** status computed from real detection records, plus first/last
detected times. Click into a person (via People or Attendees) for their full
attendance history and source images.

## How to View History

- **Upload History** — every event photo submitted, with face/match counts and processing time.
- **Recognition History** — every individual face-match attempt, searchable/filterable by name, ID, status, or date.

## How to Export Data

**Reports** and **Recognition History** pages have Export buttons (CSV).
CSV/Excel export endpoints also exist for people, attendance, and recognition
history directly via the API (`/api/export/*?format=csv|xlsx`).

## Troubleshooting

**"No face detected" when registering** — use a clear, front-facing, well-lit
photo. Sunglasses, extreme angles, or very low resolution can prevent detection.

**"Multiple faces detected"** — crop the photo to just the one person, or pick
a different photo.

**Import row fails with "Unable to access Google Drive image"** — the file
isn't shared as "Anyone with the link". Fix sharing and re-import (failed rows
are listed individually; you don't need to redo the whole file).

**First recognition/registration is slow (~10-30s)** — the InsightFace model
(~280MB) downloads once on first use and is cached in
`~/.insightface/models/`. Subsequent calls are fast.

**CORS errors in the browser console** — make sure the backend is running on
port 8000 and the frontend on port 5173; `app/main.py` only allows those two
origins by default.

## CCTV, Local Agent and LAN

Central owns database, attendance, history and settings. A Windows Local Agent
only performs local inference against Central's synchronized index:

```powershell
cd backend
.venv\Scripts\python.exe node_agent.py --node-id CAM-01 --port 8101 --central http://CENTRAL-PC:8000
```

Use station's configured node ID, port and Central admin credentials. Allow
Python/port 8000 through Windows Firewall for private LAN use. `start-network.bat`
serves Central on LAN HTTP. Remote browser camera access may fail on HTTP:
`getUserMedia` requires HTTPS or localhost. Do not assume `recognize.local`;
this repository does not provide mDNS/HTTPS server helpers.

## Google Drive

Public single-file import works with shared links. Event Photo input folders
use a service account: enable Drive API, create own service-account JSON, set
`GOOGLE_SERVICE_ACCOUNT_KEY_PATH`, then share source folder with its email.
Event Photo output uses own OAuth Web client: configure consent screen/audience,
set `GOOGLE_DRIVE_CLIENT_ID` and `GOOGLE_DRIVE_CLIENT_SECRET`, restart, then
use Connect Google Drive. Default callback is
`http://localhost:8000/api/photo-batches/drive-oauth/callback`. Never commit
JSON keys, `.env`, OAuth secrets or local database; OAuth refresh token is local
database state. Private single-file import still uses public-download behavior.

## Privacy

Face images and embeddings are stored only in the local `storage/` and
`database/` folders on this machine. Recognition runs entirely on-device via
InsightFace — no facial data is sent to any external API. See the Settings
page for a summary shown to admins.

## What's Deferred / Not in This Version

Per the "local + stable + simple" priority for a first MVP:
- Google Drive **private-file** access via OAuth (public files work now)
- Real-time/webcam recognition (upload-based recognition works now)
- Multiple concurrent events (single active event; `Event Name/Date/Location` live in Settings)
- Multiple administrators / role-based access (single demo admin account)
