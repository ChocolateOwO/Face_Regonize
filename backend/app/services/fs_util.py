"""Small filesystem helpers shared by the photo-output writers.

`os.replace` onto an existing file fails on Windows with PermissionError
(WinError 5 / 32) while ANY other handle has the destination open without
delete-sharing — the file route streaming that same MEDIA or thumbnail to a
browser, or antivirus/indexing scanning a file that was just written. Those
handles are brief, so a short bounded retry turns a spurious failure (which
would otherwise roll back a whole face-box edit) into a normal success, and a
genuinely stuck file still fails after well under a second and a half.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

REPLACE_ATTEMPTS = 8
REPLACE_BACKOFF_S = 0.02  # doubles each attempt, capped below


def replace_with_retry(src: Path, dst: Path, *, attempts: int = REPLACE_ATTEMPTS,
                       backoff_s: float = REPLACE_BACKOFF_S) -> None:
    """`os.replace(src, dst)`, retrying only PermissionError. Any other OSError
    (e.g. a missing source) is raised immediately; the last PermissionError is
    re-raised if every attempt fails."""
    delay = backoff_s
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.4)
