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
(buffalo_l model, ONNX Runtime, CPU). One reference photo per participant is
enough — there is no per-person training step. Recognition compares embeddings
with cosine similarity against a configurable threshold (Settings page).

## Requirements

Current Main features include GPU/CPU recognition, HEIC enrollment, Activities,
PDPA, Event Photos, Drive integration, multi-camera CCTV, Local Agent stations,
recording, live feed and reports. No participant data, photos, recordings,
model cache or credentials are included in Git.

- Windows 10/11, Python **3.13 x64**, Node **20.19+** or **22.12+** (tested: Node 25.2.1)
- Microsoft Visual C++ Runtime, current Chrome or Edge
- ~500MB free disk for the InsightFace model (downloaded automatically on first run)

## Installation

```bash
# Windows backend: NVIDIA GPU path, or use -Mode cpu
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -Mode gpu

# Frontend
cd ../frontend
npm ci
```

Copy `.env.example` to `.env`. Before real use set a strong `ADMIN_PASSWORD`
and `JWT_SECRET`; do this before first backend start because initial admin is
created once. Paths in `.env` are resolved from repository root; launcher
scripts start backend from `backend/`, so omit path overrides unless needed.

GPU setup needs compatible NVIDIA hardware/driver. Setup installs exact CUDA
runtime Python wheels and proves an actual InsightFace session uses CUDA; it
does not install drivers. CPU mode keeps identical recognition quality but is
slower. `buffalo_l` downloads on first model use into `~/.insightface/models/`;
internet, ~500 MB free space and writable cache are required.

## Running the Application

Two processes, in two terminals:

```bash
# Terminal 1 — backend API (http://127.0.0.1:8000)
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# Terminal 2 — frontend dev server (http://localhost:5173)
cd frontend
npm run dev
```

Open **http://localhost:5173** in Chrome or Edge.

No demo face photos are distributed. Add own test participant images.

### Single-process alternative

Run `npm run build` in `frontend/`, then start only the backend — it serves
the built frontend from `frontend/dist/` automatically at `http://127.0.0.1:8000/`.

## Demo Login

```
Username: admin
Password: admin123
```

Change these via `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env` before first
run (the account is created once on startup if it doesn't exist).

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
