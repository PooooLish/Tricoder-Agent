"""Regression coverage for the tracked offline smoke eval suite."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from tricoder.evals.loader import load_suite
from tricoder.evals.output import reserve_run_directory
from tricoder.evals.runner import run_suite
from tricoder.models import RunResult


class EvalSmokeSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self._original_cwd = Path.cwd()
        os.chdir(self._temporary.name)

    def tearDown(self) -> None:
        os.chdir(self._original_cwd)
        self._temporary.cleanup()

    def test_builtin_smoke_suite_loads_three_cases(self) -> None:
        """Removing or reordering a tracked smoke case must be detected."""
        suite = load_suite(self._original_cwd / "evals" / "smoke")

        self.assertEqual(
            ("fix-subtract", "add-validation", "cross-file-feature"),
            tuple(case.id for case in suite.cases),
        )

    def test_builtin_smoke_verifiers_fail_before_agent_changes(self) -> None:
        """A no-op Agent must not receive a passing score for any smoke case."""
        suite = load_suite(self._original_cwd / "evals" / "smoke")
        run_dir = reserve_run_directory(Path.cwd(), "noop-agent")

        report = run_suite(
            suite,
            run_dir,
            "offline",
            "noop",
            lambda _case, _workspace, _audit: RunResult(
                True, "noop", 0, verification="通过"
            ),
        )

        self.assertEqual(
            ("fix-subtract", "add-validation", "cross-file-feature"),
            tuple(case.case_id for case in report.cases),
        )
        self.assertTrue(all(case.status != "passed" for case in report.cases))
        self.assertTrue(
            all(
                case.failure_codes == ("verification_failed", "required_change_missing")
                for case in report.cases
            )
        )


if __name__ == "__main__":
    unittest.main()
