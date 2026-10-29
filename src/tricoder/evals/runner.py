"""Isolated execution and deterministic scoring for eval suites."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import time
from typing import Literal, TypeAlias

from tricoder.models import RunResult, TokenUsage
from tricoder.policy import CommandPolicy, PolicyError
from tricoder.process.control import run_bounded_process
from tricoder.task_cleanup import current_cleanup, TaskCleanup

from .cleanup import (retain_failure, verifier_scope, verification_cleanup_owner,
                      verification_owner_scope)
from .loader import is_reserved_eval_path, is_workspace_relative_path
from .models import EvalCase, EvalSuite, PathMatchMode, VerificationSpec, TrialTimings
from .output import validate_run_directory
from .workspace import (
    FileFingerprint,
    WorkspaceSafetyError,
    capture_snapshot,
    changed_paths,
    install_verifier,
    prepare_workspace,
    remove_verifier,
)


CaseStatus: TypeAlias = Literal["passed", "failed", "error"]
AgentExecutor: TypeAlias = Callable[[EvalCase, Path, Path], RunResult]

_ERROR_FAILURE_CODES = frozenset(
    {"executor_error", "workspace_error", "verification_error"}
)
_VERIFICATION_MAX_OUTPUT_BYTES = 4096


@dataclass(frozen=True, slots=True)
class VerificationResult:
    name: str
    exit_code: int | None
    passed: bool
    error_code: str | None
    diagnostics: dict[str, object] | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    case_id: str
    status: CaseStatus
    failure_codes: tuple[str, ...]
    duration_ms: int
    rounds: int
    tool_calls: int
    modified_files: tuple[str, ...]
    verifications: tuple[VerificationResult, ...]
    agent_verification: str
    usage: TokenUsage | None
    timings: TrialTimings | None = None


@dataclass(frozen=True, slots=True)
class EvalRunReport:
    run_id: str
    suite_id: str
    provider: str
    model: str
    started_at: str
    duration_ms: int
    cases: tuple[EvalCaseResult, ...]
    usage: TokenUsage | None


def run_suite(
    suite: EvalSuite,
    run_dir: Path,
    provider: str,
    model: str,
    agent_executor: AgentExecutor,
    *,
    cleanup_owner: TaskCleanup | None = None,
) -> EvalRunReport:
    """Run every case independently and return a safe structured report."""

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    run_dir = validate_run_directory(
        Path.cwd(),
        run_dir,
        require_empty=True,
    )
    workspaces_root = run_dir / "workspaces"
    audit_root = run_dir / "audit"
    workspaces_root.mkdir()
    audit_root.mkdir()

    with verification_owner_scope(cleanup_owner):
        cases = tuple(
            _run_case(case, workspaces_root, audit_root, agent_executor)
            for case in suite.cases
        )
    return EvalRunReport(
        run_id=run_dir.name,
        suite_id=suite.id,
        provider=provider,
        model=model,
        started_at=started_at,
        duration_ms=_elapsed_ms(started),
        cases=cases,
        usage=_merge_usage(cases),
    )


def run_isolated_case(
    case: EvalCase,
    trial_dir: Path,
    agent_executor: AgentExecutor,
    *,
    cleanup_owner: TaskCleanup | None = None,
) -> EvalCaseResult:
    """Run one case inside a scheduler-owned, initially empty trial directory."""

    if not trial_dir.is_absolute() or not trial_dir.is_dir() or any(trial_dir.iterdir()):
        raise ValueError("trial directory must be an empty absolute directory")
    workspaces_root = trial_dir / "workspaces"
    audit_root = trial_dir / "audit"
    workspaces_root.mkdir()
    audit_root.mkdir()
    with verification_owner_scope(cleanup_owner):
        return _run_case(case, workspaces_root, audit_root, agent_executor)


def _run_case(
    case: EvalCase,
    workspaces_root: Path,
    audit_root: Path,
    agent_executor: AgentExecutor,
) -> EvalCaseResult:
    observation = _TimingObservation()
    if _verification_cleanup_pending():
        result = _case_result(case, time.perf_counter(), ("verification_error",),
                              modified_files=(), verifications=(), run_result=None)
        return replace(result, timings=observation.finish())
    result = _run_case_observed(case, workspaces_root, audit_root, agent_executor, observation)
    return replace(result, timings=observation.finish())


class _TimingObservation:
    """Monotonic observations only; never a resource owner or deadline."""

    def __init__(self) -> None:
        self.started = time.perf_counter_ns()
        self.agent_ns = 0
        self.verifier_ns = 0

    @contextmanager
    def phase(self, field: str):
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            setattr(self, field, getattr(self, field) + time.perf_counter_ns() - started)

    def finish(self) -> TrialTimings:
        total = (time.perf_counter_ns() - self.started) // 1_000_000
        agent = self.agent_ns // 1_000_000
        verifier = self.verifier_ns // 1_000_000
        return TrialTimings(total, agent, verifier, total-agent-verifier)


def _run_case_observed(
    case: EvalCase,
    workspaces_root: Path,
    audit_root: Path,
    agent_executor: AgentExecutor,
    observation: _TimingObservation,
) -> EvalCaseResult:
    started = time.perf_counter()
    workspace: Path | None = None
    run_result: RunResult | None = None
    modified_files: tuple[str, ...] = ()
    verifications: tuple[VerificationResult, ...] = ()

    try:
        workspace = prepare_workspace(case, workspaces_root)
        before = capture_snapshot(workspace)
    except (OSError, WorkspaceSafetyError):
        return _case_result(
            case,
            started,
            ("workspace_error",),
            modified_files=modified_files,
            verifications=verifications,
            run_result=run_result,
        )

    cleanup_failed = False
    try:
        result = _run_prepared_case(
            case=case,
            started=started,
            workspace=workspace,
            before=before,
            audit_root=audit_root,
            agent_executor=agent_executor,
            observation=observation,
        )
    finally:
        try:
            remove_verifier(workspace)
        except (OSError, WorkspaceSafetyError):
            cleanup_failed = True
    if cleanup_failed:
        return _case_result(
            case,
            started,
            ("workspace_error",),
            modified_files=(),
            verifications=(),
            run_result=None,
        )
    return result


def _run_prepared_case(
    *,
    case: EvalCase,
    started: float,
    workspace: Path,
    before: dict[str, FileFingerprint],
    audit_root: Path,
    agent_executor: AgentExecutor,
    observation: _TimingObservation,
) -> EvalCaseResult:
    run_result: RunResult | None = None
    modified_files: tuple[str, ...] = ()
    verifications: tuple[VerificationResult, ...] = ()

    try:
        with observation.phase("agent_ns"):
            run_result = agent_executor(
                case,
                workspace,
                audit_root / f"{case.id}.jsonl",
            )
    except Exception:
        return _case_result(
            case,
            started,
            ("executor_error",),
            modified_files=modified_files,
            verifications=verifications,
            run_result=run_result,
        )

    try:
        after = capture_snapshot(workspace)
        modified_files = changed_paths(before, after)
    except (OSError, WorkspaceSafetyError):
        return _case_result(
            case,
            started,
            ("workspace_error",),
            modified_files=modified_files,
            verifications=verifications,
            run_result=run_result,
        )

    if any(is_reserved_eval_path(path) for path in modified_files):
        return _case_result(
            case,
            started,
            ("workspace_error",),
            modified_files=modified_files,
            verifications=verifications,
            run_result=run_result,
        )

    try:
        with observation.phase("verifier_ns"):
            install_verifier(case, workspace)
            verifications = tuple(
                _run_verification(spec, workspace) for spec in case.verifications
            )
    except (OSError, WorkspaceSafetyError):
        return _case_result(
            case,
            started,
            ("workspace_error",),
            modified_files=modified_files,
            verifications=verifications,
            run_result=run_result,
        )

    failure_codes = _score(case, run_result, modified_files, verifications)
    return _case_result(
        case,
        started,
        failure_codes,
        modified_files=modified_files,
        verifications=verifications,
        run_result=run_result,
    )


def _verification_cleanup_pending() -> bool:
    parent = current_cleanup()
    return verification_cleanup_owner().has_pending or bool(parent is not None and parent.has_pending)


def _run_verification(spec: VerificationSpec, workspace: Path) -> VerificationResult:
    if _verification_cleanup_pending():
        return VerificationResult(spec.name, None, False, "verification_error",
                                  {"stage": "pending_cleanup", "resource_pending": True})
    with verifier_scope() as scope:
        return _run_owned_verification(spec, workspace, scope)


def _run_owned_verification(
    spec: VerificationSpec, workspace: Path, scope: TaskCleanup
) -> VerificationResult:
    policy = CommandPolicy(workspace)
    try:
        args = policy.validate(spec.command)
    except PolicyError as exc:
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_policy_rejected",
            diagnostics=_verification_diagnostics("policy", scope, error=exc),
        )
    except Exception as exc:
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_error",
            diagnostics=_verification_diagnostics("policy", scope, error=exc),
        )

    try:
        completed = run_bounded_process(
            args,
            cwd=workspace,
            env=policy.subprocess_environment(),
            timeout=spec.timeout,
            max_output_bytes=_VERIFICATION_MAX_OUTPUT_BYTES,
        )
    except BaseException as exc:
        try:
            retain_failure(scope, exc)
        except BaseException:
            # The exact resource was retained before retry. A second cleanup
            # interruption must not replace the primary exception/cancellation.
            scope.mark_failed()
        if not isinstance(exc, Exception):
            raise
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_error",
            diagnostics=_verification_diagnostics("process_exception", scope, error=exc),
        )

    retain_failure(scope, completed)
    diagnostics = _verification_diagnostics("process_result", scope, result=completed)
    if completed.cleanup_failed or completed.output_exceeded:
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_error",
            diagnostics=diagnostics,
        )
    if completed.timed_out:
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_timeout",
            diagnostics=diagnostics,
        )

    passed = completed.returncode == 0
    return VerificationResult(
        spec.name,
        exit_code=completed.returncode,
        passed=passed,
        error_code=None,
        diagnostics=diagnostics,
    )


def _verification_diagnostics(stage, scope, *, error=None, result=None):
    """Only controlled categories/booleans/numbers, never argv/env/output/paths."""
    from tricoder.process.control import ProcessExecutionUncertain
    from tricoder.core.cancellation import CancellationError
    kinds = (ProcessExecutionUncertain, CancellationError, PolicyError,
             PermissionError, FileNotFoundError, OSError, ValueError, RuntimeError)
    cause = error.__cause__ if error is not None else None
    os_error = cause if isinstance(cause, OSError) else error
    def number(name):
        value = getattr(os_error, name, None)
        return value if type(value) is int else None
    source = error if error is not None else result
    return {
        "stage": stage,
        "exception_type": next((kind.__name__ for kind in kinds if isinstance(error, kind)),
                               "other" if error is not None else None),
        "errno": number("errno"), "winerror": number("winerror"),
        "timed_out": bool(result.timed_out) if result is not None else None,
        "output_exceeded": bool(result.output_exceeded) if result is not None else None,
        "cleanup_failed": bool(getattr(source, "cleanup_failed", False)),
        "resource_returned": getattr(source, "cleanup_resource", None) is not None,
        "resource_pending": scope.has_pending,
        "termination_started": scope.started_deadline is not None,
    }


def _score(
    case: EvalCase,
    run_result: RunResult,
    modified_files: tuple[str, ...],
    verifications: tuple[VerificationResult, ...],
) -> tuple[str, ...]:
    failure_codes: list[str] = []
    if not run_result.ok:
        failure_codes.append("agent_failed")
    if modified_files and run_result.verification != "通过":
        failure_codes.append("agent_unverified")
    if any(not result.passed and result.error_code is None for result in verifications):
        failure_codes.append("verification_failed")
    if any(result.error_code is not None for result in verifications):
        failure_codes.append("verification_error")
    if any(not _path_is_allowed(path, case.allowed_changes, mode=case.path_match_mode) for path in modified_files):
        failure_codes.append("change_out_of_scope")
    if any(
        not any(_path_matches(path, pattern, mode=case.path_match_mode) for path in modified_files)
        for pattern in case.required_changes
    ):
        failure_codes.append("required_change_missing")
    return tuple(failure_codes)


def _path_is_allowed(path: str, patterns: tuple[str, ...], *, mode: PathMatchMode = "legacy") -> bool:
    _validate_match_mode(mode)
    if is_reserved_eval_path(path):
        return False
    return any(_path_matches(path, pattern, mode=mode) for pattern in patterns)


def _path_matches(path: str, pattern: str, *, mode: PathMatchMode = "legacy") -> bool:
    _validate_match_mode(mode)
    if mode == "legacy":
        return PurePosixPath(path).match(pattern)
    if (
        not is_workspace_relative_path(path)
        or not is_workspace_relative_path(pattern)
        or "**" in pattern
    ):
        return False
    parts, patterns = path.split("/"), pattern.split("/")
    return len(parts) == len(patterns) and all(
        fnmatchcase(part, segment) for part, segment in zip(parts, patterns)
    )


def _validate_match_mode(mode: PathMatchMode) -> None:
    if type(mode) is not str or mode not in {"legacy", "workspace_root"}:
        raise ValueError("unsupported path_match_mode")


def _case_result(
    case: EvalCase,
    started: float,
    failure_codes: tuple[str, ...],
    *,
    modified_files: tuple[str, ...],
    verifications: tuple[VerificationResult, ...],
    run_result: RunResult | None,
) -> EvalCaseResult:
    status: CaseStatus
    if any(code in _ERROR_FAILURE_CODES for code in failure_codes):
        status = "error"
    elif failure_codes:
        status = "failed"
    else:
        status = "passed"
    return EvalCaseResult(
        case_id=case.id,
        status=status,
        failure_codes=failure_codes,
        duration_ms=_elapsed_ms(started),
        rounds=run_result.rounds if run_result is not None else 0,
        tool_calls=run_result.tool_calls if run_result is not None else 0,
        modified_files=modified_files,
        verifications=verifications,
        agent_verification=(
            run_result.verification if run_result is not None else "未运行"
        ),
        usage=run_result.usage if run_result is not None else None,
    )


def _merge_usage(cases: tuple[EvalCaseResult, ...]) -> TokenUsage | None:
    if not cases or all(case.usage is None for case in cases):
        return None

    def strict_total(field: str) -> int | None:
        values: list[int] = []
        for case in cases:
            if case.usage is None:
                return None
            value = getattr(case.usage, field)
            if value is None:
                return None
            values.append(value)
        return sum(values)

    return TokenUsage(
        input_tokens=strict_total("input_tokens"),
        output_tokens=strict_total("output_tokens"),
        cached_tokens=strict_total("cached_tokens"),
        cache_miss_tokens=strict_total("cache_miss_tokens"),
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
