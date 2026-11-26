"""Create or check Reconize's .env (run by scripts/setup-windows.ps1).

    python scripts/init_env.py            create .env if it does not exist
    python scripts/init_env.py --check    report (yes/no) whether defaults are in use

Creating: copies .env.example to .env and replaces the public defaults with
fresh values - a random JWT_SECRET (64 characters) and a random ADMIN_PASSWORD
(printed ONCE here, never stored anywhere else in plain text after the first
backend start hashes it) - and fills GITHUB_REPO from the git 'origin' URL so
web updates work. An existing .env is never overwritten.

Important: ADMIN_PASSWORD only creates the admin account on the FIRST backend
start (empty database). To change it later use
backend/scripts/set_admin_password.py.
"""
from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {"JWT_SECRET": "dev-secret-change-me-in-production", "ADMIN_PASSWORD": "admin123"}


def origin_repo() -> str:
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(ROOT), capture_output=True,
                             text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.match(r"^https://(?:[^@/]+@)?github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", url, re.I)
    return m.group(1) if m else ""


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def check(env: Path) -> int:
    if not env.is_file():
        print(".env: missing - run scripts\\setup-windows.ps1 (or python scripts\\init_env.py)")
        return 1
    values = read_env(env)
    bad = []
    for key, default in DEFAULTS.items():
        state = "not set (public default used)" if not values.get(key) else (
            "PUBLIC DEFAULT" if values[key] == default else "custom")
        print(f"{key}: {state}")
        if state != "custom":
            bad.append(key)
    print(f"GITHUB_REPO: {'set' if values.get('GITHUB_REPO') else 'not set (web updates disabled)'}")
    if bad:
        print("Note: ADMIN_PASSWORD in .env only matters before the first start; use "
              "backend\\scripts\\set_admin_password.py to change an existing admin password.")
    return 1 if "JWT_SECRET" in bad else 0


def create(env: Path, example: Path) -> int:
    if env.exists():
        print(".env already exists - left unchanged. (python scripts\\init_env.py --check reports defaults.)")
        return 0
    if not example.is_file():
        print(f"{example.name} is missing from this checkout; cannot create .env.", file=sys.stderr)
        return 2
    secret = secrets.token_urlsafe(48)
    password = secrets.token_urlsafe(12)
    repo = origin_repo()
    lines = []
    for line in example.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key == "JWT_SECRET":
            line = f"JWT_SECRET={secret}"
        elif key == "ADMIN_PASSWORD":
            line = f"ADMIN_PASSWORD={password}"
        elif key == "GITHUB_REPO" and repo:
            line = f"GITHUB_REPO={repo}"
        lines.append(line)
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Created .env with a new JWT_SECRET and a new admin password.")
    print("")
    print("  Admin login for the FIRST start:  username admin")
    print(f"                                   password {password}")
    print("")
    print("  Write this password down now - it is shown only once. Change it any time with")
    print("  cd backend   then   .venv\\Scripts\\python.exe scripts\\set_admin_password.py")
    print(f"GITHUB_REPO: {repo or 'not set (origin is not an HTTPS GitHub URL) - web updates stay off until you set it'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only report whether public defaults are in use")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    root = Path(a.root)
    if a.check:
        return check(root / ".env")
    return create(root / ".env", root / ".env.example")


if __name__ == "__main__":
    sys.exit(main())
