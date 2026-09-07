"""CSV/Excel bulk participant import pipeline.

No fixed template is required. The importer only needs to find two things in
whatever spreadsheet it's given — a NAME column and a PHOTO column — and
ignores every other column entirely. Everything else (auto-detecting the
header row, scoring candidate columns, generating sequential internal IDs)
exists to make that possible without asking the user anything unless the
system genuinely can't tell which column is which.

Flow: parse spreadsheet -> detect header row -> detect/score Name & Photo
columns -> (if ambiguous, the API layer asks the user to pick) -> build a
row-by-row preview (no network calls) -> on confirm, a background task walks
each row, downloads the reference image (Google Drive or direct URL),
detects exactly one face, generates an embedding, and creates the Person
record. Progress is written to the ImportJob/ImportRow tables so the
frontend can poll it. The face detection / embedding / recognition-index
pipeline itself is untouched — reused exactly as before.
"""
from __future__ import annotations

import io
import re
from datetime import datetime

import pandas as pd
import requests
from sqlmodel import Session, select

from app.database.db import engine
from app.face_recognition.engine import detect_faces
from app.face_recognition.enrollment import EnrollmentFaceError, select_primary_enrollment_face
from app.face_recognition.index import recognition_index
from app.models.models import ConsentRecord, ImportJob, ImportRow, Person
from app.services import storage_service
from app.services.google_drive_service import GoogleDriveError, download_public_file, is_google_drive_url
from app.services.index_sync import IndexResyncFailed, apply_index_change

NAME_HEADER_KEYWORDS = [
    "name", "full name", "fullname", "participant", "participant name",
    "attendee", "attendee name", "guest", "guest name", "student name",
    "member name", "registrant", "registrant name",
    "ชื่อ", "ชื่อ-นามสกุล", "ชื่อนามสกุล", "ผู้เข้าร่วม", "รายชื่อ", "ชื่อผู้เข้าร่วม", "ชื่อสมาชิก",
]
PHOTO_HEADER_KEYWORDS = [
    "photo", "image", "picture", "profile", "profile picture", "profile photo",
    "photo url", "image url", "picture url", "img", "avatar", "pic",
    "รูป", "รูปภาพ", "รูปผู้เข้าร่วม", "รูปถ่าย", "รูปประจำตัว",
]

CONSENT_HEADER_KEYWORDS = [
    "consent", "pdpa", "agree", "agreement", "permission", "authorise", "authorize",
    "ยินยอม", "ความยินยอม", "อนุญาต", "ข้อมูลส่วนบุคคล",
]

# Consent answers are matched as WHOLE normalised values, never as substrings.
#
# Substring matching is unsafe in both languages. In Thai the refusal is the
# acceptance with a negation glued on the front - "ไม่ให้ความยินยอม" literally
# CONTAINS "ให้ความยินยอม". In English "unknown" contains "no", so a substring
# test turned "unknown" into a recorded refusal - an uncertain answer becoming
# legal evidence of one. Exact lookup removes both failure modes at once, and
# anything not in these tables is reported as unrecognised rather than guessed.
#
# _normalize() collapses case, punctuation and spacing, so "Do not consent",
# "do-not-consent" and "DO NOT CONSENT" all arrive here as one key.
_CONSENT_YES = {
    "ยินยอม", "ให้ความยินยอม", "ยินยอมให้ใช้ข้อมูล", "ยินยอมให้ใช้ข้อมูลส่วนบุคคล",
    "อนุญาต", "ยอมรับ", "ตกลง",
    "consent", "consented", "i consent", "give consent", "agree", "agreed",
    "i agree", "accept", "accepted", "allow", "allowed", "permitted",
    "yes", "y", "true", "1",
}
_CONSENT_NO = {
    "ไม่ยินยอม", "ไม่ให้ความยินยอม", "ไม่อนุญาต", "ไม่ประสงค์", "ไม่ตกลง", "ปฏิเสธ",
    "do not consent", "does not consent", "not consent", "not consented",
    "no consent", "decline", "declined", "disagree", "disagreed", "reject",
    "rejected", "deny", "denied", "not allowed", "disallow", "refuse", "refused",
    "no", "n", "false", "0",
}

