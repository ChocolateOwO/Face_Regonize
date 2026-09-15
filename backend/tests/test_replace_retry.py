"""Phase D1 hardening — replacing a live output file on Windows.

A thumbnail/MEDIA replace intermittently failed with WinError 5 while another
handle held the destination (measured: 9 of 12 runs of the concurrent-writer
test). These tests pin the bounded retry and that it never masks a real error.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import fs_util


class ReplaceWithRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reconize-replace-")
        self.addCleanup(self.temp.cleanup)
        self.src = Path(self.temp.name) / "src.tmp"
        self.dst = Path(self.temp.name) / "dst.jpg"
        self.src.write_bytes(b"new")
        self.dst.write_bytes(b"old")
        self.real = fs_util.os.replace

    def test_a_transient_permission_error_is_retried_until_it_succeeds(self):
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            self.real(src, dst)

        with patch.object(fs_util.os, "replace", side_effect=flaky), patch.object(fs_util.time, "sleep"):
            fs_util.replace_with_retry(self.src, self.dst)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.dst.read_bytes(), b"new")

    def test_a_persistent_permission_error_is_raised_after_a_bounded_number_of_tries(self):
        with patch.object(fs_util.os, "replace", side_effect=PermissionError(5, "denied")) as rep, \
                patch.object(fs_util.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                fs_util.replace_with_retry(self.src, self.dst)
        self.assertEqual(rep.call_count, fs_util.REPLACE_ATTEMPTS)
        self.assertLess(sum(c.args[0] for c in sleep.call_args_list), 1.5, "a stuck file must fail fast")
        self.assertEqual(self.dst.read_bytes(), b"old")

    def test_other_errors_are_not_retried(self):
        with patch.object(fs_util.os, "replace", side_effect=FileNotFoundError(2, "gone")) as rep:
            with self.assertRaises(FileNotFoundError):
                fs_util.replace_with_retry(self.src, self.dst)
        self.assertEqual(rep.call_count, 1)

    def test_both_live_file_writers_use_it(self):
        import inspect

        from app.services import photo_regeneration_service, photo_thumbnail_service

        self.assertIn("replace_with_retry(", inspect.getsource(photo_thumbnail_service._atomic_write))
        self.assertIn("replace_with_retry(", inspect.getsource(photo_regeneration_service._atomic_replace))


if __name__ == "__main__":
    unittest.main()
