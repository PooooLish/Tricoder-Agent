"""Safe JSON and Markdown renderers for deterministic eval reports."""

from __future__ import annotations

import json
from dataclasses import asdict
import os
from pathlib import Path
import re
from statistics import median
import tempfile
from typing import Final

from tricoder.models import TokenUsage

from .metrics import MetricValue, aggregate_trial_metrics
from .assertions import AssertionResult, SubcaseResult
from .models import ExperimentRunReport, TrialDimensions, TrialKey, TrialRecord, EvalTurnRecord, TrialPlanEntry, TrialTimings
from .quantification import quantify_trials
from .faults import FAULT_EVIDENCE_KINDS
from .output import _is_link_or_reparse_point, validate_run_directory
from .runner import EvalCaseResult, EvalRunReport, VerificationResult


_SCHEMA_VERSION: Final = 1
_FAILURE_CODES: Final = frozenset(
    {
        "agent_failed",
        "agent_unverified",
        "verification_failed",
        "change_out_of_scope",
        "required_change_missing",
        "executor_error",
        "workspace_error",
        "verification_error",
        "cancelled",
        "budget_exhausted",
        "policy_denied",
        "fault_not_triggered",
        "recovery_failed",
    }
)
_VERIFICATION_ERROR_CODES: Final = frozenset(
    {"verification_policy_rejected", "verification_timeout", "verification_error"}
)
_IDENTIFIER_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_TIMESTAMP_PATTERN: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+")
_TRIAL_STATUSES: Final = frozenset(
    {"passed", "failed", "error", "cancelled", "not_run_budget", "not_run_cancelled"}
)
_EXECUTION_KINDS: Final = frozenset({"quality", "contract"})
_FAILURE_STAGES: Final = frozenset(
    {
        "definition", "workspace", "provider", "tool", "policy",
        "agent_budget", "agent_finish", "verifier", "cleanup", "report",
    }
)
_DIMENSION_FIELDS: Final = tuple(TrialDimensions.__dataclass_fields__)


def report_as_dict(report: EvalRunReport) -> dict[str, object]:
    """Return only the fixed, non-free-text fields safe to persist."""

    total_cases = len(report.cases)
    passed_cases = sum(case.status == "passed" for case in report.cases)
    return {
        "schema_version": _SCHEMA_VERSION,
        "run_id": _safe_identifier(report.run_id),
        "suite_id": _safe_identifier(report.suite_id),
        "provider": _safe_identifier(report.provider),
        "model": _safe_identifier(report.model),
        "started_at": _safe_timestamp(report.started_at),
        "duration_ms": report.duration_ms,
        "pass_rate": passed_cases / total_cases if total_cases else 0.0,
        "usage": _usage_as_dict(report.usage),
        "cases": [_case_as_dict(case) for case in report.cases],
    }


def normalize_report_payload(payload: object) -> dict[str, object]:
    """Adapt a persisted report into safe v2 comparison input.

    Legacy reports do not contain dimensional evidence.  Their fields remain
    explicitly unavailable instead of being guessed from the old pass bit.
    """

    if not isinstance(payload, dict):
        raise ValueError("eval report must be an object")
    version = payload.get("schema_version")
    if version != 1:
        raise ValueError("unsupported eval report schema")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("legacy eval report cases must be a list")
    trials: list[dict[str, object]] = []
    for item in raw_cases:
        if not isinstance(item, dict):
            raise ValueError("legacy eval report case must be an object")
        usage = item.get("usage")
        trials.append(
            {
                "condition_id": "legacy",
                "case_id": _safe_identifier(str(item.get("case_id", "invalid"))),
                "repetition": 1,
                "execution_kind": "unknown",
                "status": _safe_status(str(item.get("status", "error"))),
                "dimensions": "unavailable",
                "usage": _normalized_usage_payload(usage),
            }
        )
    return {
        "schema_version": 2,
        "source_schema_version": 1,
        "run_id": _safe_identifier(str(payload.get("run_id", "invalid"))),
        "suite_id": _safe_identifier(str(payload.get("suite_id", "invalid"))),
        "experiment": "unavailable",
        "dimensions": "unavailable",
        "trials": trials,
    }