# Returned by parse_consent_value() when the cell HAD content but none of the
# tables recognised it. Distinct from None (blank) so the preview can warn the
# admin instead of quietly treating "maybe" like an unanswered question.
CONSENT_UNKNOWN = "unknown"

# Written onto the ImportRow so the sync result can say what happened to each
# person's PDPA answer. Exported as constants because get_import() counts them.
CONSENT_NOTE_RECORDED = "PDPA imported from the form"
CONSENT_NOTE_UPDATED = "PDPA updated from the form"
CONSENT_NOTE_UNKNOWN = "PDPA answer not understood - consent left unchanged"
CONSENT_NOTE_ADMIN_OVERRIDE = "PDPA protected by admin override - form answer not applied"
# Deliberately excludes the admin-override note: nothing changed, so it must
# not inflate the "PDPA Updated" count.
CONSENT_CHANGED_NOTES = (CONSENT_NOTE_RECORDED, CONSENT_NOTE_UPDATED)


def _consent_note(outcome: str, consent: str | None) -> str | None:
    """Result text for one row, or None when there is nothing worth saying."""
    if outcome == "recorded":
        return CONSENT_NOTE_RECORDED
    if outcome == "updated":
        return CONSENT_NOTE_UPDATED
    if outcome == "admin_override":
        return CONSENT_NOTE_ADMIN_OVERRIDE
    if consent == CONSENT_UNKNOWN:
        return CONSENT_NOTE_UNKNOWN
    return None


def detect_consent_column(df: pd.DataFrame) -> str | None:
    """Find the registration form column holding the PDPA answer.

    Header match only. Unlike name and photo there is deliberately no content
    scoring: a yes/no column looks exactly like any other yes/no column, and
    guessing wrong would attach a consent record to the answer to a completely
    different question. If no header matches, consent is simply not imported.
    """
    for column in df.columns:
        norm = _normalize(column)
        if any(_normalize(kw) in norm for kw in CONSENT_HEADER_KEYWORDS):
            return str(column)
    return None


def parse_consent_value(value) -> str | None:
    """Map one consent cell to "consented" / "declined" / CONSENT_UNKNOWN / None.

    Three outcomes that must not be confused with each other:

      None             the cell was blank. Silence is not an answer, so no
                       consent record is created and the person stays pending.
      CONSENT_UNKNOWN  the cell had text nobody can interpret ("maybe", "-",
                       "ไม่แน่ใจ"). Also creates no record, but the preview says
                       so, because this is a form that needs fixing rather than
                       a person who did not reply.
      consented        an explicit yes.
      declined         an explicit no.

    Matching is exact on the normalised value, never a substring - see the
    tables above for why.
    """
    raw = _cell_str(value).strip()
    if not raw:
        return None
    key = _normalize(raw)
    if not key:
        return CONSENT_UNKNOWN      # punctuation only, e.g. "-" or "N/A"
    if key in _CONSENT_NO:          # refusal checked first, as a safety habit
        return "declined"
    if key in _CONSENT_YES:
        return "consented"
    return CONSENT_UNKNOWN


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_IMAGE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|bmp|webp)(\?.*)?$", re.IGNORECASE)
_ONLY_DIGITS_PUNCT_RE = re.compile(r"^[\d\s+\-().]+$")

CONFIDENT_SCORE = 45.0
CONFIDENT_MARGIN = 20.0
PLAUSIBLE_SCORE = 12.0


def _cell_str(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none", "nat", ""):
        return ""
    return s


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9ก-๙]+", " ", str(text).lower()).strip()


