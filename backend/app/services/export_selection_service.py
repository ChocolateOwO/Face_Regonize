"""Phase O — one export selection, shared by the ZIP download and the Drive upload.

Categories are MEDIA, People (SORTED) and AMBIENCE, with optional narrowing
of SORTED to chosen participants. Eligibility is ONE predicate (`eligible`)
that both consumers call, so the two can never drift apart:

  * ORIGINAL, THUMBNAILS, LOGO, MASK, UPLOAD_STAGING and anything else are
    never eligible, whatever a request asks for;
  * REVIEW is not a category here. A new-workflow selection can never contain
    it. The LEGACY full ZIP (no selection given) is a separate regime that keeps
    a historical batch's REVIEW/ folder, exactly as before.

Nothing here reprocesses anything: it only chooses among files that the
existing, validated manifest already vouched for.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from sqlmodel import Session, select

from app.services.photo_batch_download_service import DownloadNotReady, _safe_path, output_manifest

CATEGORIES = ("media", "sorted", "ambience")
_DIRS = {"media": "MEDIA", "sorted": "SORTED", "ambience": "AMBIENCE"}


class ExportSelectionError(ValueError):
    """An unusable selection. The message is shown to the admin."""


@dataclass(frozen=True)
class ExportSelection:
    media: bool = True
    sorted: bool = True
    ambience: bool = False
    person_ids: tuple[str, ...] | None = None  # None = every matched participant

    def validate(self) -> "ExportSelection":
        if not (self.media or self.sorted or self.ambience):
            raise ExportSelectionError("Select at least one of MEDIA, People (SORTED) or AMBIENCE.")
        if self.person_ids is not None and self.sorted and not self.person_ids:
            raise ExportSelectionError("Choose at least one participant, or include all participants.")
        return self


def parse_query(select_value: str | None, person_ids: str | None = None) -> ExportSelection:
    """`select=media,sorted,ambience` (+ optional `person_ids=a,b`)."""
    chosen = [c.strip().lower() for c in (select_value or "").split(",") if c.strip()]
    unknown = [c for c in chosen if c not in CATEGORIES]
    if unknown:
        raise ExportSelectionError(f"Not an export category: {', '.join(unknown)}. Choose from MEDIA, SORTED, AMBIENCE.")
    ids = None if person_ids is None else tuple(i for i in (x.strip() for x in person_ids.split(",")) if i)
    return ExportSelection(media="media" in chosen, sorted="sorted" in chosen, ambience="ambience" in chosen,
                           person_ids=ids).validate()


def sorted_folder_names(session: Session, person_ids) -> set[str] | None:
    """The SORTED folder name of each chosen participant (None = no narrowing)."""
    if person_ids is None:
        return None
    from app.models.models import Person
    from app.services.photo_processing_service import _safe_folder_name

    ids = list(person_ids)
    if not ids:
        return set()
    people = session.exec(select(Person).where(Person.id.in_(ids))).all()
    return {_safe_folder_name(p.participant_id, p.first_name, p.last_name) for p in people}


def eligible(parts: tuple[str, ...], selection: ExportSelection, sorted_folders: set[str] | None) -> bool:
    """THE eligibility rule, for a path relative to the batch output root."""
    if not parts:
        return False
    top = parts[0]
    if top == "MEDIA":
        return selection.media and len(parts) == 2
    if top == "SORTED":
        return (selection.sorted and len(parts) == 3
                and (sorted_folders is None or parts[1] in sorted_folders))
    if top == "AMBIENCE":
        return selection.ambience and len(parts) == 2
    return False  # REVIEW, ORIGINAL, THUMBNAILS, LOGO, MASK, UPLOAD_STAGING, anything else


def _ambience_files(root: Path, photos) -> list[Path]:
    names = {p.filename for p in photos if p.media_path and p.classification == "ambience"}
    directory = root / "AMBIENCE"
    if not names or not directory.exists():
        return []
    _safe_path(root, directory)
    files = []
    for parent, dirs, filenames in os.walk(directory, followlinks=False):
        for name in dirs:
            _safe_path(root, Path(parent) / name)
        for name in sorted(filenames):
            if name not in names:
                continue  # only recorded, completed images — never an arbitrary file
            file = _safe_path(root, Path(parent) / name)
            if file.stat().st_size == 0:
                raise DownloadNotReady("An AMBIENCE image is empty.")
            files.append(file)
    return files


def zip_manifest(batch, photos, storage: Path, selection: ExportSelection,
                 sorted_folders: set[str] | None) -> tuple[Path, list[Path]]:
    """The selected files, after the SAME completeness and path validation the
    full ZIP performs (output_manifest). Raises ExportSelectionError if the
    selection matches nothing."""
    root, files = output_manifest(batch, photos, storage)
    if selection.ambience:
        files = files + _ambience_files(root, photos)
    chosen = [f for f in files if eligible(f.relative_to(root).parts, selection, sorted_folders)]
    if not chosen:
        raise ExportSelectionError("Nothing in this batch matches the selected outputs.")
    return root, chosen
