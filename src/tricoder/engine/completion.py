"""finish 请求的统一可信判定；不闭合历史或释放资源。"""

from __future__ import annotations

from dataclasses import dataclass
from tricoder.execution_state import TaskStopReason
from tricoder.workspace.check_coverage import (
    normalize_target as _normalize_target,
    target_covers_path as _target_covers_path,
    source_verification_paths,
    uncovered_paths,
)

from tricoder.core.validation import (
    TaskValidationReport,
    has_effective_success_result,
)
from tricoder.engine.state import AgentRunState
from tricoder.workspace.verification import (
    VerificationScope,
    WorkspaceSnapshot,
    proves_new_file_version,
)


@dataclass(frozen=True, slots=True)
class FinishAssessment:
    """一次 finish 预检查结果；它本身没有终结任务的权限。"""

    completed: bool
    summary: str
    validation_report: TaskValidationReport
    recoverable_reason: str | None = None
    pending_paths: tuple[str, ...] = ()
    scan_complete: bool = False
    stop_reason: TaskStopReason | None = None


def assess_finish(
    state: AgentRunState,
    *,
    summary: str,
    finish_outcome: str,
    cancellation_requested: bool,
    scope: object | None,
    current: WorkspaceSnapshot | None,
    termination_started: bool = False,
) -> FinishAssessment:
    """复用最终门禁判断完成资格和唯一可恢复缺口。

    调用方负责在需要时取得 ``current`` 完整快照。本函数可以撤销失效证据，
    但不会写历史、生成记忆、发布终态或释放锁。
    """

    valid = False
    stale_evidence = False
    scan_complete = type(current) is WorkspaceSnapshot and current.complete
    if state.verification_required and isinstance(scope, VerificationScope):
        # 失效或来源异常不是单纯缺少证据；不能借一次纠正越过该停止边界。
        stale_evidence = state.evidence is not None and not (
            scope.owns(state.evidence)
            and current is not None
            and state.evidence.is_valid_for(current)
        )
        if (
            current is not None
            and state.failed_snapshot is not None
            and proves_new_file_version(state.failed_snapshot, current)
        ):
            state.failed_snapshot = None
        valid = current_verification_satisfied(state, current, scope)
        if not valid:
            state.evidence = None
            state.verification = (
                "失败" if state.failed_snapshot is not None else "待验证"
            )

    validation_report = state.validation.report()
    # failed 优先于 stale 的汇总不能掩盖“检查后外部变化”。只读失败审查
    # 仍可交付，但至少要有未失效的当前工作区检查；纯确认没有这种义务。
    stale_only = bool(validation_report.stale_check_ids) and not (
        scan_complete and any(
            record.kind != "information" and record.workspace_stable
            for record in state.validation.current_records(current.digest)
        )
    )
    if validation_report.status == "stale" or stale_only:
        state.evidence = None
        state.verification_required = True
        state.verification = "待验证"
        valid = False

    completed = bool(
        finish_outcome == "completed"
        and not cancellation_requested
        and not state.cleanup_failed
        and not state.unknown_effects
        and validation_report.status != "stale"
        and (
            not state.verification_required
            or (valid and not validation_report.unresolved_check_ids)
        )
    )
    public_summary = _summary_with_verification_fact(
        summary,
        state,
        validation_report,
    )
    pending_paths = check_coverage(state, current).pending_paths
    safe_correction = bool(
        finish_outcome == "completed"
        and not completed
        and state.verification_required
        and scan_complete
        and isinstance(scope, VerificationScope)
        and scope.task_id == state.current_task_id
        and not scope.unknown_effects
        and not stale_evidence
        and state.verification_obligation != "legacy_unknown"
        and not state.unknown_effects
        and not cancellation_requested
        and not state.cleanup_failed
        and not termination_started
    )
    missing_verification = bool(
        safe_correction
        and state.verification == "待验证"
        and state.evidence is None
        and state.failed_snapshot is None
        and validation_report.status in {"unverified", "observed"}
        and not validation_report.unresolved_check_ids
        and pending_paths
    )
    unresolved_verification = bool(
        safe_correction
        and validation_report.unresolved_check_ids
        and _has_current_check_anchor(state, current, scope)
    )
    return FinishAssessment(
        completed=completed,
        summary=public_summary,
        validation_report=validation_report,
        recoverable_reason=(
            "missing_relevant_verification" if missing_verification else
            "unresolved_verification" if unresolved_verification else None
        ),
        pending_paths=pending_paths,
        scan_complete=scan_complete,
        stop_reason=(
            TaskStopReason.CANCELLED if cancellation_requested else
            TaskStopReason.CLEANUP_FAILED if state.cleanup_failed else
            TaskStopReason.UNKNOWN_EFFECTS if state.unknown_effects else
            TaskStopReason.MODEL_INCOMPLETE if finish_outcome == "incomplete" else
            TaskStopReason.COMPLETED if completed else
            TaskStopReason.VERIFICATION_BLOCKED
        ),
    )