def normalize_name(name: str) -> str:
    """Duplicate-matching key for participant names: collapses any run of
    whitespace to a single space and trims the ends, then casefolds for
    case-insensitive comparison. Deliberately does NOT strip punctuation or
    non-ASCII characters (unlike _normalize above, which is only for scoring
    column headers/content) — Thai characters and meaningful punctuation in
    a name are preserved exactly, only whitespace and case are normalized."""
    return re.sub(r"\s+", " ", str(name)).strip().casefold()


def detect_header_row(raw_rows: list[list], scan_limit: int = 20) -> int:
    """Finds the real header row, skipping title/banner rows above it (e.g.
    "Event Registration List" as row 0, with the actual "Name | Photo | ..."
    header on row 1). Heuristic: a title row is usually much narrower (often
    a single merged-looking cell) than the real table, so the header row is
    the first row whose filled-cell count is close to the widest row seen in
    the scanned window."""
    window = raw_rows[:scan_limit]
    if not window:
        return 0
    filled_counts = [sum(1 for c in row if _cell_str(c)) for row in window]
    max_width = max(filled_counts) if filled_counts else 0
    if max_width < 2:
        return 0
    for i, count in enumerate(filled_counts):
        if count >= max(2, max_width - 1):
            return i
    return 0


def parse_spreadsheet(file_bytes: bytes, filename: str) -> tuple[pd.DataFrame, int]:
    """Returns (dataframe, header_row_index). No column names or order are
    assumed — the header row is detected from the sheet itself, and any
    blank/duplicate header cells are given a safe placeholder name so the
    rest of the pipeline never has to handle missing column names."""
    ext = filename.lower().rsplit(".", 1)[-1]
    buf = io.BytesIO(file_bytes)
    if ext == "csv":
        raw = pd.read_csv(buf, dtype=str, keep_default_na=False, header=None)
    elif ext in ("xlsx", "xls"):
        raw = pd.read_excel(buf, dtype=str, header=None)
    else:
        raise ValueError(f"Unsupported spreadsheet type: .{ext}")

    raw_rows = raw.values.tolist()
    if not raw_rows:
        return pd.DataFrame(), 0
    header_idx = detect_header_row(raw_rows)

    header_cells = [_cell_str(c) for c in raw_rows[header_idx]]
    columns: list[str] = []
    seen: dict[str, int] = {}
    for i, h in enumerate(header_cells):
        name = h or f"Column {i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 0
        columns.append(name)

    data_rows = raw_rows[header_idx + 1:]
    df = pd.DataFrame(data_rows, columns=columns)
    df = df[df.apply(lambda r: any(_cell_str(v) for v in r), axis=1)].reset_index(drop=True)  # drop blank rows
    return df, header_idx


def _header_score(header: str, keywords: list[str]) -> float:
    norm = _normalize(header)
    if not norm:
        return 0.0
    if norm in keywords:
        return 100.0
    for kw in keywords:
        if kw in norm or norm in kw:
            return 70.0
    header_words = set(norm.split())
    for kw in keywords:
        if set(kw.split()) & header_words:
            return 40.0
    return 0.0


def _sample_values(df: pd.DataFrame, column: str, limit: int = 20) -> list[str]:
    values = []
    for v in df[column]:
        s = _cell_str(v)
        if s:
            values.append(s)
        if len(values) >= limit:
            break
    return values


def name_content_score(values: list[str]) -> float:
    if not values:
        return 0.0
    good = 0
    for v in values:
        if _URL_RE.match(v) or "@" in v or "drive.google.com" in v.lower():
            continue
        if _ONLY_DIGITS_PUNCT_RE.match(v):
            continue
        word_count = len(v.split())
        has_letters = any(ch.isalpha() for ch in v)
        if has_letters and 1 <= word_count <= 5 and 2 <= len(v) <= 60:
            good += 1
    return (good / len(values)) * 100.0


