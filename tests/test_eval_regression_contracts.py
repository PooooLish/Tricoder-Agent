"""Regression V1 六个 contract 场景的离线生产路径回归。"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from tricoder.evals.assertions import (
    AssertionObservation,
    SubcaseResult,
    evaluate_assertions,
)
from tricoder.evals.loader import load_suite
from tricoder.evals.service import run_eval_command


_ROOT = Path(__file__).parents[1]
_EXPERIMENT = _ROOT / "evals" / "regression-v1" / "experiment-contract.toml"


class EvalRegressionContractTests(unittest.TestCase):
    def test_offline_contract_experiment_runs_all_six_without_external_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            previous = Path.cwd()
            os.chdir(temporary)
            try:
                output = io.StringIO()
                external_provider_calls: list[str] = []
                args = argparse.Namespace(
                    suite=None,
                    experiment=_EXPERIMENT,
                    repeat=1,
                    provider="openai",
                    model=None,
                    base_url=None,
                    env_file=None,
                    case=None,
                    dry_run=False,
                    no_color=True,
                )

                exit_code = run_eval_command(
                    args,
                    environ={},
                    provider_factory=lambda *_args: external_provider_calls.append("called"),  # type: ignore[arg-type]
                    output=output,
                )

                self.assertEqual(0, exit_code, output.getvalue())
                self.assertEqual([], external_provider_calls)
                result_line = next(
                    line for line in output.getvalue().splitlines() if line.startswith("result=")
                )
                payload = json.loads(Path(result_line.partition("=")[2]).read_text("utf-8"))
                self.assertEqual(6, payload["planned_trials"])
                self.assertEqual(6, len(payload["trials"]))
                self.assertTrue(all(row["expectation_met"] is True for row in payload["trials"]))
                by_case = {row["case_id"]: row for row in payload["trials"]}
                self.assertFalse(by_case["c-text-termination"]["dimensions"]["agent_completed"])
                self.assertIn("agent_failed", by_case["c-text-termination"]["failure_codes"])
                self.assertFalse(by_case["c-approval-denied"]["dimensions"]["agent_completed"])
                changed = by_case["c-restart-changed"]["subcases"]
                self.assertEqual({"accept", "deny"}, {item["id"] for item in changed})
                self.assertTrue(next(item for item in changed if item["id"] == "accept")["agent_completed"])
                self.assertFalse(next(item for item in changed if item["id"] == "deny")["agent_completed"])
                corrupted = by_case["c-baseline-corrupted"]["subcases"]
                self.assertEqual(
                    {"kind-array", "kind-object", "invalid-json"},
                    {item["id"] for item in corrupted},
                )
                self.assertTrue(all(item["expectation_met"] for item in corrupted))
                report_path = Path(
                    next(
                        line.partition("=")[2]
                        for line in output.getvalue().splitlines()
                        if line.startswith("report=")
                    )
                )
                report_text = report_path.read_text("utf-8")
                self.assertIn("## Subcases", report_text)
                self.assertIn("| accept | True | True | ok |", report_text)
                self.assertIn("| invalid-json | True | False | ok |", report_text)
            finally:
                os.chdir(previous)

    def test_contract_assertions_reject_missing_trigger_and_failed_subcase(self) -> None:
        no_denial = evaluate_assertions(
            ("approval_denied",),
            AssertionObservation(approval_decisions=(True,), writes_after_denial=0),
        )[0]
        failed_subcase = evaluate_assertions(
            ("baseline_changed_both",),
            AssertionObservation(
                subcases=(
                    SubcaseResult("accept", True, True),
                    SubcaseResult("deny", False, False, "provider_called"),
                )
            ),
        )[0]

        self.assertFalse(no_denial.passed)
        self.assertFalse(failed_subcase.passed)

    def test_every_contract_assertion_rejects_an_empty_observation(self) -> None:
        suite = load_suite(_EXPERIMENT.parent)

        for case in (item for item in suite.cases if item.execution_kind == "contract"):
            with self.subTest(case=case.id):
                results = evaluate_assertions(case.assertions, AssertionObservation())
                self.assertTrue(results)
                self.assertTrue(all(item.passed is not True for item in results), results)


if __name__ == "__main__":
    unittest.main()
