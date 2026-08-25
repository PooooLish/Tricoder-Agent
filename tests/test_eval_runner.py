"""Tests for deterministic eval execution and scoring."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tricoder.evals.models import EvalCase, EvalSuite, VerificationSpec
from tricoder.evals.output import EvalOutputError
from tricoder.evals.runner import run_suite
from tricoder.evals.workspace import RESERVED_VERIFIER_DIR
from tricoder.models import RunResult, TokenUsage


class EvalRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_cwd = Path.cwd()
        os.chdir(self.root)
        self.run_dir = self.root / "runtime" / "evals" / "run-001"
        self.run_dir.mkdir(parents=True)
        self.case = self._make_case("case-one")
        self.suite = EvalSuite(
            id="smoke",
            title="Smoke",
            source_dir=self.root / "suite",
            cases=(self.case,),
        )

    def tearDown(self) -> None:
        os.chdir(self.original_cwd)
        self.temporary.cleanup()

    def _make_case(
        self,
        case_id: str,
        *,
        allowed_changes: tuple[str, ...] = ("app.py",),
        required_changes: tuple[str, ...] = ("app.py",),
        command: str | None = None,
        timeout: float = 5.0,
        verifier_source: str | None = None,
    ) -> EvalCase:
        case_dir = self.root / "suite" / "cases" / case_id
        workspace = case_dir / "workspace"
        verifier = case_dir / "verifier"
        workspace.mkdir(parents=True)
        verifier.mkdir()
        (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        (verifier / "check.py").write_text(
            verifier_source
            or "from pathlib import Path\n"
            "raise SystemExit(0 if 'value = 2' in "
            "Path('app.py').read_text(encoding='utf-8') else 1)\n",
            encoding="utf-8",
        )
        return EvalCase(
            id=case_id,
            title=case_id,
            task="change the value",
            source_dir=case_dir,
            workspace_dir=workspace,
            verifier_dir=verifier,
            allowed_changes=allowed_changes,
            required_changes=required_changes,
            max_rounds=8,
            max_context_chars=40_000,
            verifications=(
                VerificationSpec(
                    "hidden",
                    command
                    or f"python {RESERVED_VERIFIER_DIR}/check.py",
                    timeout,
                ),
            ),
        )

    @staticmethod
    def _passing_executor(
        case: EvalCase, workspace: Path, audit_path: Path
    ) -> RunResult:
        del case, audit_path
        (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
        return RunResult(True, "done", 2, 1, ("app.py",), "通过")

    def test_run_suite_hides_verifier_until_agent_returns(self) -> None:
        seen: list[bool] = []
        audit_paths: list[Path] = []

        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            del case
            seen.append((workspace / RESERVED_VERIFIER_DIR).exists())
            audit_paths.append(audit_path)
            (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            return RunResult(True, "done", 2, 1, ("app.py",), "通过")

        report = run_suite(
            self.suite, self.run_dir, "openai", "test-model", executor
        )

        self.assertEqual([False], seen)
        self.assertEqual("passed", report.cases[0].status)
        self.assertEqual(("app.py",), report.cases[0].modified_files)
        self.assertEqual("通过", report.cases[0].agent_verification)
        self.assertEqual("hidden", report.cases[0].verifications[0].name)
        self.assertTrue(report.cases[0].verifications[0].passed)
        self.assertTrue(audit_paths[0].is_relative_to(self.run_dir))
        self.assertFalse(
            (
                self.run_dir
                / "workspaces"
                / self.case.id
                / RESERVED_VERIFIER_DIR
            ).exists()
        )

    def test_run_suite_rejects_nonempty_run_dir_before_creating_state(self) -> None:
        """Reusing a prior run directory must not create audit/workspace children."""
        marker = self.run_dir / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")
        executor_calls: list[str] = []

        with self.assertRaises(EvalOutputError):
            run_suite(
                self.suite,
                self.run_dir,
                "openai",
                "test-model",
                lambda *_args: executor_calls.append("called"),  # type: ignore[arg-type]
            )

        self.assertEqual([], executor_calls)
        self.assertEqual("preserve", marker.read_text(encoding="utf-8"))
        self.assertFalse((self.run_dir / "workspaces").exists())
        self.assertFalse((self.run_dir / "audit").exists())

    def test_agent_failure_has_fixed_failure_code(self) -> None:
        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            del case, audit_path
            (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            return RunResult(False, "provider detail", 3, 2, (), "通过")

        result = run_suite(
            self.suite, self.run_dir, "openai", "test-model", executor
        ).cases[0]

        self.assertEqual("failed", result.status)
        self.assertEqual(("agent_failed",), result.failure_codes)

    def test_agent_must_report_verified(self) -> None:
        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            del case, audit_path
            (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            return RunResult(True, "done", 1, 1, (), "未运行")

        result = run_suite(
            self.suite, self.run_dir, "openai", "test-model", executor
        ).cases[0]

        self.assertEqual("failed", result.status)
        self.assertEqual(("agent_unverified",), result.failure_codes)

    def test_nonzero_hidden_verifier_is_a_normal_failure_and_is_cleaned_up(self) -> None:
        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            del case, audit_path
            (workspace / "app.py").write_text("value = 3\n", encoding="utf-8")
            return RunResult(True, "done", 1, 1, (), "通过")

        result = run_suite(
            self.suite, self.run_dir, "openai", "test-model", executor
        ).cases[0]

        self.assertEqual("failed", result.status)
        self.assertEqual(("verification_failed",), result.failure_codes)
        self.assertEqual(1, result.verifications[0].exit_code)
        self.assertFalse(result.verifications[0].passed)
        self.assertIsNone(result.verifications[0].error_code)
        self.assertFalse(
            (self.run_dir / "workspaces" / self.case.id / RESERVED_VERIFIER_DIR).exists()
        )

    def test_change_outside_allowed_patterns_fails(self) -> None:
        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            del case, audit_path
            (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            (workspace / "extra.txt").write_text("extra\n", encoding="utf-8")
            return RunResult(True, "done", 1, 1, (), "通过")

        result = run_suite(
            self.suite, self.run_dir, "openai", "test-model", executor
        ).cases[0]

        self.assertEqual("failed", result.status)
        self.assertEqual(("change_out_of_scope",), result.failure_codes)
        self.assertEqual(("app.py", "extra.txt"), result.modified_files)

    def test_broad_workspace_pattern_allows_nested_normal_changes(self) -> None:
        case = replace(
            self.case,
            allowed_changes=("**",),
            required_changes=("nested/result.txt",),
        )
        suite = replace(self.suite, cases=(case,))

        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            del case, audit_path
            nested = workspace / "nested"
            nested.mkdir()
            (nested / "result.txt").write_text("done\n", encoding="utf-8")
            (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            return RunResult(True, "done", 1, 1, (), "通过")

        result = run_suite(
            suite, self.run_dir, "openai", "test-model", executor
        ).cases[0]

        self.assertEqual("passed", result.status)
        self.assertEqual(("app.py", "nested/result.txt"), result.modified_files)

    def test_missing_required_pattern_fails(self) -> None:
        case = replace(
            self.case,
            allowed_changes=("**",),
            required_changes=("required.py",),
        )
        suite = replace(self.suite, cases=(case,))

        result = run_suite(
            suite, self.run_dir, "openai", "test-model", self._passing_executor
        ).cases[0]

        self.assertEqual("failed", result.status)
        self.assertEqual(("required_change_missing",), result.failure_codes)

    def test_executor_error_is_fixed_and_does_not_stop_later_cases(self) -> None:
        second = self._make_case("case-two")
        suite = replace(self.suite, cases=(self.case, second))

        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            if case.id == "case-one":
                raise RuntimeError("PROVIDER-SECRET-SENTINEL")
            return self._passing_executor(case, workspace, audit_path)

        report = run_suite(suite, self.run_dir, "openai", "test-model", executor)

        self.assertEqual(("error", "passed"), tuple(c.status for c in report.cases))
        self.assertEqual(("executor_error",), report.cases[0].failure_codes)
        self.assertEqual(0, report.cases[0].rounds)
        self.assertEqual("未运行", report.cases[0].agent_verification)

    def test_unknown_token_usage_remains_none(self) -> None:
        report = run_suite(
            self.suite,
            self.run_dir,
            "openai",
            "test-model",
            self._passing_executor,
        )

        self.assertIsNone(report.cases[0].usage)
        self.assertIsNone(report.usage)

    def test_suite_token_usage_merges_all_known_case_fields(self) -> None:
        second = self._make_case("case-two")
        suite = replace(self.suite, cases=(self.case, second))

        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            result = self._passing_executor(case, workspace, audit_path)
            usage = (
                TokenUsage(input_tokens=2, cached_tokens=1)
                if case.id == "case-one"
                else TokenUsage(input_tokens=3, output_tokens=7)
            )
            return replace(result, usage=usage)

        report = run_suite(suite, self.run_dir, "openai", "test-model", executor)

        self.assertEqual(
            TokenUsage(input_tokens=5, output_tokens=7, cached_tokens=1),
            report.usage,
        )
        self.assertIsNone(report.usage.cache_miss_tokens)  # type: ignore[union-attr]

    def test_verification_timeout_has_fixed_error_mapping(self) -> None:
        case = self._make_case(
            "timeout-case",
            timeout=0.01,
            verifier_source="import time\ntime.sleep(1)\n",
        )
        suite = replace(self.suite, cases=(case,))

        result = run_suite(
            suite, self.run_dir, "openai", "test-model", self._passing_executor
        ).cases[0]

        self.assertEqual("error", result.status)
        self.assertEqual(("verification_error",), result.failure_codes)
        self.assertEqual("verification_timeout", result.verifications[0].error_code)
        self.assertIsNone(result.verifications[0].exit_code)

    def test_verification_policy_rejection_has_fixed_error_mapping(self) -> None:
        case = replace(
            self.case,
            verifications=(VerificationSpec("unsafe", "python missing.py", 5),),
        )
        suite = replace(self.suite, cases=(case,))

        result = run_suite(
            suite, self.run_dir, "openai", "test-model", self._passing_executor
        ).cases[0]

        self.assertEqual("error", result.status)
        self.assertEqual(("verification_error",), result.failure_codes)
        self.assertEqual(
            "verification_policy_rejected", result.verifications[0].error_code
        )
        self.assertIsNone(result.verifications[0].exit_code)

    def test_verification_validation_error_has_fixed_error_mapping(self) -> None:
        with patch(
            "tricoder.evals.runner.CommandPolicy.validate",
            side_effect=OSError("PROVIDER-SECRET-SENTINEL"),
        ):
            result = run_suite(
                self.suite,
                self.run_dir,
                "openai",
                "test-model",
                self._passing_executor,
            ).cases[0]

        self.assertEqual("error", result.status)
        self.assertEqual(("verification_error",), result.failure_codes)
        self.assertEqual("verification_error", result.verifications[0].error_code)
        self.assertIsNone(result.verifications[0].exit_code)

    def test_verification_os_error_has_fixed_error_mapping(self) -> None:
        with patch(
            "tricoder.evals.runner.subprocess.run",
            side_effect=OSError("PROVIDER-SECRET-SENTINEL"),
        ):
            result = run_suite(
                self.suite,
                self.run_dir,
                "openai",
                "test-model",
                self._passing_executor,
            ).cases[0]

        self.assertEqual("error", result.status)
        self.assertEqual(("verification_error",), result.failure_codes)
        self.assertEqual("verification_error", result.verifications[0].error_code)
        self.assertIsNone(result.verifications[0].exit_code)

    def test_reserved_verifier_collision_is_workspace_error_and_is_removed(self) -> None:
        case = replace(self.case, allowed_changes=("**",))
        suite = replace(self.suite, cases=(case,))

        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            del case, audit_path
            (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            reserved = workspace / RESERVED_VERIFIER_DIR
            reserved.mkdir()
            (reserved / "forged.py").write_text("pass\n", encoding="utf-8")
            return RunResult(True, "done", 1, 1, (), "通过")

        result = run_suite(
            suite, self.run_dir, "openai", "test-model", executor
        ).cases[0]

        self.assertEqual("error", result.status)
        self.assertEqual(("workspace_error",), result.failure_codes)
        self.assertIn(
            f"{RESERVED_VERIFIER_DIR}/forged.py", result.modified_files
        )
        self.assertFalse(
            (self.run_dir / "workspaces" / case.id / RESERVED_VERIFIER_DIR).exists()
        )

    def test_nested_reserved_path_is_rejected_even_with_broad_allowed_glob(
        self,
    ) -> None:
        case = replace(self.case, allowed_changes=("**",))
        suite = replace(self.suite, cases=(case,))

        def executor(
            case: EvalCase, workspace: Path, audit_path: Path
        ) -> RunResult:
            del case, audit_path
            (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            reserved = workspace / "nested" / RESERVED_VERIFIER_DIR
            reserved.mkdir(parents=True)
            (reserved / "forged.py").write_text("pass\n", encoding="utf-8")
            return RunResult(True, "done", 1, 1, (), "通过")

        result = run_suite(
            suite, self.run_dir, "openai", "test-model", executor
        ).cases[0]

        self.assertEqual("error", result.status)
        self.assertEqual(("workspace_error",), result.failure_codes)
        self.assertEqual((), result.verifications)
        self.assertIn(
            f"nested/{RESERVED_VERIFIER_DIR}/forged.py",
            result.modified_files,
        )
        self.assertFalse(
            (self.run_dir / "workspaces" / case.id / RESERVED_VERIFIER_DIR).exists()
        )


if __name__ == "__main__":
    unittest.main()
