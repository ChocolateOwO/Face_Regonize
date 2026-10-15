"""Create, reset or delete a machine account for the robot integration.

A machine account logs in through the normal POST /api/auth/login, but the
backend only lets it call the robot-integration allowlist (see
app/auth/deps.py: get_api_client). It never needs the admin password.

Run from backend/ against the database the backend uses (DATABASE_URL in
.env, same as the server):

    python scripts/manage_machine_user.py create --username robot-hub
    python scripts/manage_machine_user.py reset  --username robot-hub
    python scripts/manage_machine_user.py delete --username robot-hub
    python scripts/manage_machine_user.py list

create/reset print a newly generated password ONCE; only its bcrypt hash is
stored. The script refuses to touch any account whose role is not "machine",
so it can never change or remove the admin.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from app.auth.deps import MACHINE_ROLE  # noqa: E402
from app.auth.security import hash_password  # noqa: E402
from app.config import DATABASE_PATH  # noqa: E402
from app.models.models import User  # noqa: E402


def run(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["create", "reset", "delete", "list"])
    ap.add_argument("--username")
    args = ap.parse_args(argv)
    if args.action != "list" and not args.username:
        ap.error("--username is required")
    if engine is None:
        from app.database.db import engine, init_db

        init_db()  # same startup path as the server: tables + pending migrations
        print(f"database: {DATABASE_PATH}")
    with Session(engine) as session:
        if args.action == "list":
            for user in session.exec(select(User).where(User.role == MACHINE_ROLE)).all():
                print(f"{user.username}  created {user.created_at:%Y-%m-%d %H:%M}")
            return 0
        user = session.exec(select(User).where(User.username == args.username)).first()
        if user is not None and user.role != MACHINE_ROLE:
            print(f"refused: '{args.username}' exists and is not a machine account (role={user.role}).")
            return 2
        if args.action == "delete":
            if user is None:
                print("no such machine account")
                return 1
            session.delete(user)
            session.commit()
            print(f"deleted machine account '{args.username}'")
            return 0
        if args.action == "create" and user is not None:
            print(f"refused: '{args.username}' already exists; use reset.")
            return 2
        if args.action == "reset" and user is None:
            print("no such machine account")
            return 1
        password = secrets.token_urlsafe(24)
        if user is None:
            user = User(username=args.username, password_hash=hash_password(password), role=MACHINE_ROLE)
        else:
            user.password_hash = hash_password(password)
        session.add(user)
        session.commit()
        print(f"machine account '{args.username}' {args.action}d. Password (shown once):\n{password}")
        return 0


if __name__ == "__main__":
    sys.exit(run())
