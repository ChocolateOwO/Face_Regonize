# Reconize ↔ robot integration API

**Status: sections up to `call_as` were applied to Main on 2026-10-10. Live
presence (below) was added on 2026-10-12; its node-identity checks on
2026-10-13.** Not committed, published or
released. Measured numbers are from this PC (RTX 4050 Laptop, CUDA) with
synthetic frames in the test environment. They are measurements, not
guarantees.

Base URL: `http://127.0.0.1:8000`. All existing paths and fields keep working.
Everything below is either new or an additive field.

## What changed for the robot team (2026-10-12)

- **New:** `GET /api/node/{node_id}/present?seconds=10` — who is in front of one camera right now, unknown faces included. Machine account allowed.
- **New, opt-in:** `…/events-feed?token=…&include=presence` also delivers `{"type":"presence",…}` messages. Without `include=presence` the feed is exactly as before.
- **Unchanged:** every existing path, parameter and field; the matched event's fields; `/api/look` (still stateless — it never feeds presence).
- **Measured:** `/api/look` stays fast while a tagged-video render runs (numbers below).
- **Still not available:** `want=persons` and a check-in option on `/api/look` (see "Deferred").
- **2026-10-20 (Dummy, not yet in Main):** separate live/background inference with fair turns per `source`, a real `Retry-After`, a detailed `/api/health`, `title`/`language`, and `detected_at` with `+07:00`. See the sections below.
- **Not done yet:** the robot's machine account on the real system. It is created when the hub is ready to receive the password.

## Authentication: machine account

An admin creates the robot's account on the Reconize PC. Run this from `backend/`:

```
python scripts/manage_machine_user.py create --username robot-hub
```

The password is printed once; reset it with `reset`. Log in as before with
`POST /api/auth/login` `{username, password}` → `access_token`. The token lasts
480 minutes, so renew it before then.

A machine account can call only these:

| Method | Path | Notes |
|---|---|---|
| POST | `/api/auth/login` | as before |
| GET | `/api/auth/me` | `role` is `"machine"` |
| GET | `/api/health` | no token needed |
| POST | `/api/look` | new, see below |
| POST | `/api/recognition/upload?persist=false` | `persist=true`, or the parameter omitted, returns **403** for a machine |
| GET | `/api/history` | read only |
| GET | `/api/reports/dashboard` | |
| GET | `/api/node/status` | |
| GET | `/api/node/{node_id}/present` | new, see "Live presence" |
| GET | `/api/settings` | for a machine, returns only `camera_label`, `app_name`, `event_name` |
| WS | `/api/node/events-feed?token=…` | add `&include=presence` for presence messages |

Every other route returns **403** for a machine token. An admin account still
works everywhere, as before.

## Recognition without saving: `persist=false`

`POST /api/recognition/upload?persist=false` and
`POST /api/node/recognize-central?persist=false` do the following:

- Detect and identify, then return the usual response with `"persisted": false`.
- Matched entries have `checkin_status: "not_recorded"`. That means "seen", not "checked in".
- Nothing is saved: no image, history row or attendance, and no events-feed message.

`activity_id` is ignored. With `persist` omitted, both routes behave exactly as
before. Discover the parameter in `/openapi.json`.

## `POST /api/look` (stateless, one JPEG)

Multipart form fields:
- `photo`: required JPEG, at most 2 MiB and at most 2048 px per side / 4.2 MP.
- `source`: optional camera label. **Send it on every request** (one label per
  camera): each source gets a fair turn on the live model (round-robin), so a
  camera that sends often cannot starve a quiet one. Without it, all requests
  from one account share one turn. Logged, never stored or shown in health.
- `want`: `faces` (default). `persons` is reserved and returns 422.

```json
{"image":{"w":640,"h":480},"took_ms":72.6,"kept":false,
 "faces":[{"box":[0.31,0.22,0.52,0.61],"face_score":0.83,"status":"matched",
           "person_id":"…","participant_id":"9002","first_name":"Ann","name":"Ann Alpha",
           "call_as":"พี่แอน","confidence":0.94},
          {"box":[0.6,0.3,0.7,0.45],"face_score":0.66,"status":"unknown"}]}
```

