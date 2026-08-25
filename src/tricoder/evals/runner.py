"""Isolated execution and deterministic scoring for eval suites."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import subprocess
import time
from typing import Literal, TypeAlias

from tricoder.models import RunResult, TokenUsage
from tricoder.policy import CommandPolicy, PolicyError
from tricoder.subprocess_env import filtered_subprocess_env

from .loader import is_reserved_eval_path
from .models import EvalCase, EvalSuite, VerificationSpec
from .workspace import (
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


@dataclass(frozen=True, slots=True)
class VerificationResult:
    name: str
    exit_code: int | None
    passed: bool
    error_code: str | None


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
) -> EvalRunReport:
    """Run every case independently and return a safe structured report."""

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    run_dir.mkdir(parents=True, exist_ok=True)
    workspaces_root = run_dir / "workspaces"
    audit_root = run_dir / "audit"
    audit_root.mkdir(parents=True, exist_ok=True)

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


def _run_case(
    case: EvalCase,
    workspaces_root: Path,
    audit_root: Path,
    agent_executor: AgentExecutor,
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

    try:
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

    try:
        try:
            install_verifier(case, workspace)
            verifications = tuple(
                _run_verification(spec, workspace) for spec in case.verifications
            )
        finally:
            remove_verifier(workspace)
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


def _run_verification(
    spec: VerificationSpec, workspace: Path
) -> VerificationResult:
    try:
        args = CommandPolicy(workspace).validate(spec.command)
    except PolicyError:
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_policy_rejected",
        )
    except Exception:
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_error",
        )

    try:
        completed = subprocess.run(
            args,
            cwd=workspace,
            env=filtered_subprocess_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=spec.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_timeout",
        )
    except Exception:
        return VerificationResult(
            spec.name,
            exit_code=None,
            passed=False,
            error_code="verification_error",
        )

    passed = completed.returncode == 0
    return VerificationResult(
        spec.name,
        exit_code=completed.returncode,
        passed=passed,
        error_code=None,
    )


def _score(
    case: EvalCase,
    run_result: RunResult,
    modified_files: tuple[str, ...],
    verifications: tuple[VerificationResult, ...],
) -> tuple[str, ...]:
    failure_codes: list[str] = []
    if not run_result.ok:
        failure_codes.append("agent_failed")
    if run_result.verification != "通过":
        failure_codes.append("agent_unverified")
    if any(not result.passed and result.error_code is None for result in verifications):
        failure_codes.append("verification_failed")
    if any(result.error_code is not None for result in verifications):
        failure_codes.append("verification_error")
    if any(not _path_is_allowed(path, case.allowed_changes) for path in modified_files):
        failure_codes.append("change_out_of_scope")
    if any(
        not any(_path_matches(path, pattern) for path in modified_files)
        for pattern in case.required_changes
    ):
        failure_codes.append("required_change_missing")
    return tuple(failure_codes)


def _path_is_allowed(path: str, patterns: tuple[str, ...]) -> bool:
    if is_reserved_eval_path(path):
        return False
    return any(_path_matches(path, pattern) for pattern in patterns)


def _path_matches(path: str, pattern: str) -> bool:
    return PurePosixPath(path).match(pattern)


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
    usage: TokenUsage | None = None
    for case in cases:
        if case.usage is None:
            continue
        usage = case.usage if usage is None else usage.merge(case.usage)
    return usage


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
