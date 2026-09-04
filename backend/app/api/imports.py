from __future__ import annotations

import io

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import ImportJob, ImportRow, User
from app.services.export_service import to_csv_bytes, to_xlsx_bytes
from pydantic import BaseModel

from app.models.models import Setting
from app.services.google_sheet_service import (
    GoogleSheetError,
    fetch_sheet_csv,
    sheet_display_name,
)
from app.services.import_service import (
    NAME_HEADER_KEYWORDS,
    PHOTO_HEADER_KEYWORDS,
    build_preview,
    build_sheet_sync_preview,
    CONSENT_CHANGED_NOTES,
    CONSENT_NOTE_UNKNOWN,
    CONSENT_UNKNOWN,
    detect_consent_column,
    existing_normalized_names,
    existing_people_by_name,
    next_auto_id_start,
    parse_spreadsheet,
    pick_column,
    run_import_job,
    score_columns,
    name_content_score,
    photo_content_score,
)

router = APIRouter(prefix="/api", tags=["imports"])


@router.get("/import/template")
def download_template():
    """Just an example — no template is required. Any spreadsheet with a
    recognizable Name and Photo column works, in any order, with any other
    columns present (they're ignored)."""
    rows = [
        {"Name": "John Doe", "Photo": "https://drive.google.com/file/d/FILE_ID/view"},
        {"Name": "Jane Smith", "Photo": "https://drive.google.com/file/d/FILE_ID/view"},
    ]
    data = to_xlsx_bytes(rows, sheet_name="Participants")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=participant_import_example.xlsx"},
    )