- `box` is `[x1, y1, x2, y2]` normalized to 0–1.
- Faces are sorted by box area, largest (usually the closest) first. No faces gives `faces: []` with status 200.
- `call_as` is `null` when no form of address is set. An unknown face has no identity fields. Gender and age are never estimated.
- Matched faces also carry `title` and `language` (both `null` unless an admin set them; see below). Additive fields.
- Nothing is saved, published or remembered.

| Status | Meaning |
|---|---|
| 400 | Not a readable or complete JPEG |
| 401 | Bad or expired token |
| 403 | Account not allowed |
| 413 | Too many bytes, or too many pixels |
| 422 | `want=persons` |
| 429 | Busy: **drop this frame** and send the next one. `Retry-After` is the estimated wait in whole seconds (at least 1); `X-Retry-After-Ms` is the estimate itself in milliseconds. A frame waits at most 0.5 s for its turn; one camera may have one frame waiting besides the one being answered |
| 503 | Model not loaded, model failure, or app update in progress |

### Live and background inference (2026-10-20)

Live callers — `/api/look`, the kiosk page and camera nodes — use a **live
lane**; Event Photo, video, import and enrollment use a **background lane**.

- **Dual mode** (GPU with enough free memory at startup): the live lane has
  its own copy of the model. A live frame never waits behind an Event Photo
  scan or a video; the two still share the one GPU, so heavy background work
  can slow live answers somewhat.
- **Shared mode** (CPU only, not enough GPU memory, or the second copy failed
  to load): one model. Background work steps aside for a waiting live frame
  before each of its calls (at most 0.3 s each, so it still progresses), and
  Event Photo now holds the model for one tile at a time instead of a whole
  photo.
- Live frames take turns **per source**, round-robin.
- Both lanes match against the same participant list, so a new, changed or
  deleted participant applies to every caller at once.

`GET /api/health` (no token) says which mode is active and what is happening:

```json
{"status":"ok","model_ready":true,"inference_mode":"dual","device":"cuda",
 "busy_with":"look","live_queue":0,"background_queue":0,"look_avg_wait_ms":3.1,
 "inference":{"mode_reason":"separate live session on CUDAExecutionProvider",
   "live_busy_with":"look","background_busy_with":"event_photo",
   "live":{"queue":0,"granted":812,"expired":0,"rejected_full":0,"rejected_source":0,...},
   "look":{"requests_ok":800,"requests_busy":0,"avg_wait_ms":3.1,"p50_ms":41.0,"p95_ms":88.2,
           "p99_ms":120.4,"longest_busy_streak_s":0.0,...},...}}
```

- `busy_with`: what is using the model a live frame needs right now (`look`,
  `kiosk`, `camera_node`; in shared mode also `event_photo`, `video`,
  `import`, `enrollment`, ...), or `null`.
- `live_queue` / `background_queue`: requests waiting for each lane.
- `look_avg_wait_ms`: mean wait of recent `/api/look` calls (last 512), `null` before the first.
- Only workload names and counters; never camera labels, people or images.
- The server can be forced to one mode with the environment variable
  `RECONIZE_INFERENCE_MODE=shared` (or `dual`, which also allows two CPU
  copies). Default `auto`.

The GPU is shared with the kiosk, CCTV, Event Photo and video jobs. Measured
here (2 cameras × 4 req/s, 640×480): idle p50 77 ms / p95 172 ms with no 429s.
**While a large Event Photo batch runs, service is degraded:** p95 475 ms and
about 4% of frames refused with 429. Design the client to drop and move on.

While a tagged-video render runs (1080p source, measured 2026-10-12, same two
cameras × 4 req/s, 60 s each):

