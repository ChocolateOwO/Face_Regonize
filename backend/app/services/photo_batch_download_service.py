"""Read-only export of completed local output; no Drive or processing calls."""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

from starlette.responses import FileResponse


class DownloadNotReady(ValueError):
    pass


OUTPUTS = ("SORTED", "REVIEW", "MEDIA")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".gif", ".avif", ".jp2", ".jpe", ".jfif", ".dib", ".ppm", ".pgm", ".pbm", ".pnm", ".exr", ".hdr", ".pic"}


def _safe_path(root: Path, path: Path) -> Path:
    """Reject escapes, links/junctions and non-regular files (including hardlinks)."""
    relative = path.relative_to(root)
    current = root
    for part in ("", *relative.parts):
        if part:
            current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DownloadNotReady("Linked output paths cannot be downloaded.")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise DownloadNotReady("Invalid output file type.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise DownloadNotReady("Linked output files cannot be downloaded.")
    path.resolve(strict=True).relative_to(root.resolve(strict=True))
    return path


def batch_output_root(batch, storage: Path) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", batch.id):
        raise DownloadNotReady("Invalid batch storage identity.")
    expected = f"photo_batches/{batch.id}"
    if batch.storage_dir.replace("\\", "/") != expected:
        raise DownloadNotReady("Batch storage does not match its identity.")
    storage = storage.absolute()
    return _safe_path(storage, storage / "photo_batches" / batch.id)


def local_output_ready(batch, storage: Path) -> bool:
    """Cheap UI hint. The download itself additionally validates the file manifest.

    Existing Phase 2/completed states are published only after the local loop.
    Counters alone are insufficient: they also advance for failed photos.
    Drive failures normally keep status=completed in the existing pipeline.
    """
    if batch.status not in {"syncing_drive", "completed", "failed"}:
        return False
    if batch.status == "failed" and not batch.drive_error:
        return False
    if not (batch.total_photos > 0 and batch.processed_photos == batch.total_photos
            and 0 <= batch.failed_photos < batch.total_photos):
        return False
    try:
        root = batch_output_root(batch, storage)
        return all(_safe_path(root, root / name).is_dir() for name in ("SORTED", "MEDIA"))
    except (OSError, ValueError):
        return False


def output_manifest(batch, photos, storage: Path) -> tuple[Path, list[Path]]:
    if not local_output_ready(batch, storage):
        raise DownloadNotReady("Local processing is incomplete or output is unavailable.")
    root = batch_output_root(batch, storage)
    completed = [p for p in photos if p.media_path]
    if len(completed) != batch.total_photos - batch.failed_photos:
        raise DownloadNotReady("Completed local photo records are incomplete.")
    names = set()
    storage_relative = batch.storage_dir.replace("\\", "/")
    for photo in completed:
        if (photo.batch_id != batch.id or Path(photo.filename).name != photo.filename
                or any(c in photo.filename for c in '/\\:') or photo.filename.startswith('.')
                or Path(photo.filename).suffix.lower() not in IMAGE_EXTENSIONS
                or photo.media_path.replace('\\', '/') != f"{storage_relative}/MEDIA/{photo.filename}"):
            raise DownloadNotReady("Invalid batch-local photo path.")
        media = _safe_path(root, root / "MEDIA" / photo.filename)
        if not media.is_file() or media.stat().st_size == 0:
            raise DownloadNotReady("A completed MEDIA file is missing or empty.")
        names.add(photo.filename)
    files = []
    for output in OUTPUTS:
        directory = root / output
        if not directory.exists() and output == "REVIEW":
            continue
        _safe_path(root, directory)
        for parent, dirs, filenames in os.walk(directory, followlinks=False):
            for name in dirs:
                _safe_path(root, Path(parent) / name)
            for name in sorted(filenames):
                # Only recorded completed images, never arbitrary files in storage.
                if name not in names:
                    continue
                file = _safe_path(root, Path(parent) / name)
                parts = file.relative_to(root).parts
                if len(parts) != (3 if output == "SORTED" else 2):
                    continue
                if file.stat().st_size == 0:
                    raise DownloadNotReady("An output image is empty.")
                files.append(file)
    file_set = set(files)
    sorted_counts = Counter(f.name for f in files if f.relative_to(root).parts[0] == "SORTED")
    for photo in completed:
        if photo.classification == "review" and root / "REVIEW" / photo.filename not in file_set:
            raise DownloadNotReady("A completed REVIEW file is missing.")
        if sorted_counts[photo.filename] < photo.faces_matched:
            raise DownloadNotReady("A completed SORTED copy is missing.")
    return root, files


def build_output_zip(root: Path, files: list[Path]) -> Path:
    """ZIP_STORED avoids recompressing images. Bounded RAM; temp outside storage.

    No processing lock is acquired. Deletion/expiry races fail the download;
    they never change or cancel the processing worker.
    """
    fd, name = tempfile.mkstemp(prefix="reconize-download-", suffix=".zip")
    os.close(fd)
    archive = Path(name)
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zip_file:
            for output in OUTPUTS:
                if (root / output).is_dir():
                    zip_file.writestr(output + "/", b"")
            for file in files:
                _safe_path(root, file)
                before = file.stat()
                with file.open("rb") as source, zip_file.open(file.relative_to(root).as_posix(), "w", force_zip64=True) as dest:
                    shutil.copyfileobj(source, dest, length=1024 * 1024)
                after = file.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise DownloadNotReady("Local output changed while preparing download.")
        for file in files:
            _safe_path(root, file)
        return archive
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


class TemporaryZipResponse(FileResponse):
    """Remove temporary ZIP on success, send failure or client disconnect."""

    async def __call__(self, scope, receive, send):
        try:
            # Finish sending bytes before unlinking, even on servers supporting
            # deferred pathsend offload.
            scope = {**scope, "extensions": {k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"}}
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)
