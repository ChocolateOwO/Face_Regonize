"""Fresh-install tools (2026-11-26): scripts/init_env.py creates .env with
fresh secrets and never overwrites one; backend/scripts/set_admin_password.py
changes an existing admin password (the only supported way after first start)."""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.auth.security import hash_password, verify_password  # noqa: E402
from app.models.models import User  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))
import init_env  # noqa: E402
import set_admin_password  # noqa: E402


class InitEnvTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="reconize-env-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copy2(ROOT / ".env.example", self.root / ".env.example")

    def call(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = init_env.main(["--root", str(self.root), *args])
        return rc, buf.getvalue()

    def test_creates_env_with_fresh_secret_and_password(self):
        rc, out = self.call()
        self.assertEqual(rc, 0)
        values = init_env.read_env(self.root / ".env")
        self.assertNotEqual(values["JWT_SECRET"], init_env.DEFAULTS["JWT_SECRET"])
        self.assertGreaterEqual(len(values["JWT_SECRET"]), 48)
        self.assertNotEqual(values["ADMIN_PASSWORD"], "admin123")
        self.assertIn(values["ADMIN_PASSWORD"], out, "the generated password is shown once")
        self.assertNotIn(values["JWT_SECRET"], out, "the signing secret is never printed")
        # every other example setting is kept
        example = init_env.read_env(self.root / ".env.example")
        for key in set(example) - {"JWT_SECRET", "ADMIN_PASSWORD", "GITHUB_REPO"}:
            self.assertEqual(values[key], example[key], key)
        self.assertEqual(self.call("--check")[0], 0)

    def test_two_installs_get_different_secrets(self):
        self.call()
        first = init_env.read_env(self.root / ".env")["JWT_SECRET"]
        (self.root / ".env").unlink()
        self.call()
        self.assertNotEqual(first, init_env.read_env(self.root / ".env")["JWT_SECRET"])

    def test_existing_env_is_never_overwritten(self):
        (self.root / ".env").write_text("JWT_SECRET=mine\n", encoding="utf-8")
        rc, out = self.call()
        self.assertEqual(rc, 0)
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), "JWT_SECRET=mine\n")
        self.assertIn("left unchanged", out)

    def test_check_flags_public_defaults(self):
        shutil.copy2(self.root / ".env.example", self.root / ".env")
        rc, out = self.call("--check")
        self.assertEqual(rc, 1)
        self.assertIn("JWT_SECRET: PUBLIC DEFAULT", out)
        self.assertIn("ADMIN_PASSWORD: PUBLIC DEFAULT", out)

    def test_github_repo_comes_from_an_https_origin(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin",
                        "https://github.com/Owner/Reconize-App.git"], check=True)
        saved = init_env.ROOT
        init_env.ROOT = self.root
        try:
            self.call()
        finally:
            init_env.ROOT = saved
        self.assertEqual(init_env.read_env(self.root / ".env")["GITHUB_REPO"], "Owner/Reconize-App")


class SetAdminPasswordTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            s.add(User(username="admin", password_hash=hash_password("admin123"), role="admin"))
            s.add(User(username="robot-hub", password_hash=hash_password("x"), role="machine"))
            s.commit()

    def hash_of(self, name):
        with Session(self.engine) as s:
            return s.exec(select(User).where(User.username == name)).first().password_hash

    def call(self, argv, answers=()):
        it = iter(answers)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = set_admin_password.run(argv, engine=self.engine, ask=lambda prompt: next(it))
        return rc, buf.getvalue()

    def test_prompted_password_replaces_the_default(self):
        rc, out = self.call([], ["a-strong-passphrase", "a-strong-passphrase"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(verify_password("a-strong-passphrase", self.hash_of("admin")))
        self.assertFalse(verify_password("admin123", self.hash_of("admin")))
        self.assertNotIn("a-strong-passphrase", out)

    def test_generated_password_is_printed_once_and_works(self):
        rc, out = self.call(["--generate"])
        self.assertEqual(rc, 0)
        password = out.strip().splitlines()[-1]
        self.assertTrue(verify_password(password, self.hash_of("admin")))

    def test_mismatch_and_weak_passwords_change_nothing(self):
        before = self.hash_of("admin")
        self.assertEqual(self.call([], ["a-strong-passphrase", "another-passphrase"])[0], 1)
        self.assertEqual(self.call([], ["admin123", "admin123"])[0], 1)
        self.assertEqual(self.call([], ["short", "short"])[0], 1)
        self.assertEqual(self.hash_of("admin"), before)

    def test_machine_account_is_refused(self):
        before = self.hash_of("robot-hub")
        rc, out = self.call(["--username", "robot-hub", "--generate"])
        self.assertEqual(rc, 2)
        self.assertIn("not an admin", out)
        self.assertEqual(self.hash_of("robot-hub"), before)

    def test_unknown_account(self):
        self.assertEqual(self.call(["--username", "nobody", "--generate"])[0], 2)


if __name__ == "__main__":
    unittest.main()