def trial_record_as_dict(record: TrialRecord) -> dict[str, object]:
    """Serialize one trial using only fixed identifiers, counts and booleans."""

    TrialPlanEntry(record.key, record.execution_kind, record.category, record.split)
    turns = _read_turn_results([asdict(turn) for turn in record.turn_results])
    _validate_first_failed(turns, record.first_failed_turn)
    triggered = _read_fault_ids(list(record.triggered_faults))
    evidence = _read_fault_evidence([
        {"fault_id": fault, "kind": kind} for fault, kind in record.fault_evidence
    ], triggered)
    return {
        "condition_id": _safe_identifier(record.key.condition_id),
        "case_id": _safe_identifier(record.key.case_id),
        "repetition": record.key.repetition,
        "execution_kind": record.execution_kind,
        "category": _safe_identifier(record.category),
        "split": record.split,
        "timings": asdict(record.timings) if record.timings is not None else None,
        "status": record.status if record.status in _TRIAL_STATUSES else "error",
        "dimensions": {
            field: getattr(record.dimensions, field) for field in _DIMENSION_FIELDS
        },
        "failure_stage": (
            record.failure_stage if record.failure_stage in _FAILURE_STAGES else None
        ),
        "failure_codes": list(_safe_failure_codes(record.failure_codes)),
        "duration_ms": _safe_optional_count(record.duration_ms),
        "rounds": _safe_optional_count(record.rounds),
        "tool_calls": _safe_optional_count(record.tool_calls),
        "retries": _safe_optional_count(record.retries),
        "usage": _usage_as_dict(record.usage),
        "summary_usage": _usage_as_dict(record.summary_usage),
        "triggered_faults": list(triggered),
        "turn_results": [asdict(turn) for turn in turns],
        "first_failed_turn": record.first_failed_turn,
        "fault_evidence": [{"fault_id": fault, "kind": kind} for fault, kind in evidence],
        "recovery_path": (
            _safe_identifier(record.recovery_path)
            if record.recovery_path is not None
            else None
        ),
        "summary_triggered": record.summary_triggered,
        "summary_count": _safe_optional_count(record.summary_count),
        "dangerous_actions_proposed": _safe_optional_count(record.dangerous_actions_proposed),
        "dangerous_actions_executed": _safe_optional_count(record.dangerous_actions_executed),
        "safety_bypass_attempts": _safe_optional_count(record.safety_bypass_attempts),
        "legitimate_actions_attempted": _safe_optional_count(record.legitimate_actions_attempted),
        "legitimate_actions_allowed": _safe_optional_count(record.legitimate_actions_allowed),
        "expectation_met": record.expectation_met,
        "assertions": [
            {
                "id": _safe_identifier(item.id),
                "passed": item.passed,
                "reason_code": _safe_identifier(item.reason_code),
                "evidence_refs": [_safe_identifier(ref) for ref in item.evidence_refs],
            }
            for item in record.assertion_results
        ],
        "subcases": [
            {
                "id": _safe_identifier(item.id),
                "expectation_met": item.expectation_met,
                "agent_completed": item.agent_completed,
                "reason_code": _safe_identifier(item.reason_code),
            }
            for item in record.subcase_results
        ],
    }


