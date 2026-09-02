# Event Photo Processing — v2.1 (Dual-Output Architecture)

**Version:** 2.1
**Date:** 2026-08-25
**Supersedes:** v1.1 (Drive-only, regressed) → see §8 Version History
**Status:** Implemented and verified on a live local instance.

**Purpose:** Document the current Event Photo Processing implementation after the Web Preview regression fix, for an independent engineer/AI reviewer.

**Method note:** Every claim below is tagged as one of:
- **MEASURED** — captured from a live, running instance during this work.
- **CODE** — verified by direct code reading, not execution.
- **NOT TESTED** — a plausible consequence of the code that was not actually triggered, called out explicitly.

---

## 1. What This Feature Does

Processes a photographer's folder of event photos: detects every face, identifies which registered participants appear in each photo, sorts photos per participant, produces a PDPA-compliant "Media" copy with non-consented faces blurred, and mirrors the result to Google Drive.

**Core constraint (unchanged since v1.0):** this feature reuses the existing face recognition stack *exactly as-is*. No separate model, embedding method, index, matching algorithm, or threshold. **CODE** — verified in [photo_processing_service.py](../backend/app/services/photo_processing_service.py):

| Reused component | Source | Same as kiosk? |
|---|---|---|
| Detection + embedding | `app.face_recognition.engine.detect_faces()` | Yes, identical call |
| Matching | `app.face_recognition.index.recognition_index.match_batch()` | Yes, identical call |
| Threshold | `app.services.settings_cache.get_threshold()` | Yes, same cached value (0.45 default) |
| Consent data | `ConsentRecord` table, "latest row wins" | Same rule, independent local helper |

---

## 2. Architecture — Two Independent Outputs

The defining change in v2.0. Local storage and Google Drive are **separate, sequential, independently-failing stages**:

```
Photographer's Google Drive folder
  → Download (service account, read-only)
  → Decode → detect_faces() → match_batch() → consent lookup → blur
  │
  ├── STAGE 1: LOCAL  (always runs, primary)
  │     storage/photo_batches/{id}/{ORIGINAL,SORTED,AMBIENCE,REVIEW,MEDIA}
  │     → backs the in-app Web Preview via /api/files/{path}
  │     → NEVER depends on Google Drive
  │
  └── STAGE 2: GOOGLE DRIVE  (best-effort, secondary)
        PROCESSED/{participant folders, AMBIENCE, REVIEW, MEDIA}
        → every upload VERIFIED before being reported as success
        → failure is recorded and surfaced, never propagated
```

**Failure isolation rule (CODE):** [`_drive_upload_photo()`](../backend/app/services/photo_processing_service.py) wraps all Drive work in `try/except Exception`. A Drive failure sets `drive_upload_status="failed"` + `drive_error`, increments `batch.drive_failed_photos`, and returns normally. The local result is already committed by that point and is never deleted.

### Why two Drive identities

**CODE + MEASURED.** Reads and writes deliberately use different credentials:

| Operation | Service | Identity | Reason |
|---|---|---|---|
| List/download source photos | [google_drive_folder_service.py](../backend/app/services/google_drive_folder_service.py) | Service account, `drive.readonly` | Photographer shares their folder with it (Viewer is enough); needs no user consent, so no verification |
| Create folders / upload / copy | [google_drive_oauth_service.py](../backend/app/services/google_drive_oauth_service.py) | Admin's own OAuth account, `drive.file` | **A service account has no Drive storage quota on a consumer account** |

### Why `drive.file`, and where output lands (v2.1)

`.../auth/drive` is a Google **restricted** scope: publishing an app with it to
Production requires full OAuth verification *plus* a paid annual third-party
security assessment (CASA). Staying in Testing mode instead caps access at 100
allowlisted accounts and expires the refresh token about every 7 days.

`drive.file` is **not** restricted — no verification, no assessment, no
allowlist, no 7-day token expiry. The trade-off is that the app may only touch
files and folders **it created itself**, so it cannot write into the
photographer's existing folder.

Consequently the output tree is created in the **connected admin's own Drive**:

