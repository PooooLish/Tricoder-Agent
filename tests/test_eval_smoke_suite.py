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
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry


_CORRECT_SOLUTIONS = {
    "fix-subtract": {
        "calculator.py": (
            "def subtract(a: int, b: int) -> int:\n"
            "    return a - b\n"
        ),
    },
    "add-validation": {
        "usernames.py": (
            "def normalize_username(value: str) -> str:\n"
            "    if not isinstance(value, str):\n"
            "        raise TypeError('value must be a string')\n"
            "    normalized = value.strip().lower()\n"
            "    if not normalized:\n"
            "        raise ValueError('value must not be blank')\n"
            "    return normalized\n"
        ),
    },
    "cross-file-feature": {
        "discounts.py": (
            "def percentage_discount(amount: int, percent: int) -> int:\n"
            "    if amount < 0 or percent < 0 or percent > 100:\n"
            "        raise ValueError('invalid discount bounds')\n"
            "    return amount * (100 - percent) // 100\n"
        ),
        "pricing.py": (
            "from discounts import percentage_discount\n\n"
            "def final_price(amount: int, percent: int = 0) -> int:\n"
            "    return percentage_discount(amount, percent)\n"
        ),
    },
}


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

    def test_correct_solutions_pass_public_tests_and_hidden_verifiers(self) -> None:
        """Public test execution must not create scored cache files."""
        suite = load_suite(self._original_cwd / "evals" / "smoke")
        run_dir = reserve_run_directory(Path.cwd(), "correct-solutions")

        def executor(case, workspace, audit_path):  # type: ignore[no-untyped-def]
            del audit_path
            for relative, content in _CORRECT_SOLUTIONS[case.id].items():
                (workspace / relative).write_text(content, encoding="utf-8")
            tools = ToolRegistry(
                ToolContext(
                    workspace_policy=WorkspacePolicy(workspace),
                    command_policy=CommandPolicy(workspace),
                    approver=lambda _action, _detail: True,
                )
            )
            public_tests = tools.execute(
                "run_command",
                {"command": "python -m unittest discover -s tests -q"},
            )
            return RunResult(
                public_tests.ok,
                "offline",
                1,
                tool_calls=1,
                modified_files=tuple(_CORRECT_SOLUTIONS[case.id]),
                verification="通过" if public_tests.ok else "失败",
            )

        report = run_suite(suite, run_dir, "offline", "correct", executor)

        self.assertEqual(
            ("passed", "passed", "passed"),
            tuple(case.status for case in report.cases),
            report.cases,
        )

    def test_hidden_verifier_rejects_business_module_assertion_monkeypatch(self) -> None:
        """A tested module must not share the verifier's assertion process."""
        suite = load_suite(
            self._original_cwd / "evals" / "smoke",
            case_id="fix-subtract",
        )
        run_dir = reserve_run_directory(Path.cwd(), "malicious-assertion-patch")

        def executor(case, workspace, audit_path):  # type: ignore[no-untyped-def]
            del case, audit_path
            (workspace / "calculator.py").write_text(
                "import unittest\n"
                "unittest.TestCase.assertEqual = lambda *args, **kwargs: None\n\n"
                "def subtract(a: int, b: int) -> int:\n"
                "    return 999\n",
                encoding="utf-8",
            )
            tools = ToolRegistry(
                ToolContext(
                    workspace_policy=WorkspacePolicy(workspace),
                    command_policy=CommandPolicy(workspace),
                    approver=lambda _action, _detail: True,
                )
            )
            public_tests = tools.execute(
                "run_command",
                {"command": "python -m unittest discover -s tests -q"},
            )
            return RunResult(
                public_tests.ok,
                "offline",
                1,
                tool_calls=1,
                modified_files=("calculator.py",),
                verification="通过" if public_tests.ok else "失败",
            )

        result = run_suite(suite, run_dir, "offline", "malicious", executor).cases[0]

        self.assertEqual("failed", result.status, result)
        self.assertIn("verification_failed", result.failure_codes)

    def test_hidden_probe_bounds_stdout_and_stderr_before_marker(self) -> None:
        """Probe capture must cap both streams and kill the flooding process."""
        suite = load_suite(
            self._original_cwd / "evals" / "smoke",
            case_id="fix-subtract",
        )
        for stream_name in ("stdout", "stderr"):
            with self.subTest(stream=stream_name):
                run_dir = reserve_run_directory(
                    Path.cwd(),
                    f"malicious-{stream_name}-flood",
                )
                marker_name = f"probe-{stream_name}-after-flood.txt"

                def executor(case, workspace, audit_path):  # type: ignore[no-untyped-def]
                    del case, audit_path
                    (workspace / "calculator.py").write_text(
                        "from pathlib import Path\n"
                        "import sys\n"
                        f"sys.{stream_name}.write('x' * 5_000_000)\n"
                        f"sys.{stream_name}.flush()\n"
                        f"Path({marker_name!r}).write_text('alive')\n\n"
                        "def subtract(a: int, b: int) -> int:\n"
                        "    return a - b\n",
                        encoding="utf-8",
                    )
                    return RunResult(True, "offline", 1, verification="通过")

                result = run_suite(
                    suite,
                    run_dir,
                    "offline",
                    f"malicious-{stream_name}",
                    executor,
                ).cases[0]
                marker = run_dir / "workspaces" / "fix-subtract" / marker_name

                self.assertEqual("failed", result.status, result)
                self.assertIn("verification_failed", result.failure_codes)
                self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
