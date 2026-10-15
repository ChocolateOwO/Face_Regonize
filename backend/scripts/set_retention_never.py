"""Offline: stop automatic deletion of saved Event Photo batches.

Run from backend/ while the backend is STOPPED, against the database the
backend uses (DATABASE_URL in .env - the same one the server opens):

    python scripts/set_retention_never.py --existing-batches --default
    python scripts/set_retention_never.py --existing-batches --default --dry-run

  --existing-batches  every batch that exists right now gets retention
                      "never": no deadline, never removed by automatic cleanup.
                      Only retention fields change (policy, days, deadline).
  --default           the app-wide default for NEW batches becomes "never"
                      (Settings: photo_retention_default).

It first brings the schema up to date through the server's own startup path
(tables + pending migrations), because the Never state needs migration 013.
It deletes nothing, recreates nothing, and touches no participant,
recognition, attendance or import data. Manual Delete in the app still works.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from app.models.models import PhotoBatch, Setting  # noqa: E402
from app.services import retention_policy  # noqa: E402


def convert(session: Session, *, existing_batches: bool, default: bool, dry_run: bool = False) -> dict:
    """Returns what was (or with dry_run, would be) changed."""
    changed = []
    if existing_batches:
        for batch in session.exec(select(PhotoBatch).order_by(PhotoBatch.created_at)).all():
            already = batch.retention_policy == retention_policy.RETENTION_NEVER and batch.delete_at is None
            changed.append({"id": batch.id, "label": batch.label, "status": batch.status,
                            "was_policy": batch.retention_policy, "was_days": batch.retention_days,
                            "was_delete_at": batch.delete_at.isoformat(sep=" ") if batch.delete_at else None,
                            "already_never": already})
            if not already and not dry_run:
                batch.retention_policy = retention_policy.RETENTION_NEVER
                batch.retention_days = 0
                batch.delete_at = None
                session.add(batch)
    previous_default = None
    if default:
        row = session.get(Setting, retention_policy.DEFAULT_SETTING_KEY)
        previous_default = row.value if row else None
        if not dry_run:
            if row:
                row.value = retention_policy.RETENTION_NEVER
            else:
                row = Setting(key=retention_policy.DEFAULT_SETTING_KEY, value=retention_policy.RETENTION_NEVER)
            session.add(row)
    if not dry_run:
        session.commit()
    return {"batches": changed, "default_changed": default, "previous_default": previous_default}


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--existing-batches", action="store_true")
    ap.add_argument("--default", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not (args.existing_batches or args.default):
        ap.error("choose --existing-batches and/or --default")

    from app.config import DATABASE_PATH
    from app.database.db import engine, init_db

    print(f"database: {DATABASE_PATH.resolve()}")
    if not DATABASE_PATH.exists():
        print("refused: that database file does not exist (run this from backend/).")
        return 2
    if args.dry_run:
        import sqlite3

        raw = sqlite3.connect(f"file:{DATABASE_PATH.resolve().as_posix()}?mode=ro", uri=True)
        migrated = any(r[1] == "retention_policy" for r in raw.execute("PRAGMA table_info(photobatch)"))
        if not migrated:
            rows = raw.execute("SELECT id, status, retention_days, delete_at FROM photobatch ORDER BY created_at").fetchall()
            raw.close()
            print("dry run: migration 013 is not applied yet; a real run applies pending migrations first.")
            for row in rows:
                print(f"  batch {row[0]}  status={row[1]}  would set never (was days, {row[2]} day(s), deadline {row[3]})")
            print(f"batches examined: {len(rows)}")
            return 0
        raw.close()
    else:
        init_db()  # tables + pending migrations, exactly as server startup does
    with Session(engine) as session:
        result = convert(session, existing_batches=args.existing_batches, default=args.default, dry_run=args.dry_run)
    verb = "would set" if args.dry_run else "set"
    for b in result["batches"]:
        state = "already never" if b["already_never"] else f"{verb} never (was {b['was_policy']}, {b['was_days']} day(s), deadline {b['was_delete_at']})"
        print(f"  batch {b['id']}  status={b['status']}  {state}")
    if args.existing_batches:
        print(f"batches examined: {len(result['batches'])}")
    if args.default:
        print(f"default for new batches: {verb} never (was {result['previous_default'] or 'unset = 7 days'})")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(run())