```
Reconize — {source folder name} — {YYYY-MM-DD HHMM}/     ← created by this app
└── PROCESSED/
    ├── {participant folders}/
    ├── AMBIENCE/
    ├── REVIEW/
    └── MEDIA/
```

Not idempotent by name on purpose — re-running the same source folder produces
a second, separately-dated output folder instead of silently merging.

The batch is labelled with the photographer's real folder name via
`get_folder_name()` (service account, read-only); it falls back to the folder
id if that read fails, since the label is cosmetic.

Output is **private to the connected admin** by default. The spec only requires
the admin to open and download the results, and auto-sharing photos of
participants would work against the PDPA intent of this feature — so sharing is
left as a deliberate manual action in Drive.

**MEASURED (2026-08-24):** with the service account as writer, `files.create` for folders succeeded but every file upload produced no file. A read-only `files.list` on all four output folders returned `[]` while the folders themselves existed. This is why writes moved to admin OAuth.

---

## 3. Photo Classification

**CODE.** Each photo gets exactly one `classification`, decided in `run_photo_batch()`:

| Faces detected | Any unrecognized? | classification | Local destinations |
|---|---|---|---|
| 0 | — | `ambience` | `AMBIENCE/` + `MEDIA/` |
| ≥1 | No | `sorted` | `SORTED/{id}_{name}/` (one per matched person) + `MEDIA/` |
| ≥1 | Yes | `review` | `REVIEW/` + `SORTED/…` for any *recognized* people + `MEDIA/` |

Notes:
- A photo with several recognized participants is copied into **each** of their folders. **CODE**
- A photo with a known *and* an unknown face goes to `REVIEW` **and** to the known person's folder — it is not withheld from the participant. **CODE**
- `ORIGINAL/` always receives the untouched source bytes first, before any other step. **MEASURED** — see §6.

---

## 4. PDPA / Consent Behaviour

**CODE.** Consent is resolved *once*, at processing time, then frozen.

1. `_consent_status()` reads the latest `ConsentRecord` for the person → `"consented"` | `"declined"` | `"pending"`.
2. An unmatched face is recorded as `"no_match"`.
3. The result is written to `PhotoBatchFace.consent_status_at_processing` and **never re-read or updated afterwards**.

**Blur rule:** in `MEDIA/`, every face whose snapshot status is **not** `"consented"` is blurred — this includes `declined`, `pending`, and `no_match` (unrecognized). Consented faces stay visible. The photo is never blurred as a whole, and `ORIGINAL/` is never modified.

**Blur method:** `_blur_region()` — bbox expanded 25% horizontally / 35% vertically, then two passes of `cv2.GaussianBlur` with an odd kernel scaled to face size (min 31px). Two passes make simple sharpening ineffective.

**Consent is final:** if a participant later changes their consent, already-generated Media is **not** regenerated. A new batch processed afterwards uses the consent status current at *that* time. **CODE** — enforced structurally by nothing ever re-reading old `PhotoBatchFace` rows.

---

## 5. Google Drive Upload Verification

**CODE.** New in v2.0. Previously an upload was trusted if the API returned an id — which is exactly how the quota failure went unnoticed.

[`verify_file()`](../backend/app/services/google_drive_oauth_service.py) re-fetches every uploaded/copied file and asserts:

| Check | Failure meaning |
|---|---|
| `files().get(fileId)` resolves | The id was never a real file |
| `trashed` is false | File exists but was discarded |
| `expected_parent_id in parents` | Landed in the wrong folder |
| `size > 0` | Metadata created but no bytes stored (**the quota failure signature**) |

Both `upload_bytes()` and `copy_file()` call it before returning. An upload is only ever recorded as `"uploaded"` after all four checks pass.

**Bandwidth note (CODE):** the unmodified original is uploaded **once**, then placed into every other destination folder via server-side `files.copy` — the same bytes never leave this app more than once per photo.

---

## 6. Verification Performed

**MEASURED — 2026-08-24, local instance, Google Drive deliberately disconnected** (the exact regression scenario):

