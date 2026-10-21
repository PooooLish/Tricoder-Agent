"""固定回归题的宿主行为断言与期望结果评分。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
import tempfile

from tricoder.evals.assertions import (
    AssertionObservation,
    ToolObservation,
    evaluate_assertions,
    score_expectation,
)
from tricoder.evals.loader import EvalDefinitionError, load_suite
from tricoder.evals.models import (
    EvalCase,
    EvalCondition,
    PlannedTrial,
    TrialDimensions,
    TrialKey,
    TrialRecord,
)
from tricoder.evals.report import trial_record_as_dict, trial_record_from_dict
from tricoder.evals.runner import EvalCaseResult, VerificationResult
from tricoder.evals.service import _trial_from_case_result


class EvalRegressionAssertionTests(unittest.TestCase):
    def test_hidden_success_without_readback_cannot_satisfy_spill_assertion(self) -> None:
        observation = AssertionObservation(
            tool_events=(
                ToolObservation("read_file", True, spilled=True),
            ),
        )

        result = evaluate_assertions(("readback_after_spill",), observation)[0]

        self.assertFalse(result.passed)
        self.assertEqual("tail_readback_missing", result.reason_code)

    def test_readback_must_cover_tail_facts_not_only_call_the_tool(self) -> None:
        missing_tail = evaluate_assertions(
            ("readback_after_spill",),
            AssertionObservation(
                tool_events=(
                    ToolObservation("read_file", True, spilled=True),
                    ToolObservation("read_tool_result", True),
                )
            ),
        )[0]
        covered_tail = evaluate_assertions(
            ("readback_after_spill",),
            AssertionObservation(
                tool_events=(
                    ToolObservation("read_file", True, spilled=True),
                    ToolObservation(
                        "read_tool_result",
                        True,
                        fact_ids=("EVAL_TAIL_731", "connection_pool_exhausted"),
                    ),
                )
            ),
        )[0]

        self.assertFalse(missing_tail.passed)
        self.assertTrue(covered_tail.passed)

    def test_missing_observation_stays_unknown_instead_of_becoming_true(self) -> None:
        result = evaluate_assertions(
            ("workspace_change_notice",), AssertionObservation()
        )[0]

        self.assertIsNone(result.passed)
        self.assertEqual("observation_missing", result.reason_code)

    def test_blocked_contract_can_meet_expectation_while_agent_remains_incomplete(self) -> None:
        assertion = evaluate_assertions(
            ("text_termination_limit",),
            AssertionObservation(stop_reason="native_missing_tool_call", rounds=3),
        )

        met = score_expectation(
            execution_kind="contract",
            expected_outcome="blocked",
            artifact_correct=True,
            agent_completed=False,
            scope_compliant=True,
            cleanup_confirmed=True,
            infrastructure_error=False,
            assertions=assertion,
        )

        self.assertTrue(met)

    def test_unrelated_runtime_error_cannot_masquerade_as_expected_block(self) -> None:
        met = score_expectation(
            execution_kind="contract",
            expected_outcome="blocked",
            artifact_correct=True,
            agent_completed=False,
            scope_compliant=None,
            cleanup_confirmed=None,
            infrastructure_error=True,
            assertions=(),
        )

        self.assertFalse(met)

    def test_review_can_complete_while_truthfully_reporting_project_test_failure(self) -> None:
        assertions = evaluate_assertions(
            ("review_report_facts",),
            AssertionObservation(
                report_facts=("calculator.py", "test_subtract", "reversed_operands"),
                reviewed_test_failed=True,
            ),
        )

        met = score_expectation(
            execution_kind="quality",
            expected_outcome="completed",
            artifact_correct=True,
            agent_completed=True,
            scope_compliant=True,
            cleanup_confirmed=True,
            infrastructure_error=False,
            assertions=assertions,
        )

        self.assertTrue(met)

    def test_registered_report_and_baseline_assertions_return_results(self) -> None:
        """固定断言不能因遗漏 return 而把运行时评分变成 None/异常。"""

        review, deny, spill = evaluate_assertions(
            ("review_report_facts", "baseline_changed_deny", "spill_tail_facts"),
            AssertionObservation(
                report_facts=(
                    "calculator.py", "test_subtract", "reversed_operands",
                    "EVAL_TAIL_731", "connection_pool_exhausted",
                ),
                reviewed_test_failed=True,
                baseline_confirmations=1,
                provider_requests=0,
                baseline_revision_preserved=True,
            ),
        )

        self.assertTrue(review.passed)
        self.assertTrue(deny.passed)
        self.assertTrue(spill.passed)

    def test_approval_denial_requires_observed_denial_and_no_write_afterward(self) -> None:
        denied = evaluate_assertions(
            ("approval_denied",),
            AssertionObservation(
                approval_decisions=(False,),
                writes_after_denial=0,
            ),
        )[0]
        missing = evaluate_assertions(
            ("approval_denied",),
            AssertionObservation(approval_decisions=(), writes_after_denial=0),
        )[0]

        self.assertTrue(denied.passed)
        self.assertFalse(missing.passed)

    def test_unknown_assertion_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unregistered"):
            evaluate_assertions(("arbitrary_python",), AssertionObservation())

    def test_all_assertions_must_be_true(self) -> None:
        assertions = evaluate_assertions(
            ("runtime_restarted", "session_id_preserved"),
            AssertionObservation(runtime_instances=2, session_id_preserved=False),
        )

        self.assertFalse(
            score_expectation(
                execution_kind="quality",
                expected_outcome="completed",
                artifact_correct=True,
                agent_completed=True,
                scope_compliant=True,
                cleanup_confirmed=True,
                infrastructure_error=False,
                assertions=assertions,
            )
        )

    def test_trial_record_round_trip_preserves_expectation_and_assertions(self) -> None:
        assertion = evaluate_assertions(
            ("text_termination_limit",),
            AssertionObservation(stop_reason="native_missing_tool_call", rounds=3),
        )
        record = TrialRecord(
            TrialKey("contract", "text", 1),
            "contract",
            "passed",
            TrialDimensions(
                artifact_correct=True,
                agent_completed=False,
                scope_compliant=True,
                cleanup_confirmed=True,
                behavior_correct=True,
            ),
            failure_codes=("agent_failed",),
            expectation_met=True,
            assertion_results=assertion,
        )

        restored = trial_record_from_dict(trial_record_as_dict(record))

        self.assertTrue(restored.expectation_met)
        self.assertFalse(restored.dimensions.agent_completed)
        self.assertEqual("agent_failed", restored.failure_codes[0])
        self.assertEqual("text_termination_limit", restored.assertion_results[0].id)

    def test_old_v2_case_defaults_to_completed_without_assertions(self) -> None:
        suite = load_suite(Path("evals/quality-v1"))

        self.assertEqual((), suite.cases[0].assertions)
        self.assertEqual("completed", suite.cases[0].expected_outcome)

    def test_loader_rejects_unknown_assertion_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "suite"
            case = root / "cases" / "bad"
            (case / "workspace").mkdir(parents=True)
            (case / "verifier").mkdir()
            (root / "suite.toml").write_text(
                'schema_version = 2\nbenchmark_version = "regression-v1"\n'
                'id = "suite"\ntitle = "Suite"\ncases = ["bad"]\n',
                encoding="utf-8",
            )
            (case / "case.toml").write_text(
                'id = "bad"\ntitle = "Bad"\ntask = "Do it"\n'
                'allowed_changes = []\nrequired_changes = []\nmax_rounds = 3\n'
                'max_context_chars = 4000\ncategory = "delivery"\nsplit = "dev"\n'
                'execution_kind = "contract"\nscorers = ["behavior_assertions"]\n'
                'faults = ["none"]\ndimensions = ["behavior_correct"]\n'
                'assertions = ["arbitrary_python"]\nexpected_outcome = "blocked"\n'
                '[[steps]]\nkind = "user_turn"\ncontent = "Do it"\n'
                '[[verification]]\nname = "hidden"\n'
                'command = "python -m unittest discover -s .tricoder_eval_verifier -q"\n'
                'timeout = 5\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EvalDefinitionError, "assertions"):
                load_suite(root)

    def test_trial_conversion_keeps_agent_failure_for_expected_safe_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            verifier = root / "verifier"
            workspace.mkdir()
            verifier.mkdir()
            case = EvalCase(
                id="contract",
                title="contract",
                task="task",
                source_dir=root,
                workspace_dir=workspace,
                verifier_dir=verifier,
                allowed_changes=(),
                required_changes=(),
                max_rounds=4,
                max_context_chars=4000,
                verifications=(),
                execution_kind="contract",
                scorers=("behavior_assertions",),
                dimensions=(
                    "artifact_correct", "behavior_correct", "agent_completed",
                    "scope_compliant", "cleanup_confirmed",
                ),
                assertions=("text_termination_limit",),
                expected_outcome="blocked",
            )
            plan = PlannedTrial(
                1,
                TrialKey("contract", "contract", 1),
                case,
                EvalCondition(
                    "contract", "fake", "offline", "contract", "off", "off",
                    ("behavior_assertions",), ("none",),
                ),
            )
            result = EvalCaseResult(
                "contract",
                "failed",
                ("agent_failed",),
                1,
                3,
                0,
                (),
                (VerificationResult("hidden", 0, True, None),),
                "未运行",
                None,
            )

            record = _trial_from_case_result(
                plan,
                result,
                assertion_observation=AssertionObservation(
                    stop_reason="native_missing_tool_call", rounds=3
                ),
            )

        self.assertEqual("passed", record.status)
        self.assertTrue(record.expectation_met)
        self.assertFalse(record.dimensions.agent_completed)
        self.assertIn("agent_failed", record.failure_codes)

    def test_hidden_success_cannot_fill_behavior_dimension_without_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            verifier = root / "verifier"
            workspace.mkdir()
            verifier.mkdir()
            case = EvalCase(
                id="quality",
                title="quality",
                task="task",
                source_dir=root,
                workspace_dir=workspace,
                verifier_dir=verifier,
                allowed_changes=(),
                required_changes=(),
                max_rounds=4,
                max_context_chars=4000,
                verifications=(),
                execution_kind="quality",
                scorers=("hidden_verifier", "behavior_assertions"),
                dimensions=(
                    "artifact_correct", "behavior_correct", "agent_completed",
                    "scope_compliant", "cleanup_confirmed",
                ),
                assertions=("workspace_change_notice",),
            )
            plan = PlannedTrial(
                1,
                TrialKey("quality", "quality", 1),
                case,
                EvalCondition(
                    "quality", "openai", "offline", "quality", "off", "off",
                    ("hidden_verifier", "behavior_assertions"), ("none",),
                ),
            )
            result = EvalCaseResult(
                "quality", "passed", (), 1, 1, 1, (),
                (VerificationResult("hidden", 0, True, None),), "未运行", None,
            )

            record = _trial_from_case_result(
                plan,
                result,
                assertion_observation=AssertionObservation(),
            )

        self.assertTrue(record.dimensions.artifact_correct)
        self.assertIsNone(record.dimensions.behavior_correct)
        self.assertFalse(record.expectation_met)
        self.assertEqual("failed", record.status)


if __name__ == "__main__":
    unittest.main()
