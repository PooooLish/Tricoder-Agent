"""Hand-derived dimensional metric contracts for versioned Eval reports."""

from __future__ import annotations

import unittest

from tricoder.evals.metrics import aggregate_trial_metrics
from tricoder.evals.models import TrialDimensions, TrialKey, TrialRecord
from tricoder.models import TokenUsage


class EvalMetricsTests(unittest.TestCase):
    def _trial(
        self,
        case_id: str,
        *,
        status: str,
        artifact_correct: bool | None,
        agent_completed: bool | None,
        scope_compliant: bool | None = True,
        cleanup_confirmed: bool | None = True,
        failure_stage: str | None = None,
    ) -> TrialRecord:
        return TrialRecord(
            key=TrialKey("baseline", case_id, 1),
            execution_kind="quality",
            status=status,
            dimensions=TrialDimensions(
                artifact_correct=artifact_correct,
                agent_completed=agent_completed,
                scope_compliant=scope_compliant,
                cleanup_confirmed=cleanup_confirmed,
            ),
            failure_stage=failure_stage,
        )

    def test_hand_calculated_metrics_keep_correctness_finish_and_e2e_separate(self) -> None:
        """Collapsing all outcomes into one pass bit would hide B's correct code."""
        trials = (
            self._trial("a", status="passed", artifact_correct=True, agent_completed=True),
            self._trial("b", status="failed", artifact_correct=True, agent_completed=False),
            self._trial("c", status="failed", artifact_correct=False, agent_completed=True),
            self._trial(
                "d",
                status="error",
                artifact_correct=None,
                agent_completed=None,
                scope_compliant=None,
                cleanup_confirmed=None,
                failure_stage="provider",
            ),
        )

        metrics = aggregate_trial_metrics(trials)

        self.assertEqual((2, 4, 1, 0.5), metrics.code_correct_planned.as_tuple())
        self.assertEqual((2, 3, 1, 2 / 3), metrics.code_correct_verified.as_tuple())
        self.assertEqual((1, 4, 1, 0.25), metrics.end_to_end.as_tuple())
        self.assertEqual((2, 3, 1, 2 / 3), metrics.normal_finish.as_tuple())
        self.assertEqual(1, metrics.infrastructure_errors)
        self.assertEqual(1, metrics.unverified)

    def test_empty_denominators_and_unknown_usage_are_not_rendered_as_zero(self) -> None:
        """No observations must remain unavailable rather than becoming a 0% score."""
        metrics = aggregate_trial_metrics(())

        self.assertEqual((0, 0, 0, None), metrics.code_correct_planned.as_tuple())
        self.assertIsNone(metrics.total_usage.input_tokens)
        self.assertIsNone(metrics.total_usage.output_tokens)
        self.assertIsNone(metrics.cost_per_success)

    def test_not_run_trials_stay_in_planned_denominators(self) -> None:
        """Budget exhaustion must not improve a conservative completion rate."""
        trial = self._trial(
            "budgeted",
            status="not_run_budget",
            artifact_correct=None,
            agent_completed=None,
            scope_compliant=None,
            cleanup_confirmed=None,
        )

        metrics = aggregate_trial_metrics((trial,))

        self.assertEqual((0, 1, 1, 0.0), metrics.code_correct_planned.as_tuple())
        self.assertEqual((0, 0, 1, None), metrics.code_correct_verified.as_tuple())
        self.assertEqual(1, metrics.not_run)

    def test_partial_and_summary_usage_keep_field_level_unknowns(self) -> None:
        """Failed attempts and summarizer usage count, but absent fields never become zero."""
        first = self._trial(
            "first", status="passed", artifact_correct=True, agent_completed=True
        )
        second = self._trial(
            "second", status="failed", artifact_correct=False, agent_completed=True
        )
        first = first.__class__(
            **{
                **{field: getattr(first, field) for field in first.__dataclass_fields__},
                "usage": TokenUsage(input_tokens=10, output_tokens=2),
                "summary_usage": TokenUsage(input_tokens=3, output_tokens=1),
            }
        )
        second = second.__class__(
            **{
                **{field: getattr(second, field) for field in second.__dataclass_fields__},
                "usage": TokenUsage(input_tokens=5),
                "summary_usage": None,
            }
        )

        metrics = aggregate_trial_metrics((first, second))

        self.assertEqual(15, metrics.total_usage.input_tokens)
        self.assertIsNone(metrics.total_usage.output_tokens)
        self.assertIsNone(metrics.total_summary_usage.input_tokens)
        self.assertEqual(2, metrics.usage_observed)
        self.assertEqual(1, metrics.summary_usage_observed)


if __name__ == "__main__":
    unittest.main()