def trial_record_from_dict(payload: object) -> TrialRecord:
    """Strictly read a persisted v2 trial row without accepting free text."""

    if not isinstance(payload, dict):
        raise ValueError("trial result must be an object")
    condition_id = _require_safe_identifier(payload.get("condition_id"))
    case_id = _require_safe_identifier(payload.get("case_id"))
    repetition = _require_positive_count(payload.get("repetition"))
    execution_kind = payload.get("execution_kind")
    status = payload.get("status")
    if execution_kind not in _EXECUTION_KINDS or status not in _TRIAL_STATUSES:
        raise ValueError("trial result enum is invalid")
    raw_dimensions = payload.get("dimensions")
    if not isinstance(raw_dimensions, dict) or set(raw_dimensions) != set(_DIMENSION_FIELDS):
        raise ValueError("trial dimensions are invalid")
    dimensions: dict[str, bool | None] = {}
    for field in _DIMENSION_FIELDS:
        value = raw_dimensions[field]
        if value is not None and not isinstance(value, bool):
            raise ValueError("trial dimension must be boolean or null")
        dimensions[field] = value
    failure_stage = payload.get("failure_stage")
    if failure_stage is not None and failure_stage not in _FAILURE_STAGES:
        raise ValueError("trial failure stage is invalid")
    raw_expectation = payload.get("expectation_met")
    if raw_expectation is not None and not isinstance(raw_expectation, bool):
        raise ValueError("trial expectation must be boolean or null")
    raw_assertions = payload.get("assertions", [])
    if not isinstance(raw_assertions, list):
        raise ValueError("trial assertions must be a list")
    assertion_results: list[AssertionResult] = []
    for item in raw_assertions:
        if not isinstance(item, dict) or set(item) != {
            "id", "passed", "reason_code", "evidence_refs"
        }:
            raise ValueError("trial assertion is invalid")
        passed = item["passed"]
        if passed is not None and not isinstance(passed, bool):
            raise ValueError("trial assertion result is invalid")
        assertion_results.append(
            AssertionResult(
                _require_safe_identifier(item["id"]),
                passed,
                _require_safe_identifier(item["reason_code"]),
                _read_identifiers(item["evidence_refs"]),
            )
        )
    raw_subcases = payload.get("subcases", [])
    if not isinstance(raw_subcases, list):
        raise ValueError("trial subcases must be a list")
    subcase_results: list[SubcaseResult] = []
    for item in raw_subcases:
        if not isinstance(item, dict) or set(item) != {
            "id", "expectation_met", "agent_completed", "reason_code"
        }:
            raise ValueError("trial subcase is invalid")
        if type(item["expectation_met"]) is not bool or type(item["agent_completed"]) is not bool:
            raise ValueError("trial subcase booleans are invalid")
        subcase_results.append(
            SubcaseResult(
                _require_safe_identifier(item["id"]),
                item["expectation_met"],
                item["agent_completed"],
                _require_safe_identifier(item["reason_code"]),
            )
        )
    turns = _read_turn_results(payload.get("turn_results", []))
    first_failed = _read_optional_count(payload.get("first_failed_turn"))
    _validate_first_failed(turns, first_failed)
    triggered = _read_fault_ids(payload.get("triggered_faults", []))
    evidence = _read_fault_evidence(payload.get("fault_evidence", []), triggered)
    split = payload.get("split", "unknown")
    TrialPlanEntry(TrialKey(condition_id, case_id, repetition), execution_kind,
                   _read_optional_identifier(payload.get("category")) or "unknown", split)
    timing_payload = payload.get("timings")
    timings = None
    if timing_payload is not None:
        if not isinstance(timing_payload, dict) or set(timing_payload) != set(TrialTimings.__dataclass_fields__):
            raise ValueError("invalid trial timings")
        timings = TrialTimings(**timing_payload)
    return TrialRecord(
        key=TrialKey(condition_id, case_id, repetition),
        execution_kind=execution_kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        dimensions=TrialDimensions(**dimensions),
        failure_stage=failure_stage,  # type: ignore[arg-type]
        failure_codes=_read_failure_codes(payload.get("failure_codes")),
        duration_ms=_read_optional_count(payload.get("duration_ms")),
        rounds=_read_optional_count(payload.get("rounds")),
        tool_calls=_read_optional_count(payload.get("tool_calls")),
        retries=_read_optional_count(payload.get("retries")),
        usage=_read_usage(payload.get("usage")),
        summary_usage=_read_usage(payload.get("summary_usage")),
        triggered_faults=triggered,
        turn_results=turns,
        first_failed_turn=first_failed,
        fault_evidence=evidence,
        recovery_path=_read_optional_identifier(payload.get("recovery_path")),
        summary_triggered=_read_optional_bool(payload.get("summary_triggered")),
        summary_count=_read_optional_count(payload.get("summary_count")),
        dangerous_actions_proposed=_read_optional_count(payload.get("dangerous_actions_proposed")),
        dangerous_actions_executed=_read_optional_count(payload.get("dangerous_actions_executed")),
        safety_bypass_attempts=_read_optional_count(payload.get("safety_bypass_attempts")),
        legitimate_actions_attempted=_read_optional_count(payload.get("legitimate_actions_attempted")),
        legitimate_actions_allowed=_read_optional_count(payload.get("legitimate_actions_allowed")),
        category=_read_optional_identifier(payload.get("category")) or "unknown",
        expectation_met=raw_expectation,
        assertion_results=tuple(assertion_results),
        subcase_results=tuple(subcase_results),
        split=split,
        timings=timings,
    )


def _read_turn_results(raw: object) -> tuple[EvalTurnRecord, ...]:
    if not isinstance(raw, list):
        raise ValueError("trial turns must be a list")
    turns = []
    fields = set(EvalTurnRecord.__dataclass_fields__)
    optional = {"finish_outcome", "stop_reason", "validation_status", "unresolved_check_count", "verification_required"}
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not fields - optional <= set(item) or not set(item) <= fields:
            raise ValueError("trial turn fields are invalid")
        turn = EvalTurnRecord(**item)
        if turn.turn_index != index:
            raise ValueError("trial turn order is invalid")
        turns.append(turn)
    return tuple(turns)


def _validate_first_failed(turns: tuple[EvalTurnRecord, ...], first: int | None) -> None:
    if first is not None and (type(first) is not int or first < 1):
        raise ValueError("first failed turn is invalid")
    if first != next((turn.turn_index for turn in turns if not turn.ok), None):
        raise ValueError("first failed turn is inconsistent")


def _read_fault_ids(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(value, str) or value not in FAULT_EVIDENCE_KINDS for value in raw):
        raise ValueError("trial fault ID is invalid")
    if len(set(raw)) != len(raw):
        raise ValueError("trial fault ID is duplicated")
    return tuple(raw)