| Background | Frame | p50 | p95 | 429 | Longest gap between answers | Render speed |
|---|---|---|---|---|---|---|
| none | 640×480 | 73 ms | 166 ms | 0.3% (1 of 344) | 1.0 s | — |
| render, Plain | 640×480 | 72 ms | 146 ms | 0% | 0.43 s | 22.8 → 20.8 fps |
| render, Plain | 320×240 | 53 ms | 121 ms | 0% | 0.59 s | 20.6 fps |
| render, Tiled | 640×480 | 55 ms | 128 ms | 0% | 0.44 s | 8.2 → 6.6 fps |
| render, Tiled | 320×240 | 50 ms | 117 ms | 0% | 0.45 s | 6.9 fps |

A render gives way to a waiting look before each of its own inference calls,
so live requests are not starved; the render slows by about 9% (Plain) to 20%
(Tiled) instead. No 5xx in any run.

## Events feed: one event per match

`/api/node/recognize-central` used to publish every match twice: once with its
`node_id`, then again as `node_id:"kiosk"`. It now publishes **once**, with the
real `node_id`. The `"kiosk"` node_id now means only the kiosk page.

The six existing fields are unchanged: `node_id`, `participant_id`, `name`,
`activity_id`, `checkin`, `at`. Each message gains an additive `call_as`, which
may be `null`.

## Live presence: who is at one camera right now

```
GET /api/node/{node_id}/present?seconds=10
```

`node_id` is the same id the events feed sends (one per camera). `seconds` is
the look-back window, 1–30 (out-of-range values are clamped; default 10).
Admin or machine token.

```json
{"node_id":"CAM-02","seconds":10,"as_of":"2026-10-12T13:22:19.74",
 "observed_at":"2026-10-12T13:22:19.13","image":{"w":1920,"h":1080},
 "faces_now":3,"unknown_now":2,
 "people":[{"participant_id":"9003","name":"ปิยะ ทองคำ","call_as":null,
            "last_seen":"2026-10-12T13:22:19.13",
            "box":[0.0521,0.0926,0.2083,0.463],"confidence":0.66}]}
```

- `faces_now` and `unknown_now` describe the newest observation in the window. `unknown_now` counts faces nobody matched; they have no identity and nothing about them is saved.
- `people` lists everyone matched inside the window, **largest face first** (usually the closest person). `box` is `[x1, y1, x2, y2]`, 0–1, from that person's most recent sighting; `image` is the frame size it refers to. `box` can be `null`.
- `observed_at` is when the camera last reported. It is `null`, with empty lists and status 200, when the camera has not reported inside the window, when the node id is not known, and after a backend restart.
- Presence lives in memory only, for at most 30 s. It is never rebuilt from history: an old recognition proves nothing about now.
- Gender, age and title are never estimated or returned.

| Status | Meaning |
|---|---|
| 200 | Always, for an allowed token — including "nobody there" |
| 401 | Bad or expired token |
| 403 | Account not allowed |
| 422 | `seconds` is not a number |

**Where presence comes from.** A camera reports while it is scanning:

- CCTV stations (Local Agent on the station PC): every scan, about 2 per second per camera, matched and unknown faces with boxes. Requires the station's agent and page from this version.
- Phone/tablet nodes (Central does the inference): every recorded scan.
- The kiosk page and `/api/look` do **not** report presence. `?persist=false` scans do not either.

So `/present` answers for cameras that are running Reconize's own CCTV or
mobile node page. For an image the hub captured itself, use `/api/look`.

**How far a `node_id` can be trusted.** Reconize has no per-camera
credential: stations sign in with a user account, and the camera's name is
part of what they send. Presence therefore checks what it can:

- A station report is accepted only for a node that registered as a
  Local-Agent camera and whose station page is online (heartbeat in the last
  10 s). Otherwise it is refused (404 / 409) and nothing is recorded.
- **One reporter per camera.** While one station tab is reporting for a
  camera, reports for that camera from any other tab, computer or account are
  refused (409) until the first has been silent for 10 s. Two stations that
  were given the same name therefore cannot mix their faces, and nothing can
  be written into a camera that is live.