def photo_content_score(values: list[str]) -> float:
    if not values:
        return 0.0
    good = 0
    for v in values:
        if _URL_RE.match(v) or "drive.google.com" in v.lower() or _IMAGE_EXT_RE.search(v):
            good += 1
    return (good / len(values)) * 100.0


def score_columns(df: pd.DataFrame, header_keywords: list[str], content_scorer) -> list[dict]:
    results = []
    for col in df.columns:
        h_score = _header_score(col, header_keywords)
        values = _sample_values(df, col)
        c_score = content_scorer(values)
        total = h_score * 0.4 + c_score * 0.6
        results.append({"column": col, "score": round(total, 1), "samples": values[:3]})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def pick_column(scored: list[dict]) -> tuple[str | None, list[dict] | None]:
    """Returns (auto_picked_column, None) when confident, or
    (None, candidates) when the system should ask the user instead of
    guessing. "Confident" means one column clearly stands out — either the
    only one above the confidence bar, or comfortably ahead of the runner-up."""
    if not scored or scored[0]["score"] < PLAUSIBLE_SCORE:
        return None, scored[:5]  # nothing looked plausible — let the user pick from whatever exists
    top = scored[0]
    second = scored[1] if len(scored) > 1 else None
    if top["score"] >= CONFIDENT_SCORE and (second is None or top["score"] - second["score"] >= CONFIDENT_MARGIN):
        return top["column"], None
    plausible = [r for r in scored if r["score"] >= PLAUSIBLE_SCORE]
    return None, plausible if len(plausible) > 1 else scored[:3]


def next_auto_id_start(session: Session) -> int:
    """Auto-generated participant numbers continue from the highest existing
    numeric participant_id already in the system (whether from an earlier
    import or manual entry), rather than restarting at 1 every time — so a
    second import (or a manually-added participant) never collides with an
    unrelated existing person under the same number."""
    existing = session.exec(select(Person.participant_id)).all()
    max_id = 0
    for pid in existing:
        if pid and pid.isdigit():
            max_id = max(max_id, int(pid))
    return max_id + 1


def existing_normalized_names(session: Session) -> set[str]:
    """Every currently-registered participant's full name, normalized for
    duplicate matching — combines first_name + last_name the same way the
    rest of the app already does (e.g. attendees.py), so this matches both
    import-created people (name held entirely in first_name) and manually
    -entered ones (separate first/last)."""
    names: set[str] = set()
    for first, last in session.exec(select(Person.first_name, Person.last_name)).all():
        full = f"{first} {last}".strip() if last else (first or "")
        if full:
            names.add(normalize_name(full))
    return names


def existing_people_by_name(session: Session) -> dict:
    """Normalized full name -> the participant already registered under it.

    The file importer only needs to know *whether* a name exists, so it uses
    existing_normalized_names(). Sheet sync additionally needs to know WHICH
    participant a name belongs to, so a re-synced row updates that exact
    person instead of creating a second record under a new id.
    """
    out: dict[str, dict] = {}
    for p in session.exec(select(Person)).all():
        full = f"{p.first_name} {p.last_name}".strip() if p.last_name else (p.first_name or "")
        if full:
            out[normalize_name(full)] = {
                "participant_id": p.participant_id,
                "original_image_url": p.original_image_url or "",
            }
    return out


