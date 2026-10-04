"""Repeat scheduling, budgets and incremental experiment persistence."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tricoder.evals.experiment import read_trial_records, run_experiment
from tricoder.evals.models import (
    EvalCase,
    EvalCondition,
    EvalSuite,
    ExperimentDefinition,
    TrialDimensions,
    TrialRecord,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class EvalExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_cwd = Path.cwd()
        os.chdir(self.temporary.name)
        self.root = Path.cwd()
        self.run_dir = self.root / "runtime" / "evals" / "experiment-run"
        self.run_dir.mkdir(parents=True)
        self.definition = self._definition(repetitions=3)

    def tearDown(self) -> None:
        os.chdir(self.original_cwd)
        self.temporary.cleanup()

    def _definition(self, *, repetitions: int, time_budget: float = 60.0) -> ExperimentDefinition:
        cases: list[EvalCase] = []
        for case_id in ("case-a", "case-b"):
            case_root = self.root / case_id
            workspace = case_root / "workspace"
            verifier = case_root / "verifier"
            workspace.mkdir(parents=True, exist_ok=True)
            verifier.mkdir(exist_ok=True)
            cases.append(
                EvalCase(
                    id=case_id,
                    title=case_id,
                    task="synthetic task",
                    source_dir=case_root,
                    workspace_dir=workspace,
                    verifier_dir=verifier,
                    allowed_changes=("app.py",),
                    required_changes=("app.py",),
                    max_rounds=4,
                    max_context_chars=4000,
                    verifications=(),
                )
            )
        conditions = tuple(
            EvalCondition(
                id=condition_id,
                provider="fake",
                model="offline",
                execution_kind="contract",
                memory_compaction="off",
                memory_persistence="off",
                scorers=("hidden_verifier",),
                faults=("none",),
            )
            for condition_id in ("baseline", "variant")
        )
        return ExperimentDefinition(
            schema_version=1,
            id="matrix",
            source_path=self.root / "experiment.toml",
            suite=EvalSuite("suite", "Suite", self.root, tuple(cases)),
            repetitions=repetitions,
            split="all",
            conditions=conditions,
            max_trials=1000,
            time_budget_seconds=time_budget,
            seed=11,
        )

    @staticmethod
    def _passed(plan: object) -> TrialRecord:
        key = plan.key  # type: ignore[attr-defined]
        condition = plan.condition  # type: ignore[attr-defined]
        return TrialRecord(
            key=key,
            execution_kind=condition.execution_kind,
            status="passed",
            dimensions=TrialDimensions(
                artifact_correct=True,
                agent_completed=True,
                scope_compliant=True,
                cleanup_confirmed=True,
            ),
        )

    def test_two_conditions_two_cases_three_repetitions_are_unique_and_isolated(self) -> None:
        """Repetitions must never share a trial directory or stale marker state."""
        seen_keys: list[object] = []
        seen_dirs: list[Path] = []

        def executor(plan: object, trial_dir: Path) -> TrialRecord:
            self.assertEqual([], list(trial_dir.iterdir()))
            seen_keys.append(plan.key)  # type: ignore[attr-defined]
            seen_dirs.append(trial_dir)
            (trial_dir / "marker.txt").write_text("fresh", encoding="utf-8")
            return self._passed(plan)

        report = run_experiment(self.definition, self.run_dir, executor)

        self.assertEqual(12, len(report.trials))
        self.assertEqual(12, len(set(seen_keys)))
        self.assertEqual(12, len(set(seen_dirs)))
        self.assertEqual(12, len(read_trial_records(self.run_dir)))
        self.assertTrue(report.complete)

    def test_interrupt_on_fourth_trial_preserves_first_three_atomic_results(self) -> None:
        """A process interruption must not discard already completed repetitions."""
        calls = 0

        def executor(plan: object, trial_dir: Path) -> TrialRecord:
            del trial_dir
            nonlocal calls
            calls += 1
            if calls == 4:
                raise KeyboardInterrupt
            return self._passed(plan)

        with self.assertRaises(KeyboardInterrupt):
            run_experiment(self.definition, self.run_dir, executor)

        records = read_trial_records(self.run_dir)
        self.assertEqual(3, len(records))
        self.assertTrue(all(record.status == "passed" for record in records))
        self.assertFalse(list((self.run_dir / "results").glob("*.tmp")))

    def test_budget_exhaustion_retains_every_remaining_trial_as_not_run(self) -> None:
        """Stopping at a time budget must not silently shrink the plan denominator."""
        clock = MutableClock()
        definition = self._definition(repetitions=2, time_budget=10.0)

        def executor(plan: object, trial_dir: Path) -> TrialRecord:
            del trial_dir
            clock.value += 6.0
            return self._passed(plan)

        report = run_experiment(
            definition,
            self.run_dir,
            executor,
            clock=clock,
        )

        self.assertEqual(8, len(report.trials))
        self.assertEqual(2, sum(trial.status == "passed" for trial in report.trials))
        self.assertEqual(6, sum(trial.status == "not_run_budget" for trial in report.trials))
        self.assertFalse(report.complete)

    def test_cancellation_never_marks_unexecuted_trials_passed(self) -> None:
        """Cancellation after one execution must preserve all remaining plan rows."""
        executed = 0

        def executor(plan: object, trial_dir: Path) -> TrialRecord:
            del trial_dir
            nonlocal executed
            executed += 1
            return self._passed(plan)

        report = run_experiment(
            self.definition,
            self.run_dir,
            executor,
            cancelled=lambda: executed >= 1,
        )

        self.assertEqual(1, sum(trial.status == "passed" for trial in report.trials))
        self.assertEqual(11, sum(trial.status == "not_run_cancelled" for trial in report.trials))
        self.assertFalse(report.complete)

    def test_read_trial_records_rejects_linked_results_directory(self) -> None:
        run_experiment(
            self.definition,
            self.run_dir,
            lambda plan, _trial_dir: self._passed(plan),
        )
        results = self.run_dir / "results"

        with patch(
            "tricoder.evals.experiment._is_link_or_reparse_point",
            side_effect=lambda path: path == results,
        ):
            with self.assertRaisesRegex(ValueError, "results directory"):
                read_trial_records(self.run_dir)


if __name__ == "__main__":
    unittest.main()