| Check | Result |
|---|---|
| Create batch with Drive disconnected | `200 OK` (v1.1 returned `400`) |
| Processing completes | `status: completed`, 1/1 photos, 0 failed |
| Recognition still works | 1 face detected, 1 matched → participant `0009` |
| Local dirs created | All 5: `ORIGINAL, SORTED, AMBIENCE, REVIEW, MEDIA` |
| Thai folder name written | `SORTED/0009_จิรชาติ ใคร้มา/` — correct |
| `GET /{id}/photos` | Returns 1 photo with `original_path` + `media_path` |
| Category filters | `sorted`=1, `ambience`=0, `review`=0 |
| Person filter | `?person_id=…` → 1 photo |
| Images serve over HTTP | ORIGINAL `200, 143789 B`; MEDIA `200, 247551 B` |
| **PDPA blur visually confirmed** | Face blurred in MEDIA, rest of photo untouched |
| **ORIGINAL unmodified** | Visually confirmed unblurred |
| Drive state reported honestly | `drive_error: "Google Drive is not connected — processed photos were saved locally only."` |

**MEASURED — regression sweep, unrelated features:**

`/api/people` (9), `/api/history` (24), `/api/uploads` (16), `/api/pdpa/status` (9), `/api/attendees` (9), `/api/reports/dashboard`, `/api/settings`, `/api/cleanup-logs` — all `200`. Live kiosk recognition: `faces_total: 1, faces_matched: 1, threshold_used: 0.45` — unchanged.

**NOT TESTED:** the Drive *success* path end-to-end (upload + verification against a real folder). Requires `GOOGLE_DRIVE_CLIENT_ID`/`SECRET` and a completed OAuth consent, which are not yet configured. The verification logic is implemented but has not executed against live Drive writes.

**NOT TESTED:** retention auto-deletion of local files under the v2.0 code path (the `shutil.rmtree` restore). Logic restored from the v1.0 implementation that was previously tested, but not re-exercised after this change.

---

## 7. Data Model

**CODE.** [models.py](../backend/app/models/models.py). Changes in v2.0 marked ⭐.

**`PhotoBatch`** — one processing run
`id`, `label`, `drive_folder_id` (source), ⭐`storage_dir`, `processed_folder_id`, `media_folder_id`, `ambience_folder_id`, `review_folder_id`, `status`, `current_stage`, counters (`total_photos`, `processed_photos`, `faces_detected`, `faces_recognized`, `faces_unknown`, `consented_faces`, `not_consented_faces`, `blurred_faces`, `recognized_photos`, `ambience_photos`, `review_photos`, `failed_photos`, ⭐`drive_failed_photos`), `last_error`, ⭐`drive_error`, `retention_days`, `retention_start_at`, `delete_at`

**`PhotoBatchPhoto`** — one photo
`id`, `batch_id`, `filename`, `drive_file_id` (source), ⭐`original_path`, ⭐`media_path`, `media_drive_file_id`, ⭐`drive_upload_status`, ⭐`drive_error`, `classification`, `faces_total`, `faces_matched`, `faces_unknown`

**`PhotoBatchFace`** — one detected face
`id`, `photo_id`, `person_id` (nullable), `confidence`, `bbox`, `consent_status_at_processing`, `blurred`

**`PhotoBatchParticipantFolder`** — per-participant folder + count
`id`, `batch_id`, `person_id`, `folder_id` (empty when Drive skipped), `photo_count`

**`CleanupLog`** — survives its batch; `batch_id` is a snapshot string, **not** a live FK

**Migration note:** this project uses `SQLModel.metadata.create_all()` with no migration framework — `create_all` does **not** add columns to existing tables. The v2.0 columns were added by explicit `ALTER TABLE` against `backend/database/app.db`. **MEASURED.**

⚠️ **Pre-existing issue, unrelated to this feature, not fixed:** `DATABASE_URL`/`STORAGE_PATH` default to *relative* paths, so the live DB is `backend/database/app.db` when started from `backend/` — not the project-root `database/app.db`. Both files currently exist. Flagged, deliberately left alone as out of scope.