def build_sheet_sync_preview(
    df: pd.DataFrame,
    name_column: str,
    photo_column: str | None,
    start_id: int,
    existing_people: dict,
    consent_column: str | None = None,
) -> list[dict]:
    """Preview for Google Sheet sync: add new people, update changed ones.

    New rows follow exactly the same rules as build_preview - Name and Photo
    only, ids assigned in sheet order, ids never consumed by a row that will
    not be processed. The difference is that a row whose name already belongs
    to a participant is matched to that participant instead of being skipped:

      photo link changed  -> reuse their participant_id, status "ready", so
                             run_import_job's "update" strategy refreshes the
                             photo and re-runs face detection for that person
      photo link the same -> status "duplicate", which run_import_job already
                             skips before any download or face work happens

    Removing a row from the sheet does nothing here, deliberately: the sheet
    adds and corrects people, it never removes them, so an accidental deletion
    in the sheet cannot destroy a participant or their attendance history.

    When the SAME person appears more than once, the LAST row wins. A form
    response sheet is append-only: someone who re-submits to correct their photo
    produces a second row further down, and that newer row is the one they mean.
    Keeping the first occurrence instead - which is what this used to do - threw
    the new photo away as an in-file duplicate and then compared the OLD row's
    unchanged link against the database, so the update never happened and the
    person could never replace their photo.

    build_preview itself is untouched - file upload behaviour is unchanged.
    """
    rows = []
    next_id = start_id

    # Which row is each name's final appearance. Computed up front because the
    # decision needs to be known while looking at the earlier rows, not after.
    last_row_for_name: dict[str, int] = {}
    for i, row in df.iterrows():
        raw = _cell_str(row[name_column]) if name_column in df.columns else ""
        if raw:
            last_row_for_name[normalize_name(raw)] = int(i)

    for i, row in df.iterrows():
        row_number = int(i) + 2  # 1-indexed, +1 for the header row
        raw_name = _cell_str(row[name_column]) if name_column in df.columns else ""
        photo = _cell_str(row[photo_column]) if photo_column and photo_column in df.columns else ""
        # The registration form is where PDPA consent was actually given, so it
        # travels with the row and becomes a ConsentRecord once the person
        # exists. None means the sheet said nothing usable (see
        # parse_consent_value) and no record is invented in that case.
        consent = parse_consent_value(row[consent_column]) if consent_column and consent_column in df.columns else None

        if not raw_name:
            rows.append({
                "consent": consent,
                "row_number": row_number, "participant_id": None, "name": "",
                "image_url": photo, "status": "invalid", "message": "Missing name",
            })
            continue

        normalized = normalize_name(raw_name)

        # Skip every appearance except the person's last one, so a re-submission
        # replaces the earlier answer instead of being discarded by it.
        if int(i) != last_row_for_name.get(normalized, int(i)):
            rows.append({
                "consent": consent,
                "row_number": row_number, "participant_id": None, "name": raw_name,
                "image_url": photo, "status": "duplicate_in_file",
                "message": "SUPERSEDED - a later row for this person is used instead",
            })
            continue

        existing = existing_people.get(normalized)
        if existing:
            if photo and photo != existing["original_image_url"]:
                rows.append({
                    "consent": consent,
                    "row_number": row_number,
                    "participant_id": existing["participant_id"],
                    "name": raw_name,
                    "image_url": photo,
                    "status": "ready",
                    "message": "UPDATE - photo changed in the sheet",
                })
            else:
                rows.append({
                    "consent": consent,
                    "row_number": row_number,
                    "participant_id": existing["participant_id"],
                    "name": raw_name,
                    "image_url": photo,
                    "status": "duplicate",
                    "message": "Already up to date",
                })
            continue

        participant_id = f"{next_id:04d}"
        next_id += 1
        rows.append({
            "consent": consent,
            "row_number": row_number,
            "participant_id": participant_id,
            "name": raw_name,
            "image_url": photo,
            "status": "ready" if photo else "missing_photo",
            "message": "" if photo else "Missing Photo",
        })
    return rows


