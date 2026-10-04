"""Strict offline comparison for safe Eval v2 result payloads."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from statistics import median
import tempfile
from typing import TextIO
from typing import Any

from .models import TrialRecord
from .output import reserve_run_directory, validate_run_directory
from .report import trial_record_from_dict


_ALLOWED_VARIABLES = frozenset({"model", "provider", "memory", "faults", "code"})
_FINGERPRINT_KEYS = frozenset(
    {"suite", "verifier", "tasks", "budgets", "approval_policy", "environment", "code"}
)


class ComparisonError(ValueError):
    """Reports cannot be compared without misrepresenting the experiment."""


@dataclass(frozen=True, slots=True)
class _ComparableReport:
    condition: dict[str, object]
    fingerprints: dict[str, str]
    benchmark_version: str
    trials: tuple[TrialRecord, ...]


def run_compare_command(args: object, *, output: TextIO) -> int:
    """CLI boundary: read two local safe reports and write an atomic comparison."""

    try:
        baseline = _read_json_report(Path(getattr(args, "baseline")))
        candidate = _read_json_report(Path(getattr(args, "candidate")))
        allowed = frozenset(getattr(args, "allow_variable", ()) or ())
        comparison = compare_report_payloads(
            baseline,
            candidate,
            allowed_variables=allowed,
        )
        run_id = datetime.now(timezone.utc).strftime("compare-%Y%m%dt%H%M%S.%fz")
        directory = reserve_run_directory(Path.cwd(), run_id)
        json_path, markdown_path = write_comparison_reports(comparison, directory)
    except Exception:
        output.write("eval_compare_error=incompatible\n")
        return 2
    output.write(f"result={json_path}\n")
    output.write(f"report={markdown_path}\n")
    return 0


def write_comparison_reports(
    comparison: dict[str, object], run_dir: Path
) -> tuple[Path, Path]:
    directory = validate_run_directory(Path.cwd(), run_dir, require_empty=True, create=False)
    json_path = directory / "comparison.json"
    markdown_path = directory / "comparison.md"
    _atomic_write(json_path, json.dumps(comparison, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(markdown_path, _comparison_markdown(comparison))
    return json_path, markdown_path


def compare_report_payloads(
    baseline_payload: object,
    candidate_payload: object,
    *,
    allowed_variables: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Pair case/repetition rows after checking all non-variable experiment facts."""

    unknown = allowed_variables - _ALLOWED_VARIABLES
    if unknown:
        raise ComparisonError("comparison variable is unsupported")
    baseline = _load_comparable(baseline_payload)
    candidate = _load_comparable(candidate_payload)
    _check_compatibility(baseline, candidate, allowed_variables)

    baseline_rows = _index_trials(baseline.trials)
    candidate_rows = _index_trials(candidate.trials)
    shared = tuple(sorted(set(baseline_rows) & set(candidate_rows)))
    baseline_only = tuple(sorted(set(baseline_rows) - set(candidate_rows)))
    candidate_only = tuple(sorted(set(candidate_rows) - set(baseline_rows)))
    baseline_values = tuple(_end_to_end(baseline_rows[key]) for key in shared)
    candidate_values = tuple(_end_to_end(candidate_rows[key]) for key in shared)
    baseline_known = tuple(value for value in baseline_values if value is not None)
    candidate_known = tuple(value for value in candidate_values if value is not None)
    improvements = sum(left is False and right is True for left, right in zip(baseline_values, candidate_values))
    regressions = sum(left is True and right is False for left, right in zip(baseline_values, candidate_values))
    baseline_rate = _rate(baseline_known)
    candidate_rate = _rate(candidate_known)
    duration_baseline = [row.duration_ms for row in baseline_rows.values() if row.duration_ms is not None]
    duration_candidate = [row.duration_ms for row in candidate_rows.values() if row.duration_ms is not None]
    baseline_usage = _usage_totals(baseline.trials, summary=False)
    candidate_usage = _usage_totals(candidate.trials, summary=False)
    baseline_summary_usage = _usage_totals(baseline.trials, summary=True)
    candidate_summary_usage = _usage_totals(candidate.trials, summary=True)
    return {
        "schema_version": 1,
        "benchmark_version": baseline.benchmark_version,
        "allowed_variables": sorted(allowed_variables),
        "paired": len(shared),
        "unpaired_baseline": len(baseline_only),
        "unpaired_candidate": len(candidate_only),
        "baseline_end_to_end": _metric(baseline_values),
        "candidate_end_to_end": _metric(candidate_values),
        "end_to_end_percentage_point_delta": (
            (candidate_rate - baseline_rate) * 100
            if baseline_rate is not None and candidate_rate is not None
            else None
        ),
        "improvements": improvements,
        "regressions": regressions,
        "unchanged": len(shared) - improvements - regressions,
        "baseline_duration_median_ms": median(duration_baseline) if duration_baseline else None,
        "candidate_duration_median_ms": median(duration_candidate) if duration_candidate else None,
        "duration_median_delta_ms": (
            median(duration_candidate) - median(duration_baseline)
            if duration_baseline and duration_candidate
            else None
        ),
        "baseline_usage": baseline_usage,
        "candidate_usage": candidate_usage,
        "usage_delta": _usage_delta(baseline_usage, candidate_usage),
        "baseline_summary_usage": baseline_summary_usage,
        "candidate_summary_usage": candidate_summary_usage,
        "summary_usage_delta": _usage_delta(
            baseline_summary_usage, candidate_summary_usage
        ),
        "baseline_cost_per_success": None,
        "candidate_cost_per_success": None,
        "baseline_failure_distribution": _failure_distribution(baseline.trials),
        "candidate_failure_distribution": _failure_distribution(candidate.trials),
    }


