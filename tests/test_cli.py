"""Acceptance tests for the CLI, including atomic output replacement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VALID_PLAN = {
    "tz": "UTC",
    "start": "1970-01-01T00:00:00",
    "rule": "FREQ=DAILY;COUNT=2",
    "overrides": {},
    "window": [0, 86401],
}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plan_path = os.path.join(self.tmp, "plan.json")
        self.out_path = os.path.join(self.tmp, "out.json")
        with open(self.plan_path, "w", encoding="utf-8") as handle:
            json.dump(VALID_PLAN, handle)

    def _run(self, *args):
        env = dict(os.environ)
        # Keep the environment clean; main.py bootstraps sys.path itself,
        # so the package resolves even in isolated mode without PYTHONPATH.
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, "-I", os.path.join(ROOT, "main.py"), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_stdout_output(self):
        proc = self._run(self.plan_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual([o["utc"] for o in data], [0, 86400])

    def test_atomic_replace_on_success(self):
        with open(self.out_path, "w", encoding="utf-8") as handle:
            handle.write("PREVIOUS CONTENT")
        proc = self._run(self.plan_path, "-o", self.out_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.out_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(len(data), 2)

    def test_failure_keeps_old_output_file(self):
        with open(self.out_path, "w", encoding="utf-8") as handle:
            handle.write("PREVIOUS CONTENT")
        bad_plan = os.path.join(self.tmp, "bad.json")
        with open(bad_plan, "wb") as handle:
            handle.write(b'{"tz": "UTC", "start":, }')
        proc = self._run(bad_plan, "-o", self.out_path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("syntax error", proc.stderr)
        with open(self.out_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "PREVIOUS CONTENT")

    def test_validation_failure_keeps_old_output_file(self):
        with open(self.out_path, "w", encoding="utf-8") as handle:
            handle.write("PREVIOUS CONTENT")
        bad_plan = os.path.join(self.tmp, "bad.json")
        with open(bad_plan, "w", encoding="utf-8") as handle:
            json.dump({
                "tz": "UTC",
                "start": "1970-01-01T00:00:00",
                "rule": "FREQ=DAILY;COUNT=1",
                "overrides": {},
                "window": [True, 5],
            }, handle)
        proc = self._run(bad_plan, "-o", self.out_path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("$.window[0]", proc.stderr)
        with open(self.out_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "PREVIOUS CONTENT")

    def test_stdin_input(self):
        proc = subprocess.run(
            [sys.executable, "-I", os.path.join(ROOT, "main.py"), "-"],
            cwd=ROOT,
            input=json.dumps(VALID_PLAN),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(json.loads(proc.stdout)), 2)

    def test_missing_input_file(self):
        proc = self._run(os.path.join(self.tmp, "nope.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot read input", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