def _has_current_check_anchor(
    state: AgentRunState, current: WorkspaceSnapshot, scope: VerificationScope,
) -> bool:
    """纠正只依赖当前任务的稳定检查事实；这不是通过能力。"""
    records = state.validation.current_records(current.digest)
    if not all(scope.owns_check(record) for record in records):
        return False
    unresolved = state.validation.report().unresolved_check_ids
    return any(
        record.kind in {"tests", "syntax", "static"}
        and record.workspace_stable
        and record.execution_complete
        and any(_normalize_target(record.cwd, target) is not None for target in record.targets)
        and (has_effective_success_result(record)
             or (record.check_id in unresolved and record.returncode not in {None, 0}))
        for record in records
    )


def current_verification_satisfied(
    state: AgentRunState,
    current: WorkspaceSnapshot | None,
    scope: object | None,
) -> bool:
    """只读判断：当前能力、覆盖和失败事实必须同时满足。"""
    return bool(
        isinstance(scope, VerificationScope)
        and type(current) is WorkspaceSnapshot and current.complete
        and scope.owns(state.evidence)
        and state.evidence.is_valid_for(current)
        and state.failed_snapshot is None
        and not state.validation.report().unresolved_check_ids
        and all(scope.owns_check(record) for record in state.validation.current_records(current.digest))
        and has_relevant_check(state, current)
    )


def has_relevant_check(
    state: AgentRunState,
    current: WorkspaceSnapshot,
) -> bool:
    """完成判定和反馈共用有效检查并集，不要求单条命令覆盖所有文件。"""

    coverage = check_coverage(state, current)
    return coverage.has_valid_check and not coverage.pending_paths


@dataclass(frozen=True, slots=True)
class CheckCoverage:
    """覆盖结果不是验证能力；最终仍需 authority、快照及安全门禁。"""

    pending_paths: tuple[str, ...]
    has_valid_check: bool = False


def check_coverage(
    state: AgentRunState,
    current: WorkspaceSnapshot | None,
) -> CheckCoverage:
    """从本轮宿主确认的路径和有效记录计算尚未覆盖的目标。"""

    files = tuple(dict.fromkeys(state.task_modified_files))
    # 成功创建的父目录由文件工具和账本确认，不要求子文件检查反向覆盖父目录。
    # 独立目录仍保留原验证要求；未知、失败或外部目录变化不由这里认可。
    paths = source_verification_paths(files, state.task_modified_directories)
    if type(current) is not WorkspaceSnapshot or not current.complete:
        return CheckCoverage(paths)

    successful_targets: list[str] = []
    has_valid_check = False
    for record in state.validation.current_records(current.digest):
        if (
            record.kind not in {"tests", "syntax", "static"}
            or not has_effective_success_result(record)
            or not record.workspace_stable
            or record.snapshot_id != current.digest
        ):
            continue
        targets = tuple(
            normalized for target in record.targets
            if (normalized := _normalize_target(record.cwd, target)) is not None
        )
        if not targets:
            continue
        has_valid_check = True
        successful_targets.extend(targets)
    return CheckCoverage(uncovered_paths(paths, successful_targets), has_valid_check)


def _summary_with_verification_fact(
    summary: str,
    state: AgentRunState,
    validation_report: TaskValidationReport,
) -> str:
    if state.verification_required and state.verification == "待验证":
        return (
            f"{summary}；文件修改后尚未运行验证命令，"
            "或尚无认可且覆盖修改范围的有效证据"
        )
    if state.verification_required and state.verification == "失败":
        return f"{summary}；文件修改后的验证失败"
    if validation_report.status == "stale":
        return f"{summary}；检查后工作区状态已变化，旧记录已过期"
    if validation_report.status == "failed":
        return f"{summary}；检查发现失败，见检查记录"
    return summary


__all__ = ["FinishAssessment", "assess_finish", "has_relevant_check", "check_coverage"]