@router.post("/import/preview")
def preview_import(
    file: UploadFile = File(...),
    name_column: str | None = Form(None),
    photo_column: str | None = Form(None),
    photo_column_none: bool = Form(False),  # user explicitly said "this file has no photo column"
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    filename = file.filename or "import.csv"
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext not in ("csv", "xlsx", "xls"):
        raise HTTPException(400, "Unsupported file type. Use .csv, .xlsx, or .xls")

    file_bytes = file.file.read()
    try:
        df, header_row = parse_spreadsheet(file_bytes, filename)
    except Exception as e:
        raise HTTPException(400, f"Could not read spreadsheet: {e}")

    if df.empty:
        raise HTTPException(400, "No data rows found in this file.")

    columns = list(df.columns)

    # Resolve the Name column: explicit override wins, else auto-detect.
    if name_column and name_column in columns:
        resolved_name_column = name_column
        name_candidates = None
    else:
        name_scored = score_columns(df, NAME_HEADER_KEYWORDS, name_content_score)
        resolved_name_column, name_candidates = pick_column(name_scored)

    # Resolve the Photo column: explicit override or explicit "none" wins, else auto-detect.
    if photo_column_none:
        resolved_photo_column = None
        photo_candidates = None
    elif photo_column and photo_column in columns:
        resolved_photo_column = photo_column
        photo_candidates = None
    else:
        photo_scored = score_columns(df, PHOTO_HEADER_KEYWORDS, photo_content_score)
        resolved_photo_column, photo_candidates = pick_column(photo_scored)
        if resolved_photo_column is None and photo_candidates:
            photo_candidates = photo_candidates + [{"column": None, "score": 0, "samples": [], "label": "No photo column in this file"}]

    needs = []
    if name_candidates is not None:
        needs.append("name")
    if photo_candidates is not None:
        needs.append("photo")

    if needs:
        # Don't guess — hand the ambiguity back to the user instead of
        # creating any import job yet.
        return {
            "status": "needs_column_selection",
            "needs": needs,
            "columns": columns,
            "header_row": header_row,
            "name_candidates": name_candidates,
            "photo_candidates": photo_candidates,
            "resolved_name_column": resolved_name_column,
            "resolved_photo_column": resolved_photo_column,
        }

    # Consent is optional and header-detected only. Name/Photo resolution above
    # is untouched: a file with no consent column behaves exactly as before.
    resolved_consent_column = detect_consent_column(df)

    start_id = next_auto_id_start(session)
    existing_names = existing_normalized_names(session)
    preview_rows = build_preview(
        df, resolved_name_column, resolved_photo_column, start_id, existing_names,
        resolved_consent_column,
    )

    job = ImportJob(
        filename=filename,
        file_type=ext,
        total_rows=len(preview_rows),
        status="pending",
        imported_by=user.id,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    for row in preview_rows:
        session.add(ImportRow(
            import_id=job.id,
            row_number=row["row_number"],
            participant_id=row["participant_id"],
            first_name=row["name"],  # holds the full detected name, not just a "first" name
            image_url=row["image_url"] or None,
            status=row["status"],
            error_message=row["message"] or None,
            consent=row.get("consent"),
        ))
    session.commit()

    new_count = sum(1 for r in preview_rows if r["status"] in ("ready", "missing_photo"))
    duplicate_count = sum(1 for r in preview_rows if r["status"] in ("duplicate", "duplicate_in_file"))
    invalid_count = sum(1 for r in preview_rows if r["status"] == "invalid")

    return {
        "status": "ok",
        "import_id": job.id,
        "name_column": resolved_name_column,
        "photo_column": resolved_photo_column,
        "consent_column": resolved_consent_column,
        "consented_count": sum(1 for r in preview_rows if r.get("consent") == "consented"),
        "declined_count": sum(1 for r in preview_rows if r.get("consent") == "declined"),
        "unknown_consent_count": sum(1 for r in preview_rows if r.get("consent") == CONSENT_UNKNOWN),
        "total_rows": len(preview_rows),
        "ready_count": sum(1 for r in preview_rows if r["status"] == "ready"),
        "missing_photo_count": sum(1 for r in preview_rows if r["status"] == "missing_photo"),
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "invalid_count": invalid_count,
        "preview": [
            {
                "row_number": r["row_number"],
                "participant_id": r["participant_id"],
                "name": r["name"],
                "has_photo": bool(r["image_url"]),
                "status": r["status"],
                "message": r["message"],
                # null = blank cell (stays pending); "unknown" = the cell had
                # text nobody could interpret, which the UI flags as a warning.
                "consent": r.get("consent"),
            }
            for r in preview_rows
        ],
    }


class SheetPreviewRequest(BaseModel):
    url: str
    name_column: str | None = None
    photo_column: str | None = None
    photo_column_none: bool = False


@router.post("/import/sheet/preview")
def preview_sheet_import(
    body: SheetPreviewRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Read a shared Google Sheet and build the same preview a file upload
    produces, except that a row whose name already belongs to a participant is
    matched to that participant so the sync can update them.

    The sheet is fetched fresh on every call - that is what makes "Sync now"
    pick up rows added since the last sync. Nothing polls in the background.
    """
    try:
        file_bytes = fetch_sheet_csv(body.url)
    except GoogleSheetError as e:
        raise HTTPException(400, str(e))

    try:
        df, header_row = parse_spreadsheet(file_bytes, "sheet.csv")
    except Exception as e:
        raise HTTPException(400, f"Could not read that sheet: {e}")

    if df.empty:
        raise HTTPException(400, "No data rows found in that sheet.")

    columns = list(df.columns)

    if body.name_column and body.name_column in columns:
        resolved_name_column = body.name_column
        name_candidates = None
    else:
        name_scored = score_columns(df, NAME_HEADER_KEYWORDS, name_content_score)
        resolved_name_column, name_candidates = pick_column(name_scored)

    if body.photo_column_none:
        resolved_photo_column = None
        photo_candidates = None
    elif body.photo_column and body.photo_column in columns:
        resolved_photo_column = body.photo_column
        photo_candidates = None
    else:
        photo_scored = score_columns(df, PHOTO_HEADER_KEYWORDS, photo_content_score)
        resolved_photo_column, photo_candidates = pick_column(photo_scored)
        if resolved_photo_column is None and photo_candidates:
            photo_candidates = photo_candidates + [{"column": None, "score": 0, "samples": [], "label": "No photo column in this sheet"}]

    resolved_consent_column = detect_consent_column(df)

    needs = []
    if name_candidates is not None:
        needs.append("name")
    if photo_candidates is not None:
        needs.append("photo")

    if needs:
        return {
            "status": "needs_column_selection",
            "needs": needs,
            "columns": columns,
            "header_row": header_row,
            "name_candidates": name_candidates,
            "photo_candidates": photo_candidates,
            "resolved_name_column": resolved_name_column,
            "resolved_photo_column": resolved_photo_column,
        }

    start_id = next_auto_id_start(session)
    existing_people = existing_people_by_name(session)
    preview_rows = build_sheet_sync_preview(
        df, resolved_name_column, resolved_photo_column, start_id, existing_people,
        resolved_consent_column,
    )

    # Remember the link so "Sync now" does not need it pasted again.
    setting = session.get(Setting, "import_sheet_url")
    if setting:
        setting.value = body.url
    else:
        setting = Setting(key="import_sheet_url", value=body.url)
    session.add(setting)

    job = ImportJob(
        filename=sheet_display_name(body.url),
        file_type="google_sheet",
        total_rows=len(preview_rows),
        status="pending",
        imported_by=user.id,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    for row in preview_rows:
        session.add(ImportRow(
            import_id=job.id,
            row_number=row["row_number"],
            participant_id=row["participant_id"],
            first_name=row["name"],
            image_url=row["image_url"] or None,
            status=row["status"],
            error_message=row["message"] or None,
            consent=row.get("consent"),
        ))
    session.commit()

    update_count = sum(1 for r in preview_rows if r["status"] == "ready" and r["message"].startswith("UPDATE"))
    new_count = sum(1 for r in preview_rows if r["status"] in ("ready", "missing_photo")) - update_count

    return {
        "status": "ok",
        "import_id": job.id,
        "sheet_url": body.url,
        "name_column": resolved_name_column,
        "photo_column": resolved_photo_column,
        "consent_column": resolved_consent_column,
        "consented_count": sum(1 for r in preview_rows if r.get("consent") == "consented"),
        "declined_count": sum(1 for r in preview_rows if r.get("consent") == "declined"),
        "no_consent_answer_count": sum(1 for r in preview_rows if r.get("consent") is None),
        "unknown_consent_count": sum(1 for r in preview_rows if r.get("consent") == CONSENT_UNKNOWN),
        "total_rows": len(preview_rows),
        "ready_count": sum(1 for r in preview_rows if r["status"] == "ready"),
        "missing_photo_count": sum(1 for r in preview_rows if r["status"] == "missing_photo"),
        "new_count": new_count,
        "update_count": update_count,
        "unchanged_count": sum(1 for r in preview_rows if r["status"] == "duplicate"),
        "duplicate_count": sum(1 for r in preview_rows if r["status"] in ("duplicate", "duplicate_in_file")),
        "invalid_count": sum(1 for r in preview_rows if r["status"] == "invalid"),
        "preview": [
            {
                "row_number": r["row_number"],
                "participant_id": r["participant_id"],
                "name": r["name"],
                "has_photo": bool(r["image_url"]),
                "status": r["status"],
                "message": r["message"],
                # null = blank cell (stays pending); "unknown" = the cell had
                # text nobody could interpret, which the UI flags as a warning.
                "consent": r.get("consent"),
            }
            for r in preview_rows
        ],
    }


@router.get("/import/sheet/url")
def get_sheet_url(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """The last sheet synced, so the page can offer it again."""
    setting = session.get(Setting, "import_sheet_url")
    return {"url": setting.value if setting else ""}


@router.post("/import/{import_id}/confirm")
def confirm_import(
    import_id: str,
    background_tasks: BackgroundTasks,
    duplicate_strategy: str = Form("skip"),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    job = session.get(ImportJob, import_id)
    if not job:
        raise HTTPException(404, "Import job not found")
    if job.status != "pending":
        raise HTTPException(409, f"Import job already {job.status}")

    job.duplicate_strategy = duplicate_strategy
    session.add(job)
    session.commit()

    background_tasks.add_task(run_import_job, import_id, duplicate_strategy)
    return {"import_id": import_id, "status": "processing"}


@router.get("/imports")
def list_imports(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    jobs = session.exec(select(ImportJob).order_by(ImportJob.imported_at.desc())).all()
    return jobs


@router.get("/imports/{import_id}")
def get_import(import_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    job = session.get(ImportJob, import_id)
    if not job:
        raise HTTPException(404, "Import job not found")
    rows = session.exec(select(ImportRow).where(ImportRow.import_id == import_id).order_by(ImportRow.row_number)).all()
    # Counted from the per-row notes rather than a new column, so the sync
    # result can report PDPA changes without a schema migration.
    return {
        "job": job,
        "rows": rows,
        "pdpa_updated": sum(1 for r in rows if r.error_message in CONSENT_CHANGED_NOTES),
        "pdpa_warnings": sum(1 for r in rows if r.error_message == CONSENT_NOTE_UNKNOWN),
    }


@router.get("/imports/{import_id}/errors.csv")
def download_import_errors(import_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    rows = session.exec(
        select(ImportRow).where(ImportRow.import_id == import_id, ImportRow.status.in_(["error", "skipped"]))
    ).all()
    data = to_csv_bytes([
        {
            "Row": r.row_number,
            "Participant ID": r.participant_id,
            "Name": r.first_name,
            "Status": r.status,
            "Reason": r.error_message,
        }
        for r in rows
    ])
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=import_{import_id}_errors.csv"},
    )