def _load_comparable(payload: object) -> _ComparableReport:
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise ComparisonError("strict comparison requires schema v2 reports")
    benchmark = payload.get("benchmark_version")
    conditions = payload.get("conditions")
    fingerprints = payload.get("fingerprints")
    trials = payload.get("trials")
    if not isinstance(benchmark, str) or not benchmark:
        raise ComparisonError("benchmark_version is missing")
    if not isinstance(conditions, list) or len(conditions) != 1 or not isinstance(conditions[0], dict):
        raise ComparisonError("strict comparison requires exactly one condition per report")
    condition = _read_condition(conditions[0])
    if not isinstance(fingerprints, dict) or set(fingerprints) != set(_FINGERPRINT_KEYS):
        raise ComparisonError("experiment fingerprints are incomplete")
    safe_fingerprints: dict[str, str] = {}
    for key in _FINGERPRINT_KEYS:
        value = fingerprints.get(key)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ComparisonError(f"{key} fingerprint is invalid")
        safe_fingerprints[key] = value
    if not isinstance(trials, list):
        raise ComparisonError("trials are missing")
    try:
        parsed = tuple(trial_record_from_dict(item) for item in trials)
    except ValueError as exc:
        raise ComparisonError("trial row is invalid") from exc
    return _ComparableReport(condition, safe_fingerprints, benchmark, parsed)


def _read_condition(raw: dict[str, object]) -> dict[str, object]:
    required = {
        "id", "provider", "model", "execution_kind", "memory_compaction",
        "memory_persistence", "scorers", "faults",
    }
    if set(raw) != required:
        raise ComparisonError("condition metadata is invalid")
    result: dict[str, object] = {}
    for key in required:
        value = raw[key]
        if key in {"scorers", "faults"}:
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ComparisonError("condition metadata is invalid")
            result[key] = tuple(value)
        elif not isinstance(value, str) or not value:
            raise ComparisonError("condition metadata is invalid")
        else:
            result[key] = value
    return result


