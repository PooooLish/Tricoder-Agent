"""离线 Eval 报告兼容性和配对比较测试。"""

from __future__ import annotations

import unittest

from tricoder.evals.compare import ComparisonError, compare_report_payloads


def _trial(
    condition: str,
    case: str,
    repetition: int,
    success: bool | None,
    *,
    execution_kind: str = "quality",
) -> dict[str, object]:
    dimensions = {
        "artifact_correct": success,
        "agent_completed": success,
        "scope_compliant": success,
        "cleanup_confirmed": success,
        "behavior_correct": None,
        "fault_triggered": None,
        "recovered": None,
        "safety_triggered": None,
        "safety_blocked": None,
        "legitimate_action_allowed": None,
        "memory_constraint_preserved": None,
        "memory_latest_correction_used": None,
        "memory_stale_state_avoided": None,
        "memory_session_isolated": None,
    }
    return {
        "condition_id": condition,
        "case_id": case,
        "repetition": repetition,
        "execution_kind": execution_kind,
        "status": "passed" if success else "failed",
        "dimensions": dimensions,
        "failure_stage": None,
        "failure_codes": [],
        "duration_ms": 10,
        "rounds": 1,
        "tool_calls": 1,
        "retries": 0,
        "usage": {"input_tokens": 3, "output_tokens": 1, "cached_tokens": None, "cache_miss_tokens": None},
        "summary_usage": {"input_tokens": None, "output_tokens": None, "cached_tokens": None, "cache_miss_tokens": None},
        "triggered_faults": [],
        "recovery_path": None,
        "summary_triggered": False,
        "summary_count": 0,
        "dangerous_actions_proposed": 0,
        "dangerous_actions_executed": 0,
        "safety_bypass_attempts": 0,
        "legitimate_actions_attempted": 0,
        "legitimate_actions_allowed": 0,
    }


def _report(condition: str, model: str, trials: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "run_id": f"run-{condition}",
        "experiment_id": "experiment",
        "suite_id": "quality-v1",
        "benchmark_version": "quality-v1",
        "started_at": "2026-09-23T00:00:00+00:00",
        "duration_ms": 20,
        "planned_trials": len(trials),
        "complete": True,
        "conditions": [{
            "id": condition,
            "provider": "openai",
            "model": model,
            "execution_kind": "quality",
            "memory_compaction": "off",
            "memory_persistence": "off",
            "scorers": ["hidden_verifier"],
            "faults": ["none"],
        }],
        "fingerprints": {
            "suite": "a" * 64,
            "verifier": "b" * 64,
            "tasks": "c" * 64,
            "budgets": "d" * 64,
            "approval_policy": "eval-auto-approve",
            "environment": "e" * 64,
            "code": "f" * 64,
        },
        "coverage": {},
        "dimensions": {},
        "usage": {},
        "summary_usage": {},
        "cost_per_success": None,
        "trials": trials,
    }


class EvalCompareTests(unittest.TestCase):
    def test_pairs_same_case_and_repetition_and_reports_transitions(self) -> None:
        baseline = _report("base", "model-a", [_trial("base", "one", 1, True), _trial("base", "two", 1, False)])
        candidate = _report("candidate", "model-b", [_trial("candidate", "one", 1, False), _trial("candidate", "two", 1, True)])

        result = compare_report_payloads(
            baseline,
            candidate,
            allowed_variables=frozenset({"model"}),
        )

        self.assertEqual(2, result["paired"])
        self.assertEqual(0, result["unpaired_baseline"])
        self.assertEqual(0, result["unpaired_candidate"])
        self.assertEqual(1, result["regressions"])
        self.assertEqual(1, result["improvements"])
        self.assertEqual(0.0, result["end_to_end_percentage_point_delta"])

    def test_rejects_undeclared_model_or_fingerprint_difference(self) -> None:
        baseline = _report("base", "model-a", [_trial("base", "one", 1, True)])
        candidate = _report("candidate", "model-b", [_trial("candidate", "one", 1, True)])

        with self.assertRaisesRegex(ComparisonError, "model"):
            compare_report_payloads(baseline, candidate)

        candidate["fingerprints"] = dict(candidate["fingerprints"], verifier="0" * 64)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ComparisonError, "verifier"):
            compare_report_payloads(
                baseline,
                candidate,
                allowed_variables=frozenset({"model"}),
            )

    def test_incomplete_pairing_is_visible_and_execution_kinds_cannot_mix(self) -> None:
        baseline = _report("base", "model-a", [_trial("base", "one", 1, True), _trial("base", "orphan", 1, True)])
        candidate = _report("candidate", "model-a", [_trial("candidate", "one", 1, True)])

        result = compare_report_payloads(baseline, candidate)
        self.assertEqual(1, result["paired"])
        self.assertEqual(1, result["unpaired_baseline"])

        candidate["trials"] = [_trial("candidate", "one", 1, True, execution_kind="contract")]
        with self.assertRaisesRegex(ComparisonError, "execution_kind"):
            compare_report_payloads(baseline, candidate)


if __name__ == "__main__":
    unittest.main()
