"""Event Photo retention policy: a finite number of days, or Never.

A batch's policy is stored on the batch itself (PhotoBatch.retention_policy):

    "days"   retention_days is 1-7 and delete_at is the deadline
    "never"  no automatic deletion: delete_at is NULL and retention_days is 0

Never is its own persisted state, not a far-future date. Automatic cleanup
skips a batch when EITHER the policy says never OR it has no deadline, so
neither field alone can expose a kept batch. Manual Delete is unaffected.

The app-wide setting `photo_retention_default` only pre-selects the policy
for batches created AFTER it is saved. Changing it never touches an existing
batch: a Never batch stays Never, and a finite batch keeps the deadline it
already has. Only an explicit per-batch change (which asks for the admin
password) alters an existing batch.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session

from app.models.models import Setting

RETENTION_DAYS = "days"
RETENTION_NEVER = "never"
MIN_DAYS, MAX_DAYS = 1, 7

DEFAULT_SETTING_KEY = "photo_retention_default"
DEFAULT_SETTING_VALUE = "7"  # the long-standing default: seven days
SETTING_CHOICES = tuple(str(d) for d in range(MIN_DAYS, MAX_DAYS + 1)) + (RETENTION_NEVER,)


class RetentionError(ValueError):
    pass


def resolve(policy: str | None, days: int | None) -> tuple[str, int]:
    """Validate an explicit choice -> (policy, retention_days). Days are
    ignored for Never (stored as 0)."""
    policy = (policy or RETENTION_DAYS).strip().lower()
    if policy == RETENTION_NEVER:
        return RETENTION_NEVER, 0
    if policy != RETENTION_DAYS:
        raise RetentionError("retention_policy must be 'days' or 'never'")
    if days is None or not (MIN_DAYS <= days <= MAX_DAYS):
        raise RetentionError("retention_days must be between 1 and 7")
    return RETENTION_DAYS, int(days)


def from_setting_value(value: str | None) -> tuple[str, int]:
    """'1'..'7' or 'never'. Anything unreadable falls back to the built-in
    default rather than guessing a policy."""
    value = (value or "").strip().lower()
    if value == RETENTION_NEVER:
        return RETENTION_NEVER, 0
    if value in SETTING_CHOICES:
        return RETENTION_DAYS, int(value)
    return RETENTION_DAYS, int(DEFAULT_SETTING_VALUE)


def default_for_new_batches(session: Session) -> tuple[str, int]:
    row = session.get(Setting, DEFAULT_SETTING_KEY)
    return from_setting_value(row.value if row else DEFAULT_SETTING_VALUE)


def resolve_request(session: Session, policy: str | None, days: int | None) -> tuple[str, int]:
    """What a create-batch request means: an explicit choice if it sent one,
    otherwise the app-wide default."""
    if policy is None and days is None:
        return default_for_new_batches(session)
    return resolve(policy, days)


def deadline(policy: str, start: datetime, days: int) -> datetime | None:
    return None if policy == RETENTION_NEVER else start + timedelta(days=days)


def is_never(batch) -> bool:
    """True when automatic cleanup must leave this batch alone on retention
    grounds: the policy says never, or there is simply no deadline."""
    return getattr(batch, "retention_policy", RETENTION_DAYS) == RETENTION_NEVER or batch.delete_at is None
