"""Tests for safe structured eval reports."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tricoder.evals.report import report_as_dict, render_markdown, write_reports
from tricoder.evals.runner import EvalCaseResult, EvalRunReport, VerificationResult
from tricoder.models import TokenUsage


class EvalReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name) / "run-001"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _report_with_unknown_usage_and_failure(self) -> EvalRunReport:
        return EvalRunReport(
            run_id="run-001",
            suite_id="smoke",
            provider="openai",
            model="test-model",
            started_at="2026-08-25T00:00:00+00:00",
            duration_ms=1250,
            cases=(
                EvalCaseResult(
                    case_id="case|one\ntwo",
                    status="failed",
                    failure_codes=("verification_failed",),
                    duration_ms=100,
                    rounds=2,
                    tool_calls=3,
                    modified_files=("app.py",),
                    verifications=(
                        VerificationResult("hidden\ncheck", 1, False, None),
                    ),
                    agent_verification="通过",
                    usage=TokenUsage(output_tokens=4),
                ),
                EvalCaseResult(
                    case_id="case-two",
                    status="passed",
                    failure_codes=(),
                    duration_ms=200,
                    rounds=1,
                    tool_calls=1,
                    modified_files=(),
                    verifications=(
                        VerificationResult("hidden", 0, True, None),
                    ),
                    agent_verification="通过",
                    usage=TokenUsage(input_tokens=2, cached_tokens=1),
                ),
            ),
            usage=TokenUsage(input_tokens=2, output_tokens=4, cached_tokens=1),
        )

    def test_write_reports_persists_metrics_without_free_text(self) -> None:
        report = self._report_with_unknown_usage_and_failure()

        json_path, markdown_path = write_reports(report, self.run_dir)

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIsNone(payload["cases"][0]["usage"]["input_tokens"])
        self.assertEqual("verification_failed", payload["cases"][0]["failure_codes"][0])
        self.assertNotIn("PROVIDER-SECRET-SENTINEL", json_path.read_text("utf-8"))
        self.assertNotIn("PROVIDER-SECRET-SENTINEL", markdown)
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual(0.5, payload["pass_rate"])
        self.assertEqual(2, payload["usage"]["input_tokens"])
        self.assertNotIn("task", payload["cases"][0])
        self.assertNotIn("title", payload["cases"][0])
        self.assertNotIn("summary", payload["cases"][0])

    def test_markdown_escapes_table_cells_and_only_includes_safe_columns(self) -> None:
        markdown = render_markdown(self._report_with_unknown_usage_and_failure())

        self.assertIn("case\\|one<br>two", markdown)
        self.assertIn("hidden<br>check: failed", markdown)
        self.assertNotIn("PROVIDER-SECRET-SENTINEL", markdown)
        self.assertNotIn("test-model", markdown)
        self.assertNotIn("app.py", markdown)

    def test_report_as_dict_has_only_fixed_structured_fields(self) -> None:
        payload = report_as_dict(self._report_with_unknown_usage_and_failure())

        self.assertEqual(
            {
                "schema_version", "run_id", "suite_id", "provider", "model",
                "started_at", "duration_ms", "pass_rate", "usage", "cases",
            },
            set(payload),
        )
        self.assertEqual(
            {
                "case_id", "status", "failure_codes", "duration_ms", "rounds",
                "tool_calls", "modified_file_count", "verifications",
                "agent_verification", "usage",
            },
            set(payload["cases"][0]),
        )

    def test_write_reports_uses_atomic_replacement_without_tmp_files(self) -> None:
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "result.json").write_text("old", encoding="utf-8")
        (self.run_dir / "report.md").write_text("old", encoding="utf-8")

        write_reports(self._report_with_unknown_usage_and_failure(), self.run_dir)

        self.assertFalse(list(self.run_dir.glob("*.tmp")))
        self.assertEqual(1, json.loads((self.run_dir / "result.json").read_text("utf-8"))["schema_version"])

    def test_write_reports_rejects_output_path_escape(self) -> None:
        with self.assertRaises(ValueError):
            write_reports(self._report_with_unknown_usage_and_failure(), Path())


if __name__ == "__main__":
    unittest.main()