def _read_fault_evidence(raw: object, triggered: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise ValueError("fault evidence must be a list")
    evidence = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"fault_id", "kind"}:
            raise ValueError("fault evidence fields are invalid")
        fault, kind = item["fault_id"], item["kind"]
        if (not isinstance(fault, str) or fault not in triggered
            or FAULT_EVIDENCE_KINDS.get(fault) != kind
            or any(previous == fault for previous, _ in evidence)):
            raise ValueError("fault evidence is invalid")
        evidence.append((fault, kind))
    return tuple(evidence)


def experiment_report_as_dict(report: ExperimentRunReport) -> dict[str, object]:
    metrics = aggregate_trial_metrics(report.trials)
    execution_groups = {
        kind: tuple(trial for trial in report.trials if trial.execution_kind == kind)
        for kind in sorted({trial.execution_kind for trial in report.trials})
    }
    category_groups = {
        category: tuple(trial for trial in report.trials if trial.category == category)
        for category in sorted({trial.category for trial in report.trials})
    }
    combined_dimensions: object = (
        _dimension_metrics(report.trials)
        if len(execution_groups) <= 1
        else "mixed_not_aggregated"
    )
    return {
        "schema_version": 2,
        "run_id": _safe_identifier(report.run_id),
        "experiment_id": _safe_identifier(report.experiment_id),
        "suite_id": _safe_identifier(report.suite_id),
        "benchmark_version": _safe_identifier(report.benchmark_version),
        "started_at": _safe_timestamp(report.started_at),
        "duration_ms": report.duration_ms,
        "planned_trials": report.planned_trials,
        "complete": report.complete,
        "conditions": [_condition_as_dict(condition) for condition in report.conditions],
        "fingerprints": {
            _safe_identifier(key): _safe_identifier(value)
            for key, value in report.fingerprints
        },
        "coverage": {
            "recorded": len(report.trials),
            "not_run": metrics.not_run,
            "unverified": metrics.unverified,
            "infrastructure_errors": metrics.infrastructure_errors,
            "usage_observed": metrics.usage_observed,
            "summary_usage_observed": metrics.summary_usage_observed,
        },
        "dimensions": combined_dimensions,
        "by_execution_kind": {
            kind: _group_metrics(trials) for kind, trials in execution_groups.items()
        },
        "by_category": {
            category: _group_metrics(trials) for category, trials in category_groups.items()
        },
        "failure_distribution": _failure_distribution(report.trials),
        "efficiency": _efficiency_metrics(report.trials),
        "usage": _usage_as_dict(metrics.total_usage),
        "summary_usage": _usage_as_dict(metrics.total_summary_usage),
        "cost_per_success": metrics.cost_per_success,
        "trials": [trial_record_as_dict(record) for record in report.trials],
        "trial_plan": [trial_plan_as_dict(entry) for entry in report.trial_plan],
        "quantification": quantify_trials(report.trials, plan=report.trial_plan or None,
                                           report_complete=report.complete),
    }


def trial_plan_as_dict(entry: TrialPlanEntry) -> dict[str, object]:
    return {"condition_id": entry.key.condition_id, "case_id": entry.key.case_id,
            "repetition": entry.key.repetition, "execution_kind": entry.execution_kind,
            "category": entry.category, "split": entry.split}


def _condition_as_dict(condition: object) -> dict[str, object]:
    return {
        "id": _safe_identifier(getattr(condition, "id")),
        "provider": _safe_identifier(getattr(condition, "provider")),
        "model": _safe_identifier(getattr(condition, "model")),
        "execution_kind": getattr(condition, "execution_kind"),
        "memory_compaction": getattr(condition, "memory_compaction"),
        "memory_persistence": getattr(condition, "memory_persistence"),
        "scorers": [_safe_identifier(value) for value in getattr(condition, "scorers")],
        "faults": [_safe_identifier(value) for value in getattr(condition, "faults")],
    }


def _dimension_metrics(trials: tuple[TrialRecord, ...]) -> dict[str, object]:
    metrics = aggregate_trial_metrics(trials)
    return {
        "code_correct_planned": _metric_as_dict(metrics.code_correct_planned),
        "code_correct_verified": _metric_as_dict(metrics.code_correct_verified),
        "end_to_end": _metric_as_dict(metrics.end_to_end),
        "normal_finish": _metric_as_dict(metrics.normal_finish),
        "expectation_met": _metric_as_dict(metrics.expectation_met),
        "recovery": _bool_metric(
            tuple(
                trial.dimensions.recovered
                for trial in trials
                if trial.dimensions.fault_triggered is True
            )
        ),
        "safety_blocked": _bool_metric(
            tuple(
                trial.dimensions.safety_blocked
                for trial in trials
                if trial.dimensions.safety_triggered is True
            )
        ),
        "legitimate_action_allowed": _bool_metric(
            tuple(
                trial.dimensions.legitimate_action_allowed
                for trial in trials
                if trial.dimensions.legitimate_action_allowed is not None
            )
        ),
        "memory_constraint_preserved": _applicable_dimension(
            trials, "memory_constraint_preserved"
        ),
        "memory_latest_correction_used": _applicable_dimension(
            trials, "memory_latest_correction_used"
        ),
        "memory_stale_state_avoided": _applicable_dimension(
            trials, "memory_stale_state_avoided"
        ),
        "memory_session_isolated": _applicable_dimension(
            trials, "memory_session_isolated"
        ),
        "stable_cases": _stable_cases(trials),
    }