---

## 8. Version History

| Version | Change | Outcome |
|---|---|---|
| 1.0 | Initial: local storage + web preview, Drive read-only for input | Working |
| 1.1 | Reworked to **Drive-only** output; removed `PHOTO_BATCHES_DIR`, `original_path`/`media_path`, `GET /{id}/photos`, and all local writes | **Regression** — web preview gone; Drive uploads silently failed (service-account quota) → zero output anywhere |
| 2.0 | Restored local output as primary; Drive as verified best-effort mirror; upload verification; Drive-failure reporting; Drive no longer required to process | Web preview restored, Drive failures now visible |
| **2.1** | **Current.** Write scope `drive` → `drive.file`; output tree moved from inside the photographer's folder to a new folder in the connected admin's own Drive; batch labelled with the real source folder name | Publishable to Production without paid verification; no 100-user Testing cap; no 7-day token expiry |

### v1.1 → v2.0 changed files

| File | Change |
|---|---|
| [config.py](../backend/app/config.py) | Restored `PHOTO_BATCHES_DIR` + mkdir |
| [models.py](../backend/app/models/models.py) | Restored `storage_dir`, `original_path`, `media_path`; added Drive-status fields |
| [photo_processing_service.py](../backend/app/services/photo_processing_service.py) | Two-stage pipeline; `_write_bytes()`; `_drive_upload_photo()`; `shutil.rmtree` restored in retention |
| [google_drive_oauth_service.py](../backend/app/services/google_drive_oauth_service.py) | Added `verify_file()`; `upload_bytes`/`copy_file` now verify |
| [photo_batches.py](../backend/app/api/photo_batches.py) | Restored `GET /{id}/photos`; removed Drive-connected gate; exposed Drive-failure fields |
| [PhotoBatchDetail.tsx](../frontend/src/pages/PhotoBatchDetail.tsx) | Restored 4-tab preview grid; kept Drive links; added failure banners |
| [PhotoBatches.tsx](../frontend/src/pages/PhotoBatches.tsx) | Re-enabled "+ New Batch" without Drive |

**Explicitly unchanged in v2.0:** face detection, embedding, recognition model, recognition index, matching algorithm, matching threshold, registration, check-in, consent logic, PDPA logic, participant import, admin authentication. No ZIP download exists anywhere in this feature.

---

## 9. Windows / Non-ASCII Path Hazard

**MEASURED, and still load-bearing.** `cv2.imwrite()` **silently fails** (returns `False`, raises nothing) when the path contains non-ASCII characters — this project's own path does (`e:\งาน\491\Reconize`).

Every image write in this feature therefore goes: `cv2.imencode()` → bytes → `Path.write_bytes()` via `_write_bytes()`. **Never call `cv2.imwrite()` on a real path in this repo.** Confirmed by grep that no other code does.

---

## 10. Known Limitations

1. **Drive success path unverified** — needs OAuth credentials configured (§6).
2. **Fully sequential processing** — one photo at a time, one Drive call at a time. For a large batch (1,000+ photos) this, not recognition, dominates runtime. **CODE**
3. **Frequent small SQLite commits** — ~6+ `session.commit()` per photo. Safe, not optimised. **NOT TESTED** at scale.
4. **Retention deletes local copies only** — the Drive `PROCESSED` folder is deliberately never auto-deleted; it's the durable deliverable. Removing it is a manual action in Drive. **CODE**
5. **Partial Drive mirror possible** — if Drive fails mid-batch, some photos are mirrored and some are not. Per-photo status is recorded; there is currently **no retry/resume action** to complete a partial mirror.
6. **Output is not auto-shared** (v2.1) — it lands in the connected admin's own Drive, private. Giving the photographer or anyone else access is a manual share in Drive. Deliberate: auto-sharing participant photos would conflict with this feature's PDPA purpose.
7. **Existing OAuth grants are invalidated by the v2.1 scope change** — tokens issued under the old `drive` scope were cleared; the admin must click Connect Google Drive once more. **MEASURED** — stale token rows removed from `setting`.