def _check_compatibility(
    baseline: _ComparableReport,
    candidate: _ComparableReport,
    allowed: frozenset[str],
) -> None:
    if baseline.benchmark_version != candidate.benchmark_version:
        raise ComparisonError("benchmark_version differs")
    for key in sorted(_FINGERPRINT_KEYS):
        if key == "code" and "code" in allowed:
            continue
        if baseline.fingerprints[key] != candidate.fingerprints[key]:
            raise ComparisonError(f"{key} fingerprint differs")
    comparisons = {
        "provider": ("provider",),
        "model": ("model",),
        "memory": ("memory_compaction", "memory_persistence"),
        "faults": ("faults",),
    }
    for variable, fields in comparisons.items():
        if variable in allowed:
            continue
        if any(baseline.condition[field] != candidate.condition[field] for field in fields):
            raise ComparisonError(f"{variable} differs but was not declared")
    for field in ("execution_kind", "scorers"):
        if baseline.condition[field] != candidate.condition[field]:
            raise ComparisonError(f"{field} differs")
    baseline_kinds = {trial.execution_kind for trial in baseline.trials}
    candidate_kinds = {trial.execution_kind for trial in candidate.trials}
    if len(baseline_kinds) > 1 or len(candidate_kinds) > 1 or baseline_kinds != candidate_kinds:
        raise ComparisonError("execution_kind rows cannot be mixed")


def _index_trials(trials: tuple[TrialRecord, ...]) -> dict[tuple[str, int], TrialRecord]:
    result: dict[tuple[str, int], TrialRecord] = {}
    for trial in trials:
        key = (trial.key.case_id, trial.key.repetition)
        if key in result:
            raise ComparisonError("duplicate case/repetition row")
        result[key] = trial
    return result


def _end_to_end(trial: TrialRecord) -> bool | None:
    primary = (
        trial.dimensions.artifact_correct
        if trial.dimensions.artifact_correct is not None
        else trial.dimensions.behavior_correct
    )
    values = (
        primary,
        trial.dimensions.agent_completed,
        trial.dimensions.scope_compliant,
        trial.dimensions.cleanup_confirmed,
    )
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True


def _metric(values: tuple[bool | None, ...]) -> dict[str, object]:
    known = tuple(value for value in values if value is not None)
    return {
        "numerator": sum(value is True for value in known),
        "denominator": len(values),
        "missing": len(values) - len(known),
        "value": _rate(known),
    }


def _rate(values: tuple[bool, ...]) -> float | None:
    return sum(value is True for value in values) / len(values) if values else None


def _failure_distribution(trials: tuple[TrialRecord, ...]) -> dict[str, int]:
    counts = Counter(
        trial.failure_stage or "none"
        for trial in trials
        if trial.status != "passed"
    )
    return dict(sorted(counts.items()))


def _usage_totals(
    trials: tuple[TrialRecord, ...], *, summary: bool
) -> dict[str, int | None]:
    fields = ("input_tokens", "output_tokens", "cached_tokens", "cache_miss_tokens")
    totals: dict[str, int | None] = {}
    for field in fields:
        values: list[int] = []
        complete = bool(trials)
        for trial in trials:
            usage = trial.summary_usage if summary else trial.usage
            value = getattr(usage, field) if usage is not None else None
            if value is None:
                complete = False
                break
            values.append(value)
        totals[field] = sum(values) if complete else None
    return totals


def _usage_delta(
    baseline: dict[str, int | None], candidate: dict[str, int | None]
) -> dict[str, int | None]:
    return {
        field: (
            candidate[field] - baseline[field]
            if candidate[field] is not None and baseline[field] is not None
            else None
        )
        for field in baseline
    }


def _read_json_report(path: Path) -> object:
    if not path.is_absolute():
        path = (Path.cwd() / path).absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ComparisonError("report path is invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComparisonError("report cannot be read") from exc


def _comparison_markdown(comparison: dict[str, object]) -> str:
    baseline = comparison["baseline_end_to_end"]
    candidate = comparison["candidate_end_to_end"]
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ComparisonError("comparison metric is invalid")
    return (
        "# Eval comparison\n\n"
        f"- Benchmark: {comparison['benchmark_version']}\n"
        f"- Paired: {comparison['paired']}\n"
        f"- Unpaired baseline: {comparison['unpaired_baseline']}\n"
        f"- Unpaired candidate: {comparison['unpaired_candidate']}\n"
        f"- Baseline end-to-end: {baseline['numerator']}/{baseline['denominator']}\n"
        f"- Candidate end-to-end: {candidate['numerator']}/{candidate['denominator']}\n"
        f"- Percentage-point delta: {comparison['end_to_end_percentage_point_delta']}\n"
        f"- Improvements: {comparison['improvements']}\n"
        f"- Regressions: {comparison['regressions']}\n"
    )


def _atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
