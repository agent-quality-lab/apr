"""Regression tests for scripts/smoke_local_alpha.py cleanup reporting (no network)."""
import importlib.util
import io
import re
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_local_alpha.py"
spec = importlib.util.spec_from_file_location("smoke_local_alpha", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def fake_stop(returncode):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout="stop output", stderr="")
    return run


class SmokeCleanupTests(unittest.TestCase):
    def test_final_stop_failure_reports_failure_and_retains_data(self):
        out = io.StringIO()
        with mock.patch.object(smoke, "run"), \
                mock.patch.object(smoke.subprocess, "run", side_effect=fake_stop(1)), \
                redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            smoke.main()
        self.assertNotEqual(raised.exception.code, 0)
        self.assertNotIn("PASS", out.getvalue())
        match = re.search(r"retained (\S+)", out.getvalue())
        self.assertIsNotNone(match, out.getvalue())
        tmp = Path(match.group(1))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertTrue(tmp.is_dir())
        self.assertIn("APR_DATA_DIR=", out.getvalue())

    def test_final_stop_success_passes_and_removes_data(self):
        out = io.StringIO()
        created = []
        real_mkdtemp = smoke.tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            created.append(real_mkdtemp(*args, **kwargs))
            return created[-1]

        with mock.patch.object(smoke, "run"), \
                mock.patch.object(smoke.subprocess, "run", side_effect=fake_stop(0)), \
                mock.patch.object(smoke.tempfile, "mkdtemp", side_effect=mkdtemp), \
                redirect_stdout(out):
            smoke.main()
        self.assertIn("PASS", out.getvalue())
        self.assertEqual(len(created), 1)
        self.assertFalse(Path(created[0]).exists())


if __name__ == "__main__":
    unittest.main()