def _group_metrics(trials: tuple[TrialRecord, ...]) -> dict[str, object]:
    return {
        "planned": len(trials),
        "dimensions": _dimension_metrics(trials),
        "safety": {
            "dangerous_actions_proposed": _sum_known(trials, "dangerous_actions_proposed"),
            "dangerous_actions_executed": _sum_known(trials, "dangerous_actions_executed"),
            "safety_bypass_attempts": _sum_known(trials, "safety_bypass_attempts"),
            "legitimate_actions_attempted": _sum_known(trials, "legitimate_actions_attempted"),
            "legitimate_actions_allowed": _sum_known(trials, "legitimate_actions_allowed"),
        },
        "efficiency": _efficiency_metrics(trials),
    }


def _applicable_dimension(trials: tuple[TrialRecord, ...], field: str) -> dict[str, object]:
    values = tuple(
        value
        for trial in trials
        if (value := getattr(trial.dimensions, field)) is not None
    )
    return _bool_metric(values)


def _bool_metric(values: tuple[bool | None, ...]) -> dict[str, object]:
    metric = MetricValue.from_values(values)
    return _metric_as_dict(metric)


def _sum_known(trials: tuple[TrialRecord, ...], field: str) -> int | None:
    values = [getattr(trial, field) for trial in trials]
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _stable_cases(trials: tuple[TrialRecord, ...]) -> dict[str, object]:
    grouped: dict[str, list[TrialRecord]] = {}
    for trial in trials:
        grouped.setdefault(trial.key.case_id, []).append(trial)
    values = tuple(
        bool(rows) and all(_trial_end_to_end(row) is True for row in rows)
        for rows in grouped.values()
    )
    return _bool_metric(values)


def _trial_end_to_end(trial: TrialRecord) -> bool | None:
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


def _failure_distribution(trials: tuple[TrialRecord, ...]) -> dict[str, object]:
    stages: dict[str, int] = {}
    codes: dict[str, int] = {}
    for trial in trials:
        if trial.failure_stage is not None:
            stages[trial.failure_stage] = stages.get(trial.failure_stage, 0) + 1
        for code in trial.failure_codes:
            codes[code] = codes.get(code, 0) + 1
    return {"primary_stage": dict(sorted(stages.items())), "all_codes": dict(sorted(codes.items()))}


def _efficiency_metrics(trials: tuple[TrialRecord, ...]) -> dict[str, object]:
    return {
        "all_started": _efficiency_subset(
            tuple(trial for trial in trials if not trial.status.startswith("not_run_"))
        ),
        "successful": _efficiency_subset(
            tuple(trial for trial in trials if trial.status == "passed")
        ),
    }


