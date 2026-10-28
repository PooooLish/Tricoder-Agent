"""根据宿主权威状态生成有界验证反馈。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from tricoder.core.validation import CommandCheckRecord, has_effective_success_result
from tricoder.engine.state import AgentRunState
from tricoder.engine.completion import check_coverage, current_verification_satisfied
from tricoder.policy import is_sensitive_workspace_path
from tricoder.workspace.verification import VerificationScope
from tricoder.workspace.check_coverage import normalize_target


MAX_FEEDBACK_PATHS = 20
MAX_FEEDBACK_CHARS = 2_000
MAX_BLOCKING_CHECKS = 5


@dataclass(frozen=True, slots=True)
class VerificationFeedbackCheckpoint:
    """工具批次开始时的最小验证状态，不包含源码或命令输出。"""

    required: bool
    verification: str
    unknown_effects: bool
    task_paths: tuple[str, ...]
    check_ids: frozenset[str]
    validation_status: str = "unverified"
    unresolved_check_count: int = 0


def checkpoint(state: AgentRunState) -> VerificationFeedbackCheckpoint:
    """捕获用于判断批次是否产生新反馈的稳定摘要。"""

    return VerificationFeedbackCheckpoint(
        required=state.verification_required,
        verification=state.verification,
        unknown_effects=state.unknown_effects,
        task_paths=_controlled_paths(state),
        check_ids=frozenset(
            record.check_id for record in state.validation.report().records
        ),
        validation_status=state.validation.report().status,
        unresolved_check_count=len(state.validation.report().unresolved_check_ids),
    )


def build_batch_feedback(
    state: AgentRunState,
    before: VerificationFeedbackCheckpoint,
) -> str | None:
    """批次产生相关检查或义务变化时返回一条最终宿主反馈。"""

    report = state.validation.report()
    new_checks = tuple(
        record for record in report.records if record.check_id not in before.check_ids
    )
    paths = _controlled_paths(state)
    state_changed = (
        before.required != state.verification_required
        or before.verification != state.verification
        or before.unknown_effects != state.unknown_effects
        or before.task_paths != paths
        or before.validation_status != report.status
        or before.unresolved_check_count != len(report.unresolved_check_ids)
    )
    if not new_checks and not state_changed:
        return None

    latest = new_checks[-1] if new_checks else None
    obligation = _obligation(state)
    # 无本轮义务时，普通信息命令或普通脚本不是需要重复灌入的验证反馈。
    if latest is not None and obligation == "none" and not state_changed:
        return None
    payload: dict[str, object] = {
        "type": "verification_feedback",
        "check_kind": latest.kind if latest is not None else None,
        "execution_status": _execution_status(latest),
        "verification_obligation": obligation,
        "pending_paths": list(paths[:MAX_FEEDBACK_PATHS]) if obligation == "pending" else [],
        "total_path_count": len(paths) if obligation == "pending" else 0,
        "validation_status": report.status,
        "unresolved_check_count": len(report.unresolved_check_ids),
        "reason_code": (
            "stale_checks" if obligation == "pending" and report.status == "stale"
            else "unresolved_checks" if obligation == "pending" and report.unresolved_check_ids
            else _reason_code(latest, obligation)
        ),
    }
    omitted = max(0, len(paths) - MAX_FEEDBACK_PATHS)
    if omitted:
        payload["omitted_count"] = omitted
    _add_blocking_checks(payload, state, check_coverage(
        state, state.evidence.after if state.evidence is not None else None,
    ).pending_paths)
    _add_next_step_hint(payload, state)
    return _bounded_json(payload)


def finish_recovery_feedback(
    state: AgentRunState, *, pending_paths: tuple[str, ...] | None = None,
    reason: str = "missing_relevant_verification",
) -> str:
    """生成首次过早 finish 的固定纠正反馈，不包含命令或源码正文。"""

    paths = _controlled_paths(state, pending_paths)
    payload: dict[str, object] = {
        "type": "verification_feedback",
        "check_kind": None,
        "execution_status": "not_run",
        "verification_obligation": "pending",
        "pending_paths": list(paths[:MAX_FEEDBACK_PATHS]),
        "total_path_count": len(paths),
        "reason_code": reason if reason == "unresolved_verification" else "missing_relevant_verification",
        "validation_status": state.validation.report().status,
        "unresolved_check_count": len(state.validation.report().unresolved_check_ids),
        "recovery_count": state.finish_verification_recoveries,
        "recovery_limit": 1,
        "message": (
            "当前任务尚未结束：文件修改后的验证义务仍未解除。普通脚本成功只记录运行事实，"
            "不能替代认可且覆盖修改范围的测试、语法或静态检查。请补充检查；若无法继续，"
            "请调用 finish(outcome=incomplete)。"
        ),
    }
    omitted = max(0, len(paths) - MAX_FEEDBACK_PATHS)
    if omitted:
        payload["omitted_count"] = omitted
    _add_blocking_checks(payload, state, pending_paths if pending_paths is not None else check_coverage(
        state, state.evidence.after if state.evidence is not None else None,
    ).pending_paths)
    _add_next_step_hint(payload, state)
    return _bounded_json(payload)


def _add_next_step_hint(payload: dict[str, object], state: AgentRunState) -> None:
    """只补充受控建议，不创建证据、修改台账或重建命令。"""
    if state.unknown_effects or state.cleanup_failed or state.verification_obligation == "legacy_unknown":
        return
    report = state.validation.report()
    if state.verification_required and report.unresolved_check_ids:
        payload["next_step_hint"] = "repair_blocking_checks"
        return  # 保留失败检查 ID、同检查复验指引及优先级。
    if payload.get("reason_code") == "zero_tests_reported":
        payload["message"] = "零测试不等于验证通过；请修正测试发现方式或补充必要测试，再执行有效检查。"
        return
    if report.status == "stale" or state.failed_snapshot is not None:
        return
    if payload.get("verification_obligation") == "satisfied":
        payload["next_step_hint"] = "finish_if_task_complete"
        payload["message"] = "当前验证义务已满足；若用户任务也已完成，请显式调用 finish。"
        return
    scope, evidence = state.verification_scope, state.evidence
    trusted_success = bool(
        isinstance(scope, VerificationScope) and scope.owns(evidence)
        and evidence.after.complete and evidence.is_valid_for(evidence.after)
        and all(scope.owns_check(r) for r in state.validation.current_records(evidence.after.digest))
    )
    # 收尾纠正已经由 assess_finish 核验；普通批次只对当前可信成功检查提示覆盖。
    if (payload.get("verification_obligation") == "pending"
            and (payload.get("reason_code") == "missing_relevant_verification"
                 or (trusted_success and payload.get("reason_code") == "verification_obligation_pending"))):
        payload["next_step_hint"] = "verify_pending_paths"
        payload["message"] = (
            "当前验证覆盖仍不足，任务尚未结束；读文件不能解除 pending_paths。"
            "若项目使用 unittest，可从工作区根目录运行 python -m unittest discover -v；"
            "或在必要业务测试通过后补相关路径的语法/静态检查。语法检查不替代业务测试。"
            "以上仅为条件示例，不是证据或自动执行；检查后再改文件需重新检查，最后显式 finish。"
        )


def _add_blocking_checks(
    payload: dict[str, object], state: AgentRunState, paths: tuple[str, ...],
) -> None:
    """只展示宿主身份和有限目标；不从签名重建或自动重放命令。"""
    report = state.validation.report()
    retained = {record.check_id: record for record in report.records}
    blocking_ids = report.unresolved_check_ids if state.verification_required else ()
    items: list[dict[str, object]] = []
    for check_id in blocking_ids[:MAX_BLOCKING_CHECKS]:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", check_id) is None:
            continue
        record = retained.get(check_id)
        targets: list[str] = []
        if record is not None:
            for target in record.targets:
                # 拒绝而非截短，避免把异常路径变成另一个可执行目标。
                if not _safe_target_part(record.cwd) or not _safe_target_part(target):
                    continue
                normalized = normalize_target(record.cwd, target)
                if normalized is not None and _safe_target_part(normalized) and normalized not in targets:
                    targets.append(normalized)
                if len(targets) == 3:
                    break
        items.append({
            "check_id": check_id,
            "kind": record.kind if record is not None else None,
            "reason": ("record_unavailable" if record is None else
                       "incomplete" if not record.execution_complete else "failed"),
            "targets": targets,
        })
    payload["historical_stale_count"] = len(report.stale_check_ids)
    payload["blocking_checks"] = items
    payload["blocking_checks_omitted_count"] = len(blocking_ids) - len(items)
    if blocking_ids:
        payload["message"] = (
            ("当前任务尚未结束。" if "recovery_count" in payload else "")
            + ("覆盖已齐全，仍须修复并复验列出的失败检查。" if not paths else
               "先修复并复验列出的失败检查，再补路径覆盖。")
            + "按 check_id 查看原工具回合，使用相同检查复验；历史过期本身不要求逐条重跑。"
            "记录超出窗口时不重建命令；无法继续请 finish(outcome=incomplete)。"
        )
    elif report.unresolved_check_ids:
        payload["message"] = "检查发现失败，仅保留检查事实；本轮没有修改验证义务，可按审查结果交付。"


def _safe_target_part(value: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", value)
        and not PurePosixPath(value).is_absolute()
        and ".." not in PurePosixPath(value).parts
        and not value.startswith("-")
        and not is_sensitive_workspace_path(value)
    )


def controlled_pending_paths(
    state: AgentRunState, pending_paths: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """公开供审计复用的同一受控路径视图。"""

    return _controlled_paths(state, pending_paths)


def _controlled_paths(
    state: AgentRunState, pending_paths: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """只返回受控相对路径；敏感、绝对和越界形式均不进入反馈。"""

    if pending_paths is None:
        current = state.evidence.after if state.evidence is not None else None
        pending_paths = check_coverage(state, current).pending_paths
    controlled: list[str] = []
    for raw in pending_paths:
        if not isinstance(raw, str):
            continue
        normalized = raw.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or PureWindowsPath(normalized).drive
            or ".." in path.parts
            or is_sensitive_workspace_path(normalized)
        ):
            continue
        # 单项同样限长，避免一个合法但极长的文件名占满整个反馈。
        controlled.append(normalized[:240])
    return tuple(dict.fromkeys(controlled))


def _obligation(state: AgentRunState) -> str:
    if state.unknown_effects:
        return "unknown"
    if not state.verification_required:
        return "none"
    report = state.validation.report()
    if (
        state.cleanup_failed
        or state.failed_snapshot is not None
        or report.unresolved_check_ids
        or report.status in {"stale", "failed"}
    ):
        return "pending"
    scope = state.verification_scope
    if (
        state.evidence is not None
        and state.verification == "通过"
        and current_verification_satisfied(state, state.evidence.after, scope)
    ):
        return "satisfied"
    return "pending"


def _execution_status(record: CommandCheckRecord | None) -> str:
    if record is None:
        return "not_run"
    if not record.execution_complete:
        return "incomplete"
    return "succeeded" if record.returncode == 0 else "failed"


def _reason_code(record: CommandCheckRecord | None, obligation: str) -> str:
    if record is None:
        return (
            "file_change_requires_verification"
            if obligation == "pending"
            else "verification_state_changed"
        )
    if "zero_tests_reported" in record.diagnostics:
        return "zero_tests_reported"
    if obligation == "satisfied" and has_effective_success_result(record):
        return "verification_accepted"
    if record.kind == "script" and obligation == "pending":
        return "script_does_not_clear_verification"
    if record.kind == "information" and obligation == "pending":
        return "information_does_not_clear_verification"
    if record.returncode not in {0, None}:
        return "verification_failed"
    if not has_effective_success_result(record):
        return "verification_not_accepted"
    return "verification_obligation_pending"


def _bounded_json(payload: dict[str, object]) -> str:
    """保持结构化消息不超过公开上限；先裁路径，不截断 JSON。"""

    while True:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= MAX_FEEDBACK_CHARS:
            return encoded
        paths = payload.get("pending_paths")
        if isinstance(paths, list) and paths:
            paths.pop()
            payload["omitted_count"] = int(payload.get("omitted_count", 0)) + 1
            continue
        checks = payload.get("blocking_checks")
        if isinstance(checks, list) and checks:
            # 保留身份优先于目标；仍超限再减少条目，并准确保留总数。
            with_targets = next((item for item in reversed(checks) if item["targets"]), None)
            if with_targets is not None:
                with_targets["targets"].pop()
                continue
            checks.pop()
            payload["blocking_checks_omitted_count"] = int(payload.get("blocking_checks_omitted_count", 0)) + 1
            continue
        message = payload.get("message")
        if isinstance(message, str) and len(message) > 80:
            payload["message"] = message[:80]
            continue
        raise ValueError("验证反馈无法在公开上限内编码")


__all__ = [
    "MAX_FEEDBACK_CHARS",
    "MAX_FEEDBACK_PATHS",
    "VerificationFeedbackCheckpoint",
    "build_batch_feedback",
    "checkpoint",
    "controlled_pending_paths",
    "finish_recovery_feedback",
]
