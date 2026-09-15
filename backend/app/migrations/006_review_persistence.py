"""Review persistence (Phase E1) — one new column on photobatchface.

Adds `manual_mask`, the persistent reviewer override for a single face:

    NULL  -> no override, use the computed privacy decision
    1     -> reviewer force-hid an otherwise-visible face
    0     -> reviewer explicitly confirmed this face visible

NULL (not 0) is the "no decision yet" value on purpose: every face that
already exists predates the Review workspace and has had no reviewer
decision, which is a different thing from a reviewer having actively
confirmed it visible. Storing 0 for those would fabricate decisions nobody
made and would be indistinguishable from a real confirmation in the audit
trail.

The two tables this phase also needs — PhotoBatchReviewItem and
PhotoBatchReviewDecision — are brand-new, and create_all() (which runs
before migrations, see app/migrations/__init__.py) creates them
automatically, so they need no ALTER-based migration of their own. Same
pattern as PhotoBatchIngestionIssue in migration 005.

Idempotent by construction via add_column_if_missing.

ROLLBACK NOTE: as with 004/005, rolling this back means reverting the
application code that reads/writes the column, NOT dropping it. The column
is additive and nullable, so code that predates it is entirely unaffected
by its presence.
"""
from __future__ import annotations

import sqlite3

from app.migrations.runner import add_column_if_missing


def upgrade(conn: sqlite3.Connection) -> None:
    add_column_if_missing(conn, "photobatchface", "manual_mask", "BOOLEAN")