def build_preview(
    df: pd.DataFrame,
    name_column: str,
    photo_column: str | None,
    start_id: int,
    existing_names: set[str],
    consent_column: str | None = None,
) -> list[dict]:
    """Only Name, Photo and (optionally) Consent are extracted — every other
    column in the sheet is ignored. consent_column defaults to None, so
    Name/Photo behaviour is byte-for-byte what it was when no consent column
    is supplied. IDs are assigned strictly in file row order, never sorted,
    starting from start_id — but ONLY for rows that turn out to be new;
    duplicates (whether against an existing participant or an earlier row in
    this same file) and rows with no name never consume an ID number and are
    never processed any further. The duplicate check happens here, before
    any ID is generated, so it's also before any image download, face
    detection, embedding, or recognition-index update ever happens for that
    row — those all happen later, per-row, only for rows still "ready" or
    "missing_photo" by the time run_import_job sees them."""
    rows = []
    seen_in_file: set[str] = set()
    next_id = start_id

    for i, row in df.iterrows():
        row_number = int(i) + 2  # 1-indexed, +1 for the header row
        raw_name = _cell_str(row[name_column]) if name_column in df.columns else ""
        photo = _cell_str(row[photo_column]) if photo_column and photo_column in df.columns else ""
        # None when there is no consent column at all - the ordinary case for a
        # spreadsheet that never asked the question.
        consent = (parse_consent_value(row[consent_column])
                   if consent_column and consent_column in df.columns else None)

        if not raw_name:
            rows.append({
                "row_number": row_number, "participant_id": None, "name": "",
                "image_url": photo, "status": "invalid", "message": "Missing name",
            })
            continue

        normalized = normalize_name(raw_name)

        if normalized in existing_names:
            rows.append({
                "row_number": row_number, "participant_id": None, "name": raw_name,
                "image_url": photo, "status": "duplicate", "message": "DUPLICATE - SKIP (already exists in the system)",
            })
            continue

        if normalized in seen_in_file:
            rows.append({
                "row_number": row_number, "participant_id": None, "name": raw_name,
                "image_url": photo, "status": "duplicate_in_file", "message": "DUPLICATE IN FILE - SKIP",
            })
            continue

        seen_in_file.add(normalized)
        participant_id = f"{next_id:04d}"
        next_id += 1

        rows.append({
            "row_number": row_number,
            "participant_id": participant_id,
            "name": raw_name,
            "image_url": photo,
            "status": "ready" if photo else "missing_photo",
            "message": "" if photo else "Missing Photo",
            "consent": consent,
        })
    return rows


def _download_image_bytes(url: str) -> bytes:
    if is_google_drive_url(url):
        try:
            result = download_public_file(url)
        except GoogleDriveError as e:
            raise RuntimeError(f"PHOTO NOT ACCESSIBLE — {e}") from e
        return result.content

    try:
        resp = requests.get(url, timeout=20)
    except requests.RequestException as e:
        raise RuntimeError(f"PHOTO NOT ACCESSIBLE — could not reach the URL ({e})") from e
    if resp.status_code != 200:
        raise RuntimeError(f"PHOTO NOT ACCESSIBLE — URL returned HTTP {resp.status_code}")
    return resp.content


