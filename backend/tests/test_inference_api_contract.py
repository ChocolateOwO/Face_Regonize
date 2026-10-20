"""2026-10-20 additions, end to end over ASGI with the real application:
/api/health schema, PUT /api/people/{id}/voice-profile, title/language in
/api/look, detected_at with an offset in /api/history, migration 014.

Temporary database and storage; detection is faked (see test_robot_contract).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session

from app.models.models import Person
from test_robot_contract import ROBOT_PASSWORD, STATION_PASSWORD, ContractCase, jpeg, multipart

OFFSET = re.compile(r"[+-]\d{2}:\d{2}$")


class HealthContractTests(ContractCase):
    def test_health_needs_no_token_and_reports_the_lanes(self):
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        body, ctype = multipart({"photo": jpeg(), "source": "mice-secret-cam"})
        self.assertEqual(self.call("POST", "/api/look", robot, body, ctype)[0], 200)
        status, out = self.call("GET", "/api/health")
        self.assertEqual((status, out["status"]), (200, "ok"))
        for key in ("model_ready", "inference_mode", "device", "busy_with", "live_queue", "background_queue",
                    "look_avg_wait_ms", "inference"):
            self.assertIn(key, out)
        self.assertGreaterEqual(out["inference"]["look"]["requests_ok"], 1)
        self.assertNotIn("mice-secret-cam", json.dumps(out))


class VoiceProfileContractTests(ContractCase):
    def test_admin_sets_title_and_language_and_look_returns_them(self):
        station = self.login("station", STATION_PASSWORD)
        path = f"/api/people/{self.ann.id}/voice-profile"
        status, out = self.call("PUT", path, station, json.dumps({"title": "  ดร. ", "language": "TH"}).encode())
        self.assertEqual(status, 200, out)
        self.assertEqual((out["title"], out["language"], out["call_as"]), ("ดร.", "th", "พี่แอน"))

        # A field left out is unchanged; an empty one clears.
        status, out = self.call("PUT", path, station, json.dumps({"language": "en-us"}).encode())
        self.assertEqual((out["title"], out["language"]), ("ดร.", "en-US"))
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        body, ctype = multipart({"photo": jpeg(), "source": "mice-1"})
        status, look = self.call("POST", "/api/look", robot, body, ctype)
        matched = next(f for f in look["faces"] if f["status"] == "matched")
        self.assertEqual((matched["title"], matched["language"], matched["call_as"]), ("ดร.", "en-US", "พี่แอน"))
        unknown = next(f for f in look["faces"] if f["status"] == "unknown")
        self.assertEqual(set(unknown), {"box", "face_score", "status"})

        status, out = self.call("PUT", path, station, json.dumps({"title": "", "language": None}).encode())
        self.assertEqual((out["title"], out["language"]), (None, None))
        status, out = self.call("GET", f"/api/people/{self.ann.id}", station)
        self.assertEqual((out["title"], out["language"]), (None, None))

    def test_invalid_values_and_machine_are_refused_and_change_nothing(self):
        station = self.login("station", STATION_PASSWORD)
        path = f"/api/people/{self.ben.id}/voice-profile"
        self.assertEqual(self.call("PUT", path, station, json.dumps({"language": "Thai please"}).encode())[0], 422)
        self.assertEqual(self.call("PUT", path, station, json.dumps({"title": "x" * 41}).encode())[0], 422)
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        self.assertEqual(self.call("PUT", path, robot, json.dumps({"title": "Boss"}).encode())[0], 403)
        self.assertEqual(self.call("PUT", "/api/people/nobody/voice-profile", station, b"{}")[0], 404)
        with Session(self.engine) as s:
            ben = s.get(Person, self.ben.id)
            self.assertEqual((ben.title, ben.language), (None, None))


class LanguageComboBoxBackendTests(ContractCase):
    """2026-10-20 Language combobox: the existing endpoint and validation
    already take any well-formed tag, not only the listed ones."""

    def put(self, token, body):
        return self.call("PUT", f"/api/people/{self.ann.id}/voice-profile", token, json.dumps(body).encode())

    def test_listed_and_typed_codes_round_trip(self):
        station = self.login("station", STATION_PASSWORD)
        self.put(station, {"title": "ดร."})
        cases = {"th": "th", "en": "en", "fr": "fr", "zh": "zh", "ja": "ja", "ko": "ko",
                 "de": "de", "de-DE": "de-DE", "es": "es", "es-MX": "es-MX", "it": "it", "ru": "ru",
                 "pt-BR": "pt-BR", "ar": "ar", "vi": "vi", "lo": "lo", "th-TH": "th-TH", "en-US": "en-US",
                 "fr-FR": "fr-FR", "TH": "th", "EN": "en", "de-de": "de-DE", "pt_br": "pt-BR"}
        for sent, stored in cases.items():
            status, out = self.put(station, {"language": sent})
            self.assertEqual((status, out["language"]), (200, stored), sent)
            status, again = self.call("GET", f"/api/people/{self.ann.id}", station)  # reload
            self.assertEqual(again["language"], stored, sent)
            self.assertEqual((again["call_as"], again["title"]), ("พี่แอน", "ดร."), "call_as/title untouched")

    def test_invalid_text_is_422_and_nothing_changes(self):
        station = self.login("station", STATION_PASSWORD)
        self.put(station, {"language": "fr"})
        for bad in ("thai language", "hello world", "12345", "ไทย", "e", "en-"):
            status, out = self.put(station, {"language": bad})
            self.assertEqual(status, 422, bad)
            self.assertTrue(out["detail"].startswith("Language"), out)
        with Session(self.engine) as s:
            self.assertEqual(s.get(Person, self.ann.id).language, "fr")

    def test_clear_to_null(self):
        station = self.login("station", STATION_PASSWORD)
        self.put(station, {"language": "ja"})
        for empty in ("", None, "   "):
            self.put(station, {"language": "ja"})
            status, out = self.put(station, {"language": empty})
            self.assertEqual((status, out["language"]), (200, None), repr(empty))


class HistoryOffsetTests(ContractCase):
    def test_detected_at_carries_the_offset_and_filters_accept_it(self):
        robot = self.login("robot-hub", ROBOT_PASSWORD)
        status, out = self.call("GET", "/api/history?page_size=100", robot)
        self.assertEqual(status, 200)
        old = next(i for i in out["items"] if i["id"] == "old-row")
        self.assertRegex(old["detected_at"], OFFSET)
        stored = datetime(2026, 9, 1)
        self.assertEqual(datetime.fromisoformat(old["detected_at"]), stored.astimezone(), "same instant as stored")
        # Bounds built from the returned value work with its offset, in UTC, or naive (local).
        seen = datetime.fromisoformat(old["detected_at"])
        for lo, hi in ((seen - timedelta(seconds=1), seen + timedelta(seconds=1)),
                       ((seen - timedelta(seconds=1)).astimezone(timezone.utc), (seen + timedelta(seconds=1)).astimezone(timezone.utc)),
                       (datetime(2026, 8, 31, 23, 59, 59), datetime(2026, 9, 1, 0, 0, 1))):
            q = lambda d: d.isoformat().replace("+", "%2B")  # noqa: E731
            status, got = self.call("GET", f"/api/history?page_size=100&date_from={q(lo)}&date_to={q(hi)}", robot)
            self.assertEqual([i["id"] for i in got["items"]], ["old-row"], (lo, hi))
        status, dash = self.call("GET", "/api/reports/dashboard", robot)
        self.assertEqual(status, 200)


class Migration014Tests(unittest.TestCase):
    def test_adds_nullable_columns_idempotently(self):
        import importlib

        mod = importlib.import_module("app.migrations.014_person_title_language")
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(str(Path(d) / "m.db"))
            try:
                conn.execute("CREATE TABLE person (id TEXT PRIMARY KEY, call_as VARCHAR)")
                conn.execute("INSERT INTO person (id, call_as) VALUES ('p1', 'Boss')")
                mod.upgrade(conn)
                mod.upgrade(conn)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(person)")}
                self.assertTrue({"title", "language"} <= cols)
                self.assertEqual(conn.execute("SELECT call_as, title, language FROM person").fetchall(), [("Boss", None, None)])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
