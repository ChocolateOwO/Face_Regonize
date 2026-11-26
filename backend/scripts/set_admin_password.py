"""Change the password of an existing admin account.

ADMIN_PASSWORD in .env only creates the admin on the very first start (empty
database); after that it is ignored. This is the supported way to change it.

Run from backend/ against the database the backend uses (DATABASE_URL in
.env, same as the server). The backend may keep running; existing sign-ins
stay valid until they expire (rotate JWT_SECRET to end them all).

    python scripts/set_admin_password.py                     # prompts twice, hidden
    python scripts/set_admin_password.py --generate          # random password, printed ONCE
    python scripts/set_admin_password.py --username alice    # another admin account

Only accounts whose role is "admin" are changed: machine accounts have their
own tool (manage_machine_user.py). Only the bcrypt hash is stored.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
# The server runs from backend/, and .env's DATABASE_URL is usually relative
# (./database/app.db): resolve it from the same folder, wherever this is run.
os.chdir(BACKEND)

from sqlmodel import Session, select  # noqa: E402

from app.auth.security import hash_password  # noqa: E402
from app.models.models import User  # noqa: E402

MIN_LENGTH = 10
WEAK = {"admin123", "password", "admin", "12345678", "1234567890"}


def run(argv: list[str] | None = None, engine=None, ask=getpass.getpass) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--username", default="admin")
    ap.add_argument("--generate", action="store_true", help="generate a random password and print it once")
    args = ap.parse_args(argv)
    if engine is None:
        from app.config import DATABASE_PATH
        from app.database.db import engine, init_db

        init_db()
        print(f"database: {DATABASE_PATH}")
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == args.username)).first()
        if not user:
            print(f"no account named '{args.username}'")
            return 2
        if user.role != "admin":
            print(f"refused: '{args.username}' is not an admin account (role={user.role}).")
            return 2
        if args.generate:
            password = secrets.token_urlsafe(12)
        else:
            password = ask("New password: ")
            if password != ask("Repeat it:    "):
                print("The two entries differ; nothing changed.")
                return 1
            if len(password) < MIN_LENGTH or password.lower() in WEAK:
                print(f"Too weak: use at least {MIN_LENGTH} characters and not a common default; nothing changed.")
                return 1
        user.password_hash = hash_password(password)
        session.add(user)
        session.commit()
    if args.generate:
        print(f"password for '{args.username}' changed. New password (shown once):\n{password}")
    else:
        print(f"password for '{args.username}' changed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