def _record_registration_consent(session: Session, person_id: str, consent: str | None) -> str:
    """Turn a registration form answer into a ConsentRecord. Returns what it did.

    source="registration" is deliberately distinct from "kiosk": it says the
    person answered on the sign-up form, NOT that they tapped anything at the
    kiosk. A compliance record that blurred those two would misrepresent where
    the consent actually came from.

    Nothing is written unless the answer was explicit. A blank cell (None) and
    an uninterpretable one (CONSENT_UNKNOWN) both mean "no evidence"; inventing
    a record for either would fabricate exactly what this table exists to prove.

    Precedence, highest first:

      1. an admin decision that is currently the latest record - never
         overwritten by a sync, whatever the form says
      2. otherwise, whether the form answer differs from the last one imported

    The comparison in step 2 is against this person's most recent record whose
    source is "registration" - NOT against their current overall status. That
    distinction is the whole design:

      A spreadsheet carries no per-row timestamp, so re-reading the same sheet
      tomorrow yields rows indistinguishable from today's. Comparing against
      the last registration value is what separates "the form actually changed"
      from "the same form is being read again". Comparing against the overall
      latest instead would make a routine re-sync keep re-appending the form
      answer on top of whatever the person did at the kiosk.

      no registration record yet        -> write it
      same as the last registration     -> write nothing. The form has not
                                           changed, so a re-sync is a no-op and
                                           any newer kiosk choice stands
                                           untouched.
      differs from the last registration -> the form answer genuinely changed
                                           since the last sync. Record it now,
                                           at sync time, so it becomes the
                                           current status.

    The last case can override a newer kiosk choice, and that is deliberate: an
    admin pressing Sync after the form changed is an explicit act, and the new
    form answer is newer information than the sync that came before it.
    """
    if consent not in ("consented", "declined"):
        return "no_answer"

    # An admin decision that is currently in force outranks the form entirely.
    # Someone looked at this participant and set their status by hand; a later
    # sheet sync is not evidence that they changed their mind, so registration
    # never writes over it - not even when the form answer itself has changed.
    # The override is only "in force" while it is the LATEST record: if the
    # participant afterwards taps the kiosk themselves, that is their own newer
    # decision and the ordinary registration rules resume.
    latest_overall = session.exec(
        select(ConsentRecord)
        .where(ConsentRecord.person_id == person_id)
        .order_by(ConsentRecord.recorded_at.desc())
        .limit(1)
    ).first()
    if latest_overall is not None and latest_overall.source == "admin":
        return "admin_override"

    # Only registration rows - a kiosk or admin record is never the baseline.
    latest_registration = session.exec(
        select(ConsentRecord)
        .where(ConsentRecord.person_id == person_id, ConsentRecord.source == "registration")
        .order_by(ConsentRecord.recorded_at.desc())
        .limit(1)
    ).first()

    if latest_registration is not None and latest_registration.choice == consent:
        return "unchanged"

    session.add(ConsentRecord(person_id=person_id, choice=consent, source="registration"))
    session.commit()
    return "recorded" if latest_registration is None else "updated"


