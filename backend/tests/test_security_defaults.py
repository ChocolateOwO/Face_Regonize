"""Public default credentials are reported (log, admin-only endpoint), never
blocking startup, and never by value."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.api import system  # noqa: E402
from app.auth import deps  # noqa: E402
from app.auth.security import hash_password  # noqa: E402
from app.models.models import User  # noqa: E402
from app.services import security_defaults  # noqa: E402

CUSTOM_SECRET = "x" * 64


class SecurityDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

    def add(self, username, password, role="admin"):
        with Session(self.engine) as s:
            user = User(username=username, password_hash=hash_password(password), role=role)
            s.add(user)
            s.commit()
            s.refresh(user)
            return user

    def check(self, secret=CUSTOM_SECRET):
        with Session(self.engine) as s:
            return security_defaults.check(s, jwt_secret=secret)

    def test_default_admin_password_and_secret_are_flagged(self):
        self.add("admin", "admin123")
        r = self.check(security_defaults.DEFAULT_JWT_SECRET)
        self.assertEqual((r["jwt_secret_default"], r["admin_password_default"], r["any"]), (True, True, True))

    def test_rotated_install_is_clean(self):
        self.add("admin", "a-new-long-password")
        r = self.check()
        self.assertEqual((r["jwt_secret_default"], r["admin_password_default"], r["any"]), (False, False, False))

    def test_empty_secret_counts_as_default(self):
        self.assertTrue(self.check("")["jwt_secret_default"])

    def test_machine_account_with_that_password_is_not_an_admin_default(self):
        self.add("admin", "a-new-long-password")
        self.add("robot-hub", "admin123", role="machine")
        self.assertFalse(self.check()["admin_password_default"])

    def test_result_never_contains_a_secret_or_password(self):
        self.add("admin", "admin123")
        r = self.check(security_defaults.DEFAULT_JWT_SECRET)
        text = repr(r)
        self.assertNotIn("admin123", text)
        self.assertNotIn(security_defaults.DEFAULT_JWT_SECRET, text)
        self.assertEqual(set(r), {"jwt_secret_default", "admin_password_default", "any"})

    def test_startup_warning_names_the_fix_not_the_value(self):
        self.add("admin", "admin123")
        with Session(self.engine) as s, self.assertLogs("app.services.security_defaults", "WARNING") as logs:
            security_defaults.warn_at_startup(s)
        text = "\n".join(logs.output)
        self.assertIn("set_admin_password.py", text)
        self.assertNotIn("admin123", text)

    def test_fix_steps_match_what_was_found(self):
        steps = security_defaults.fix_steps({"any": True, "jwt_secret_default": True, "admin_password_default": False})
        self.assertTrue(any("JWT_SECRET" in x for x in steps))
        self.assertFalse(any("set_admin_password" in x for x in steps))
        self.assertEqual(security_defaults.fix_steps({"any": False}), [])

    def test_endpoint_is_admin_only(self):
        admin = self.add("admin", "admin123")
        station = self.add("station", "station-password", role="station")
        machine = self.add("robot-hub", "machine-password", role="machine")
        from unittest.mock import patch

        import app.config

        with Session(self.engine) as s, patch.object(app.config, "JWT_SECRET", CUSTOM_SECRET):
            out = system.security_status(session=s, user=deps.require_admin(user=admin))
            self.assertTrue(out["admin_password_default"])
            self.assertTrue(any("set_admin_password.py" in step for step in out["fix"]))
            self.assertFalse(any("JWT_SECRET" in step for step in out["fix"]), "the secret is custom here: no secret step")
            with self.assertRaises(HTTPException) as ctx:
                deps.require_admin(user=station)
            self.assertEqual(ctx.exception.status_code, 403)
            with self.assertRaises(HTTPException):
                deps.require_admin(user=machine)


if __name__ == "__main__":
    unittest.main()
