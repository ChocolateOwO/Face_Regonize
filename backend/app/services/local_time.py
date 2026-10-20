"""Timezone-aware output for timestamps stored as naive local time.

FaceDetection.detected_at and Attendance.detected_at are written with
datetime.now() (models.py default_factory): the server's local wall-clock
time, stored without an offset. That meaning is fixed by the writers, so the
offset can be attached on the way out without changing a stored value:
naive.astimezone() reads a naive value as system local time and returns the
same instant with its offset (+07:00 on this server; DST-correct elsewhere).
"""
from __future__ import annotations

from datetime import datetime


def aware(value: datetime | None) -> datetime | None:
    """Stored naive local time -> the same instant with its UTC offset."""
    if value is None:
        return None
    return value.astimezone() if value.tzinfo is None else value


def to_stored(value: datetime) -> datetime:
    """A filter bound from a client -> the naive local form the column uses.
    A naive bound is taken as local time, as before."""
    return value if value.tzinfo is None else value.astimezone().replace(tzinfo=None)