def run_import_job(import_id: str, duplicate_strategy: str) -> None:
    with Session(engine) as session:
        job = session.get(ImportJob, import_id)
        if not job:
            return
        job.status = "processing"
        session.add(job)
        session.commit()

        rows = session.exec(
            select(ImportRow).where(ImportRow.import_id == import_id).order_by(ImportRow.row_number)
        ).all()

        for row in rows:
            if row.status in ("duplicate", "duplicate_in_file", "invalid"):
                # Already fully resolved at preview time — never downloads,
                # detects, embeds, or touches the recognition index or the
                # existing participant's data for these.
                #
                # Their PDPA answer is the one exception. A sheet sync marks an
                # unchanged participant "duplicate", so skipping outright meant
                # consent only ever reached people who were BRAND NEW: pressing
                # "Sync now" for an existing participant who had since answered
                # the form changed nothing, and the PDPA page kept showing
                # PENDING. Applying it here writes no photo, no embedding and
                # no index change — only the consent record, and only when the
                # row identifies exactly one existing participant.
                if row.status == "duplicate" and row.participant_id:
                    person = session.exec(
                        select(Person).where(Person.participant_id == row.participant_id)
                    ).first()
                    if person:
                        outcome = _record_registration_consent(session, person.id, row.consent)
                        # Say so in the results, otherwise a sync that DID change
                        # someone's consent looks identical to one that skipped
                        # them entirely.
                        note = _consent_note(outcome, row.consent)
                        if note:
                            row.error_message = note
                            session.add(row)
                job.skipped_count += 1
                session.add(job)
                session.commit()
                continue

            if row.status == "missing_photo" or not row.image_url:
                row.status = "error"
                row.error_message = "PHOTO NOT ACCESSIBLE — no photo provided for this participant"
                job.skipped_count += 1
                session.add(row)
                session.add(job)
                session.commit()
                continue

            try:
                job.current_stage = f"Downloading photo for {row.first_name}..."
                session.add(job)
                session.commit()
                image_bytes = _download_image_bytes(row.image_url)

                job.current_stage = f"Detecting face for {row.first_name}..."
                session.add(job)
                session.commit()
                img = storage_service.decode_image(image_bytes)
                if img is None:
                    raise RuntimeError("PHOTO NOT ACCESSIBLE — the file is not a readable image")

                # Same enrollment rule as Add Person and photo replacement, so
                # a photo accepted in one place is accepted in all of them.
                try:
                    face = select_primary_enrollment_face(detect_faces(img))
                except EnrollmentFaceError as e:
                    raise RuntimeError(f"PHOTO NOT ACCESSIBLE — {e.message}") from e

                existing = session.exec(
                    select(Person).where(Person.participant_id == row.participant_id)
                ).first()

                if existing and duplicate_strategy == "skip":
                    # The participant is skipped, but their PDPA answer is not:
                    # this branch also used to drop consent on the floor, so an
                    # existing person whose form answer changed kept their old
                    # status whenever the admin chose "Skip".
                    outcome = _record_registration_consent(session, existing.id, row.consent)
                    note = _consent_note(outcome, row.consent)
                    row.status = "skipped"
                    row.error_message = note or "Participant ID already exists (skipped)"
                    job.skipped_count += 1
                    session.add(row)
                    session.add(job)
                    session.commit()
                    continue

                job.current_stage = f"Generating embedding for {row.first_name}..."
                session.add(job)
                session.commit()

                # HEIC downloads are re-encoded so the profile photo displays in a
                # browser; the embedding above used the original bytes.
                display_bytes, ext = storage_service.to_displayable_bytes(image_bytes)
                image_path = storage_service.save_person_image(row.participant_id, display_bytes, ext)

                if existing:
                    existing.first_name = row.first_name
                    existing.last_name = ""
                    existing.image_path = image_path
                    existing.image_source = "google_drive" if is_google_drive_url(row.image_url) else "url"
                    existing.original_image_url = row.image_url
                    existing.embedding = face.embedding.tobytes()
                    existing.det_score = face.det_score
                    existing.updated_at = datetime.now()
                    session.add(existing)
                else:
                    person = Person(
                        participant_id=row.participant_id,
                        first_name=row.first_name,
                        last_name="",
                        image_path=image_path,
                        image_source="google_drive" if is_google_drive_url(row.image_url) else "url",
                        original_image_url=row.image_url,
                        embedding=face.embedding.tobytes(),
                        det_score=face.det_score,
                    )
                    session.add(person)

                row.status = "imported"
                row.error_message = None
                job.success_count += 1
                session.add(row)
                session.add(job)
                session.commit()

                # The participant is committed at this point; the index update
                # that follows is the derived cache catching up. If it fails,
                # apply_index_change rebuilds the index from the committed
                # database state, and the row stays "imported" because the
                # import genuinely did what it promised. Only if that rebuild
                # ALSO fails does the row become an error - reported here
                # rather than by the generic handler below, so the already
                # counted success is taken back instead of being counted twice.
                _record_registration_consent(session, (existing or person).id, row.consent)

                try:
                    apply_index_change(
                        session,
                        lambda: recognition_index.upsert(existing or person),
                        what=f"import participant {row.participant_id}",
                    )
                except IndexResyncFailed:
                    row.status = "error"
                    row.error_message = (
                        "SAVED BUT NOT SEARCHABLE - the participant was saved to the database, "
                        "but the recognition index could not be updated or rebuilt. "
                        "Restart the backend to reload the index from the database."
                    )
                    job.success_count -= 1
                    job.failed_count += 1
                    session.add(row)
                    session.add(job)
                    session.commit()

            except Exception as e:  # noqa: BLE001 — surface any failure as a row-level error, keep the job alive
                row.status = "error"
                row.error_message = str(e)
                job.failed_count += 1
                session.add(row)
                session.add(job)
                session.commit()

        job.status = "completed"
        job.current_stage = ""
        session.add(job)
        session.commit()