def _efficiency_subset(trials: tuple[TrialRecord, ...]) -> dict[str, object]:
    def series(field: str) -> list[int]:
        return [value for trial in trials if (value := getattr(trial, field)) is not None]

    def summary(values: list[int]) -> dict[str, object]:
        ordered = sorted(values)
        p95_index = max(0, (95 * len(ordered) + 99) // 100 - 1) if ordered else 0
        return {
            "observed": len(values),
            "median": median(values) if values else None,
            "p95": ordered[p95_index] if ordered else None,
        }

    return {
        "trials": len(trials),
        "duration_ms": summary(series("duration_ms")),
        "rounds": summary(series("rounds")),
        "tool_calls": summary(series("tool_calls")),
        "retries": summary(series("retries")),
    }


def write_experiment_reports(
    report: ExperimentRunReport, run_dir: Path
) -> tuple[Path, Path]:
    directory = validate_run_directory(Path.cwd(), run_dir, require_empty=False, create=False)
    json_path = _output_path(directory, "result.json")
    markdown_path = _output_path(directory, "report.md")
    payload = experiment_report_as_dict(report)
    _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(markdown_path, _render_experiment_markdown(payload))
    return json_path, markdown_path


def render_markdown(report: EvalRunReport) -> str:
    """Render the case-level safe metrics as a compact Markdown table."""

    lines = [
        "# Eval report",
        "",
        "| Case ID | Status | Duration (ms) | Rounds | Tool calls | Modified files | Verification | Failure codes |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for case in report.cases:
        verifications = _verification_summary(case.verifications)
        failure_codes = ", ".join(_safe_failure_codes(case.failure_codes)) or "-"
        lines.append(
            "| "
            + " | ".join(
                (
                    _safe_identifier(case.case_id),
                    _safe_status(case.status),
                    str(case.duration_ms),
                    str(case.rounds),
                    str(case.tool_calls),
                    str(len(case.modified_files)),
                    verifications,
                    failure_codes,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def write_reports(report: EvalRunReport, run_dir: Path) -> tuple[Path, Path]:
    """Atomically write fixed report files within an absolute run directory."""

    directory = validate_run_directory(
        Path.cwd(),
        run_dir,
        require_empty=False,
        create=True,
    )
    json_path = _output_path(directory, "result.json")
    markdown_path = _output_path(directory, "report.md")
    _atomic_write(json_path, json.dumps(report_as_dict(report), ensure_ascii=False, indent=2) + "\n")
    _atomic_write(markdown_path, render_markdown(report))
    return json_path, markdown_path


def _case_as_dict(case: EvalCaseResult) -> dict[str, object]:
    return {
        "case_id": _safe_identifier(case.case_id),
        "status": _safe_status(case.status),
        "failure_codes": list(_safe_failure_codes(case.failure_codes)),
        "duration_ms": case.duration_ms,
        "rounds": case.rounds,
        "tool_calls": case.tool_calls,
        "modified_file_count": len(case.modified_files),
        "verifications": [_verification_as_dict(result) for result in case.verifications],
        "agent_verification": "passed" if case.agent_verification == "通过" else "not_passed",
        "usage": _usage_as_dict(case.usage),
    }


def _verification_as_dict(result: VerificationResult) -> dict[str, object]:
    return {
        "exit_code": result.exit_code,
        "passed": result.passed,
        "error_code": _safe_verification_error_code(result.error_code),
    }


def _usage_as_dict(usage: TokenUsage | None) -> dict[str, int | None]:
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "cache_miss_tokens": None,
        }
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_tokens": usage.cached_tokens,
        "cache_miss_tokens": usage.cache_miss_tokens,
    }


def _normalized_usage_payload(value: object) -> dict[str, int | None]:
    fields = ("input_tokens", "output_tokens", "cached_tokens", "cache_miss_tokens")
    if not isinstance(value, dict):
        return {field: None for field in fields}
    return {
        field: (
            raw
            if isinstance((raw := value.get(field)), int)
            and not isinstance(raw, bool)
            and raw >= 0
            else None
        )
        for field in fields
    }


def _metric_as_dict(metric: MetricValue) -> dict[str, object]:
    return {
        "numerator": metric.numerator,
        "denominator": metric.denominator,
        "missing": metric.missing,
        "value": metric.value,
    }


def _render_experiment_markdown(payload: dict[str, object]) -> str:
    lines = [
        "# Eval experiment report",
        "",
        f"- Complete: {str(payload['complete']).lower()}",
        f"- Planned trials: {payload['planned_trials']}",
        f"- Benchmark: {payload['benchmark_version']}",
        "- Cost per success: unavailable (no complete local price table)",
        "- retries 包含模拟恢复计数，不代表实际 HTTP 重试次数；未触发故障时 recovered 为 unknown。",
        "- memory off/off 只测短多轮行为，不证明持久记忆或压缩质量。",
        "",
        "## Conditions",
        "",
        "| ID | Provider | Model | Kind | Memory | Faults |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    raw_conditions = payload["conditions"]
    if not isinstance(raw_conditions, list):
        raise ValueError("experiment conditions must be a list")
    for condition in raw_conditions:
        if not isinstance(condition, dict):
            raise ValueError("experiment condition must be an object")
        lines.append(
            f"| {condition['id']} | {condition['provider']} | {condition['model']} | "
            f"{condition['execution_kind']} | {condition['memory_compaction']}/"
            f"{condition['memory_persistence']} | {','.join(condition['faults'])} |"
        )
    lines.extend([
        "",
        "## Category metrics",
        "",
        "| Category | Planned | Expectation | Artifact/E2E | Normal finish |",
        "| --- | ---: | --- | --- | --- |",
    ])
    raw_categories = payload["by_category"]
    if not isinstance(raw_categories, dict):
        raise ValueError("experiment categories must be an object")
    for category, group in raw_categories.items():
        if not isinstance(group, dict) or not isinstance(group.get("dimensions"), dict):
            raise ValueError("experiment category metrics are invalid")
        dimensions = group["dimensions"]
        assert isinstance(dimensions, dict)
        end_to_end = dimensions["end_to_end"]
        normal_finish = dimensions["normal_finish"]
        expectation = dimensions["expectation_met"]
        assert (
            isinstance(end_to_end, dict)
            and isinstance(normal_finish, dict)
            and isinstance(expectation, dict)
        )
        lines.append(
            f"| {category} | {group['planned']} | "
            f"{expectation['numerator']}/{expectation['denominator']} | "
            f"{end_to_end['numerator']}/{end_to_end['denominator']} | "
            f"{normal_finish['numerator']}/{normal_finish['denominator']} |"
        )
    lines.extend([
        "",
        "## Trials",
        "",
        "| Condition | Case | Repetition | Kind | Status | Expected | Artifact | Agent | Scope | Cleanup | First failed turn |",
        "| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | ---: |",
    ])
    raw_trials = payload["trials"]
    if not isinstance(raw_trials, list):
        raise ValueError("experiment trials must be a list")
    for trial in raw_trials:
        if not isinstance(trial, dict):
            raise ValueError("experiment trial must be an object")
        safe = trial_record_from_dict(trial)
        lines.append(
            f"| {trial['condition_id']} | {trial['case_id']} | {trial['repetition']} | "
            f"{trial['execution_kind']} | {trial['status']} | {trial['expectation_met']} | "
            f"{safe.dimensions.artifact_correct} | {safe.dimensions.agent_completed} | "
            f"{safe.dimensions.scope_compliant} | {safe.dimensions.cleanup_confirmed} | {safe.first_failed_turn} |"
        )
    lines.extend(["", "## User turns", "", "| Condition | Case | Repetition | Turn | OK | Verification | Rounds | Tools | Terminal tool error |",
                  "| --- | --- | ---: | ---: | --- | --- | ---: | ---: | --- |"])
    for trial in raw_trials:
        safe = trial_record_from_dict(trial)
        for turn in safe.turn_results:
            lines.append(f"| {safe.key.condition_id} | {safe.key.case_id} | {safe.key.repetition} | {turn.turn_index} | {turn.ok} | {turn.verification} | {turn.rounds} | {turn.tool_calls} | {turn.terminal_tool_error} |")
        if not safe.turn_results:
            lines.append(f"| {safe.key.condition_id} | {safe.key.case_id} | {safe.key.repetition} | - | unknown | unknown | - | - | unknown |")
    lines.extend(["", "## Delivery diagnostics", "", "| Condition | Case | Repetition | Turn | Finish outcome | OK | Stop reason | Validation status | Unresolved checks | Verification required |",
                  "| --- | --- | ---: | ---: | --- | --- | --- | --- | ---: | --- |"])
    for trial in raw_trials:
        safe = trial_record_from_dict(trial)
        identity = f"| {safe.key.condition_id} | {safe.key.case_id} | {safe.key.repetition} |"
        for turn in safe.turn_results:
            values = (turn.turn_index, turn.finish_outcome, turn.ok, turn.stop_reason,
                      turn.validation_status, turn.unresolved_check_count, turn.verification_required)
            lines.append(identity + " " + " | ".join("unknown" if value is None else str(value) for value in values) + " |")
        if not safe.turn_results:
            lines.append(identity + " - | unknown | unknown | unknown | unknown | unknown | unknown |")
    lines.extend(["", "## Fault evidence", "", "| Condition | Case | Repetition | Triggered fault | Evidence kind |", "| --- | --- | ---: | --- | --- |"])
    for trial in raw_trials:
        safe = trial_record_from_dict(trial)
        evidence = dict(safe.fault_evidence)
        for fault in safe.triggered_faults:
            lines.append(f"| {safe.key.condition_id} | {safe.key.case_id} | {safe.key.repetition} | {fault} | {evidence.get(fault, '证据类型未知')} |")
    subcase_rows = [
        (trial, subcase)
        for trial in raw_trials
        if isinstance(trial, dict) and isinstance(trial.get("subcases"), list)
        for subcase in trial["subcases"]
        if isinstance(subcase, dict)
    ]
    if subcase_rows:
        lines.extend([
            "",
            "## Subcases",
            "",
            "| Condition | Case | Repetition | Subcase | Expected | Agent completed | Reason |",
            "| --- | --- | ---: | --- | --- | --- | --- |",
        ])
        for trial, subcase in subcase_rows:
            lines.append(
                f"| {trial['condition_id']} | {trial['case_id']} | {trial['repetition']} | "
                f"{subcase['id']} | {subcase['expectation_met']} | "
                f"{subcase['agent_completed']} | {subcase['reason_code']} |"
            )
    if "quantification" in payload:
        lines.append(render_quantification_markdown(payload["quantification"]))
    return "\n".join(lines) + "\n"


def render_quantification_markdown(q: dict[str, object]) -> str:
    lines = ["", "## Whole-task quantification", "",
             "token_accounting=provider_reported; billing_completeness=unknown. Tokens are not monetary costs.",
             "legacy stable_cases = observed-all-success, not pass^k. legacy_eval_duration_ms is not Agent latency.",
             "Agent time includes Session, tools, waits and cleanup; multi-turn rounds may exceed per-turn budgets.",
             "error samples count as conservative end-to-end failures; unknown outcomes remain unavailable."]
    for condition, block in q['by_condition'].items():
        groups = [('all_quality', block['all_quality'])]
        groups += [('split/'+k,v) for k,v in block['by_split'].items()]
        groups += [('category/'+k,v) for k,v in block['by_category'].items()]
        for name, group in groups:
            lines += ['', f"### {condition} / {name}", '',
                      f"Coverage: {json.dumps(group['coverage'], sort_keys=True)}", '',
                      '| Metric | k | Value | Eligible / tasks | Reason |',
                      '| --- | ---: | ---: | ---: | --- |']
            for key,label in (('artifact_pass_at_k','artifact pass@'),('delivery_pass_at_k','delivery pass@'),('delivery_pass_power_k','delivery pass^')):
                for k,v in group[key].items():
                    value = 'unavailable' if v['value'] is None else f"{v['value']:.6f}"
                    lines.append(f"| {label}{k} | {k} | {value} | {v['tasks_eligible']}/{v['tasks_total']} | {v['reason'] or '-'} |")
            lines += ['', '| Case | n planned | n started | Artifact successes | Delivery successes | Unknown artifact / delivery |',
                      '| --- | ---: | ---: | ---: | ---: | --- |']
            for c in group['case_results']:
                lines.append(f"| {c['case_id']} | {c['n_planned']} | {c['n_started']} | {c['c_artifact']} | {c['c_delivery']} | {c['unknown_artifact']}/{c['unknown_delivery']} |")
            lines += ['', 'Success histograms: '+json.dumps(group['success_histograms'],sort_keys=True), '',
                      '| Subset | Channel / field | observed / missing | Strict sum | Mean | Median | P90 | P95 | Max |',
                      '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
            for subset in ('all_started','successful','unsuccessful','unknown_outcome'):
                e=group['efficiency'][subset]
                fields=[(f'{channel}/{field}',v) for channel in ('main','summary') for field,v in e[channel].items() if field!='cache_ratio']
                fields += [('timings/'+field,v) for field,v in e['timings'].items()]
                fields += [(field,e[field]) for field in ('legacy_eval_duration_ms','rounds','tool_calls','retries')]
                for field,v in fields:
                    cells=['unavailable' if v[f] is None else str(v[f]) for f in ('strict_sum','mean','median','p90','p95','max')]
                    lines.append(f"| {subset} | {field} | {v['observed']}/{v['missing']} | "+' | '.join(cells)+' |')
            lines += ['', 'Main reported tokens per delivery success: '+json.dumps(group['efficiency']['main_reported_tokens_per_delivery_success']),
                      'Main cache coverage: '+json.dumps(group['efficiency']['all_started']['main']['cache_ratio'])]
    return '\n'.join(lines)+'\n'


def _safe_optional_count(value: int | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _require_safe_identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError("trial identifier is invalid")
    return value


def _require_positive_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("trial count is invalid")
    return value


def _read_optional_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("trial count is invalid")
    return value


def _read_optional_bool(value: object) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("trial boolean is invalid")


def _read_failure_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("trial failure codes are invalid")
    filtered = _safe_failure_codes(tuple(value))
    if len(filtered) != len(value):
        raise ValueError("trial failure code is invalid")
    return filtered


def _read_identifiers(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("trial identifiers are invalid")
    return tuple(_require_safe_identifier(item) for item in value)


def _read_optional_identifier(value: object) -> str | None:
    return None if value is None else _require_safe_identifier(value)


def _read_usage(value: object) -> TokenUsage | None:
    normalized = _normalized_usage_payload(value)
    if all(item is None for item in normalized.values()):
        return None
    return TokenUsage(**normalized)


def _safe_failure_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(code for code in codes if code in _FAILURE_CODES)


def _safe_status(status: str) -> str:
    return status if status in {"passed", "failed", "error"} else "error"


def _safe_identifier(value: str) -> str:
    return value if _IDENTIFIER_PATTERN.fullmatch(value) else "invalid"


def _safe_timestamp(value: str) -> str:
    return value if _TIMESTAMP_PATTERN.fullmatch(value) else "unknown"


def _safe_verification_error_code(value: str | None) -> str | None:
    if value is None or value in _VERIFICATION_ERROR_CODES:
        return value
    return "verification_error"


def _verification_summary(results: tuple[VerificationResult, ...]) -> str:
    if not results:
        return "-"
    return "passed" if all(result.passed for result in results) else "failed"


def _output_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if path.exists() and _is_link_or_reparse_point(path):
        raise ValueError("report output cannot be a link or reparse point")
    return path


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
