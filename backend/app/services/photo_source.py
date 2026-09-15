"""Source abstraction for Event Photo intake (Phase A1, extended in A2).

Wraps today's Drive calls with ZERO logic change — DrivePhotoSource simply
delegates to list_image_files()/download_file(), nothing reimplemented,
nothing reordered, nothing re-validated. This exists so a local-upload
source (Phase A2, below) and the continuous pipeline (Phase B) have one
call shape to plug into, without this phase changing any observable
behavior for existing Drive-sourced batches.

list_fn/fetch_fn are constructor parameters (not hardcoded inside this
class) specifically so callers construct a DrivePhotoSource using THEIR
OWN currently-bound names for list_image_files/download_file — this is
what keeps existing tests that patch those two names on
photo_processing_service's module namespace working unmodified: the
lookup happens in the caller's frame at construction time, not here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.services.google_drive_folder_service import (
    DriveImageFile,
    download_file as _download_file,
    list_image_files as _list_image_files,
)


class PhotoSourceItem(Protocol):
    id: str
    name: str


class PhotoSource(Protocol):
    def list_items(self) -> list[PhotoSourceItem]: ...

    def fetch(self, item: PhotoSourceItem) -> bytes: ...


class DrivePhotoSource:
    """Behavior-preserving wrapper — list_items()/fetch() ARE
    list_image_files()/download_file(), just called through this shape.
    Any DriveFolderError from list_image_files() propagates unchanged."""

    def __init__(
        self,
        folder_id: str,
        list_fn: Callable[[str], list[DriveImageFile]] = _list_image_files,
        fetch_fn: Callable[[str], bytes] = _download_file,
    ) -> None:
        self.folder_id = folder_id
        self._list_fn = list_fn
        self._fetch_fn = fetch_fn

    def list_items(self) -> list[DriveImageFile]:
        return self._list_fn(self.folder_id)

    def fetch(self, item: DriveImageFile) -> bytes:
        return self._fetch_fn(item.id)


@dataclass(frozen=True)
class LocalUploadItem:
    id: str  # the staged, sanitized filename — stable, used as the lookup key
    name: str


def sanitize_upload_basename(raw_name: str) -> str:
    """Strips any directory component (browsers may send a full relative
    path for folder uploads) and rejects anything that isn't a plain
    filename — no traversal, no empty/dot-only names, no separators left
    after basename extraction (a crafted name like 'a/../../x' still
    resolves to a harmless basename here since only the last path segment
    survives)."""
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."} or "\x00" in name:
        raise ValueError(f"Unsupported filename: {raw_name!r}")
    return name


def dedupe_upload_basename(name: str, taken: set[str]) -> str:
    """Duplicate-basename suffixing — two files named IMG_0001.jpg in the
    same upload (e.g. from two different folders) must not silently
    overwrite each other in the staging directory."""
    if name not in taken:
        taken.add(name)
        return name
    stem, _, ext = name.rpartition(".")
    stem, ext = (stem, "." + ext) if stem else (name, "")
    n = 1
    while f"{stem}_{n}{ext}" in taken:
        n += 1
    candidate = f"{stem}_{n}{ext}"
    taken.add(candidate)
    return candidate


class LocalUploadPhotoSource:
    """Reads whatever was already streamed into `staging_dir` by the upload
    endpoint (see photo_batches.py's upload route) before this source is
    ever constructed. list_items()/fetch() are the ONLY thing this class
    does — filename sanitization and dedup already happened once, at
    staging time, not here; this class trusts the staging directory's own
    contents are exactly what should be offered to the existing
    accept/reject pipeline (staging is a plain, flat, pre-validated set of
    files — no further traversal risk to check twice)."""

    def __init__(self, staging_dir: Path) -> None:
        self.staging_dir = staging_dir

    def list_items(self) -> list[LocalUploadItem]:
        return sorted(
            (LocalUploadItem(id=p.name, name=p.name) for p in self.staging_dir.iterdir() if p.is_file()),
            key=lambda item: item.name,
        )

    def fetch(self, item: LocalUploadItem) -> bytes:
        path = self.staging_dir / item.id
        if path.parent != self.staging_dir or not path.is_file():
            raise FileNotFoundError(f"Staged upload not found: {item.id}")
        return path.read_bytes()
