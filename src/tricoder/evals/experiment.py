"""Deterministic trial scheduling with budgets and incremental persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import random
import tempfile
import time
from typing import TypeAlias

from .models import (
    ExperimentDefinition,
    ExperimentRunReport,
    PlannedTrial,
    TrialDimensions,
    TrialKey,
    TrialRecord,
)
from .output import _is_link_or_reparse_point, validate_run_directory
from .report import (
    trial_record_as_dict,
    trial_record_from_dict,
    write_experiment_reports,
)


TrialExecutor: TypeAlias = Callable[[PlannedTrial, Path], TrialRecord]
Clock: TypeAlias = Callable[[], float]
CancelCheck: TypeAlias = Callable[[], bool]


def plan_trials(definition: ExperimentDefinition) -> tuple[PlannedTrial, ...]:
    """Freeze a seeded, cross-condition-interleaved trial schedule."""

    cases = list(definition.suite.cases)
    random.Random(definition.seed).shuffle(cases)
    planned: list[PlannedTrial] = []
    ordinal = 0
    for repetition in range(1, definition.repetitions + 1):
        for case_index, case in enumerate(cases):
            # 每道题轮换条件起点，避免一个 Provider 总在同一时间段先执行。
            conditions = [
                condition
                for condition in definition.conditions
                if (
                    definition.suite.schema_version == 1
                    or condition.execution_kind == case.execution_kind
                )
            ]
            if not conditions:
                raise ValueError("v2 case has no matching execution_kind condition")
            offset = (case_index + repetition - 1) % len(conditions)
            conditions = conditions[offset:] + conditions[:offset]
            for condition in conditions:
                ordinal += 1
                planned.append(
                    PlannedTrial(
                        ordinal=ordinal,
                        key=TrialKey(condition.id, case.id, repetition),
                        case=case,
                        condition=condition,
                    )
                )
    return tuple(planned)


def run_experiment(
    definition: ExperimentDefinition,
    run_dir: Path,
    executor: TrialExecutor,
    *,
    clock: Clock = time.monotonic,
    cancelled: CancelCheck = lambda: False,
) -> ExperimentRunReport:
    """Run a frozen plan serially and persist every completed plan row."""

    directory = validate_run_directory(Path.cwd(), run_dir, require_empty=True)
    planned = plan_trials(definition)
    if len(planned) != definition.planned_trials:
        raise ValueError("experiment plan size mismatch")
    results_dir = directory / "results"
    trials_dir = directory / "trials"
    results_dir.mkdir()
    trials_dir.mkdir()
    _atomic_json(
        directory / "plan.json",
        {
            "schema_version": 1,
            "experiment_id": definition.id,
            "suite_id": definition.suite.id,
            "seed": definition.seed,
            "planned_trials": [
                {
                    "ordinal": item.ordinal,
                    "condition_id": item.key.condition_id,
                    "case_id": item.key.case_id,
                    "repetition": item.key.repetition,
                }
                for item in planned
            ],
        },
    )

    started_at = datetime.now(timezone.utc).isoformat()
    started = clock()
    records: list[TrialRecord] = []
    stop_status: str | None = None
    for item in planned:
        if stop_status is None and cancelled():
            stop_status = "not_run_cancelled"
        if stop_status is None and clock() - started >= definition.time_budget_seconds:
            stop_status = "not_run_budget"
        if stop_status is not None:
            record = _not_run_record(item, stop_status)
        else:
            trial_dir = trials_dir / f"{item.ordinal:04d}"
            trial_dir.mkdir()
            try:
                record = executor(item, trial_dir)
            except Exception:
                # 普通单次执行错误要形成结果并继续；KeyboardInterrupt/SystemExit 原样传播。
                record = TrialRecord(
                    key=item.key,
                    execution_kind=item.condition.execution_kind,
                    status="error",
                    dimensions=TrialDimensions(),
                    failure_stage="tool",
                    failure_codes=("executor_error",),
                )
            _validate_executor_record(item, record)
        records.append(record)
        _atomic_json(results_dir / f"{item.ordinal:04d}.json", trial_record_as_dict(record))

    duration_ms = max(0, int((clock() - started) * 1000))
    report = ExperimentRunReport(
        run_id=directory.name,
        experiment_id=definition.id,
        suite_id=definition.suite.id,
        started_at=started_at,
        duration_ms=duration_ms,
        planned_trials=len(planned),
        complete=all(
            record.status not in {"not_run_budget", "not_run_cancelled", "cancelled"}
            for record in records
        ),
        trials=tuple(records),
        benchmark_version=definition.suite.benchmark_version,
        conditions=definition.conditions,
        fingerprints=_experiment_fingerprints(definition),
    )
    write_experiment_reports(report, directory)
    return report


def read_trial_records(run_dir: Path) -> tuple[TrialRecord, ...]:
    """Read already persisted safe trial rows after completion or interruption."""

    directory = validate_run_directory(Path.cwd(), run_dir, require_empty=False, create=False)
    results_dir = directory / "results"
    if not results_dir.is_dir():
        return ()
    if _is_link_or_reparse_point(results_dir):
        raise ValueError("results directory must be a normal directory")
    records: list[TrialRecord] = []
    for path in sorted(results_dir.glob("*.json")):
        if not path.is_file() or path.is_symlink():
            raise ValueError("trial result must be a normal file")
        records.append(trial_record_from_dict(json.loads(path.read_text("utf-8"))))
    return tuple(records)


def _not_run_record(item: PlannedTrial, status: str) -> TrialRecord:
    if status not in {"not_run_budget", "not_run_cancelled"}:
        raise ValueError("invalid not-run status")
    return TrialRecord(
        key=item.key,
        execution_kind=item.condition.execution_kind,
        status=status,  # type: ignore[arg-type]
        dimensions=TrialDimensions(),
        failure_stage="agent_budget" if status == "not_run_budget" else None,
        category=item.case.category,
    )


def _validate_executor_record(item: PlannedTrial, record: TrialRecord) -> None:
    if record.key != item.key:
        raise ValueError("executor returned a mismatched trial key")
    if record.execution_kind != item.condition.execution_kind:
        raise ValueError("executor returned a mismatched execution kind")


def _atomic_json(path: Path, payload: object) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _experiment_fingerprints(
    definition: ExperimentDefinition,
) -> tuple[tuple[str, str], ...]:
    """只保存不可逆哈希与固定策略 ID，不落盘题面、源码或 verifier 正文。"""

    task_parts: list[bytes] = []
    verifier_parts: list[bytes] = []
    budget_parts: list[bytes] = []
    for case in definition.suite.cases:
        task_parts.append(
            json.dumps(
                {
                    "id": case.id,
                    "category": case.category,
                    "split": case.split,
                    "kind": case.execution_kind,
                    "task": case.task,
                    "steps": [
                        {"kind": step.kind, "content": step.content, "target": step.target}
                        for step in case.steps
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )
        budget_parts.append(
            f"{case.id}:{case.max_rounds}:{case.max_context_chars}".encode("ascii")
        )
        verifier_parts.extend(_tree_parts(case.verifier_dir))
    budget_parts.append(
        (
            f"experiment:{definition.repetitions}:{definition.max_trials}:"
            f"{definition.time_budget_seconds}:{definition.seed}"
        ).encode("ascii")
    )
    code_root = Path(__file__).resolve().parents[1]
    fingerprints = {
        "suite": _digest(_suite_parts(definition)),
        "verifier": _digest(verifier_parts),
        "tasks": _digest(task_parts),
        "budgets": _digest(budget_parts),
        "approval_policy": "eval-auto-approve",
        "environment": _digest(
            [
                f"python:{platform.python_version()}".encode("ascii"),
                f"system:{platform.system()}:{platform.machine()}".encode("ascii"),
            ]
        ),
        "code": _digest(_tree_parts(code_root, suffix=".py")),
    }
    return tuple(sorted(fingerprints.items()))


def _suite_parts(definition: ExperimentDefinition) -> list[bytes]:
    """题库指纹只覆盖 suite 与 case，不受同目录实验样例变化影响。"""

    parts: list[bytes] = []
    suite_file = definition.suite.source_dir / "suite.toml"
    if suite_file.is_file() and not suite_file.is_symlink():
        parts.extend((b"suite.toml", suite_file.read_bytes()))
    else:
        parts.append(
            (
                f"programmatic:{definition.suite.id}:"
                f"{definition.suite.schema_version}:"
                f"{definition.suite.benchmark_version}"
            ).encode("utf-8")
        )
    for case in definition.suite.cases:
        for payload in _tree_parts(case.source_dir):
            parts.append(case.id.encode("utf-8"))
            parts.append(payload)
    return parts


def _tree_parts(root: Path, *, suffix: str | None = None) -> list[bytes]:
    parts: list[bytes] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if suffix is not None and path.suffix != suffix:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        parts.extend((relative, path.read_bytes()))
    return parts


def _digest(parts: list[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()