- A scan sent to Central updates presence only for a node registered as a
  Central-inference node, so it cannot overwrite a Local-Agent camera.
- The robot's machine account cannot report presence at all (403); it can
  only read it.

What is still trusted: **anyone holding a valid staff login** can report for a
camera that nobody is currently reporting for (by also sending its
heartbeat), and can send a photo to Central under any Central-inference node
name. That is the same trust the attendance routes already place in station
logins. Before relying on presence in a public deployment: keep staff
passwords private and changed from the defaults, give every station a unique
node name, and treat `/present` as "what the station at that name reports",
not as proof of where a person is.

**Presence messages on the feed (opt-in).** Connect with
`/api/node/events-feed?token=…&include=presence` to also receive:

```json
{"type":"presence","node_id":"CAM-02","status":"unknown","faces":3,"unknown":2,"at":"2026-10-12T13:22:19.13"}
```

- Sent when the number of unknown faces at a node changes, and at most every 2 s while unknown faces remain. `status` is `"unknown"`, or `"clear"` once when they are gone.
- A presence message always has `type`. Matched events never do — that is how to tell them apart. Matched events are unchanged and still arrive on the same socket.
- A client that does not ask for presence never receives it.

## History: provenance and incremental cursor

`GET /api/history` keeps all of its old params, fields and paging. Each item adds:

- `seq`: write order (integer). It is `null` for rows written before this change.
- `node_id`: `"kiosk"` for the kiosk, otherwise the CCTV or mobile node. It is `null` when unknown.
- `source`: `kiosk`, `node_event` (CCTV local agent), `node_central` (mobile/central), or `unknown` (older rows).
- `call_as`

The response adds `latest_seq`.

To read only new history, start with `after_seq = latest_seq`, then repeat:

```
GET /api/history?after_seq=<n>&page_size=100   ->  items oldest-first, next_after_seq
```

The `id` value is random. Use `seq`, not `id`, for ordering.

## `call_as` (form of address)

An admin enters it on the participant page ("เรียกว่า / Call as"). The backend
route is `PUT /api/people/{id}/call-as` with `{call_as}`, which is admin only.

- At most 40 characters. An empty value clears it.
- It appears in `/api/look`, recognition results, the events feed, history, and `/api/people`.
- It is never inferred.

## `title` and `language` (2026-10-20)

Optional, admin-entered per participant on the participant page ("คำนำหน้า/ตำแหน่ง /
Title", "ภาษา / Language"); backend route `PUT /api/people/{id}/voice-profile`
with `{title, language}` (admin only; a field left out is unchanged, empty clears it).

- `title`: free text, at most 40 characters (e.g. `ดร.`, `อาจารย์`, `CEO`).
- `language`: a language tag such as `th`, `en`, `zh`, `en-US` (stored in canonical case).
- Both are `null` until an admin sets them. Never guessed from a name, a face or registration data.
- Returned in `/api/look` matched faces and `/api/people`.

## Timestamps with an offset (2026-10-20)

`detected_at` in `/api/history`, `/api/reports/dashboard` (recent detections)
and a participant's attendance history now carries the server's UTC offset,
e.g. `2026-10-19T10:00:00.123456+07:00`. The stored values are unchanged
(server local time); only the offset is added on output. `date_from` /
`date_to` on `/api/history` accept either form.

## Deferred

- **`want=persons`** on `/api/look`: reserved, returns 422. It needs a body/person
  detector, which the installed face model pack does not include, and an agreed
  response shape.
- **Check-in from `/api/look`**: not available. `/api/look` never records anything; use the existing recognition routes to check someone in.

## Tagged video

An admin feature of the Local Video Experiment page (not an API for the
robot): an optional copy of a processed video with faces boxed and named. See
`docs/tagged-video.md`. Its only effect on the robot is GPU sharing, measured
above.
