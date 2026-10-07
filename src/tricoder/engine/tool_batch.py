"""串行工具批次执行、结果配对和失败后 skipped 补齐。"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from dataclasses import replace
from typing import Any, Callable

from tricoder.core.cancellation import (
    CancellationError,
    CancellationToken,
    NativeCancellationError,
)
from tricoder.core.clarification import ClarificationResult, ClarificationStatus
from tricoder.core.events import (
    ApprovalRequested,
    EventSink,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from tricoder.engine.state import AgentRunState, ToolBatchOutcome, ToolBatchStop
from tricoder.engine.progress import (
    ProgressAction,
    ProgressDecision,
    ProgressObservation,
    ProgressReason,
)
from tricoder.engine.telemetry import (
    AgentObserver,
    elapsed_ms,
    emit,
    tool_origin_event,
)
from tricoder.execution_state import (
    EffectState,
    ErrorCode,
    FileEffects,
    RecoveryAction,
    should_stop_task,
)
from tricoder.models import ToolAction, ToolCall, ToolResult, tool_failure
from tricoder.protocols import ActionProtocol, ResolvedAction
from tricoder.task_cleanup import run_in_cleanup_thread
from tricoder.task_observation import apply_tool_transition
from tricoder.workspace.verification import VerificationScope


_READ_ONLY_BUILTINS = frozenset(
    {"list_files", "read_file", "search_text", "glob_files", "git_diff", "read_tool_result"}
)
_DURATION_PATTERN = re.compile(
    r"(?i)\b(?:in\s+)?\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|secs?|s)\b"
)


def skipped_result(blocked_by: str, known_call_ids: tuple[str, ...]) -> ToolResult:
    """为批次未执行项生成只引用本轮已知 ID 的结构化结果。"""

    if (
        any(not isinstance(call_id, str) or not call_id for call_id in known_call_ids)
        or len(set(known_call_ids)) != len(known_call_ids)
        or blocked_by not in known_call_ids
    ):
        raise ValueError("跳过来源必须是本轮唯一的已知调用")
    return tool_failure(
        ErrorCode.SKIPPED,
        json.dumps(
            {
                "status": "skipped",
                "blocked_by_call_index": known_call_ids.index(blocked_by),
            }
        ),
        recovery=RecoveryAction.REPLAN,
    )


class ToolBatchExecutor:
    """执行一个已解析动作批次；所有可变事实提交到同一个 RunState。"""

    def __init__(
        self,
        *,
        tools: object,
        protocol: ActionProtocol,
        observer: AgentObserver,
        state: AgentRunState,
        cancellation: CancellationToken,
        event_sink: EventSink | None,
        verification_scope: object | None,
        workspace_policy: object | None,
        observation: object | None,
        publish_state: Callable[[], None],
        log: Callable[[dict[str, Any]], bool],
        audit_arguments: Callable[[ToolAction, ToolResult], dict[str, Any]],
    ) -> None:
        self.tools = tools
        self.protocol = protocol
        self.observer = observer
        self.state = state
        self.cancellation = cancellation
        self.event_sink = event_sink
        self.verification_scope = verification_scope
        self.workspace_policy = workspace_policy
        self.observation = observation
        self.publish_state = publish_state
        self.log = log
        self.audit_arguments = audit_arguments

    async def execute(
        self,
        resolved: ResolvedAction,
        round_number: int,
    ) -> ToolBatchOutcome:
        """串行执行批次；首次停止条件后其余动作只补 skipped。"""

        state = self.state
        progress_feedback: list[str] = []
        for action_index, (action, tool_call_id) in enumerate(
            zip(resolved.actions, resolved.tool_call_ids)
        ):
            if self.cancellation.is_cancelled:
                self.fill_remaining_results(
                    resolved,
                    action_index,
                    action_index,
                    round_number,
                )
                return ToolBatchOutcome(ToolBatchStop.CANCELLED)
            action = self._with_reason(action)
            self.observer.on_action(action)
            event_call = self._event_call(
                action,
                tool_call_id,
                round_number,
                action_index,
            )
            state.tool_calls += 1
            action_started = time.perf_counter()
            state.file_effects_observed = False
            if self.observation is not None:
                self.observation.begin_tool()
            interrupted = False
            try:
                if action.tool == "ask_user" and state.clarification_requests >= 2:
                    result = tool_failure(
                        ErrorCode.NEEDS_INPUT,
                        "本任务已达到最多 2 次有效提问次数；任务仍需用户补充信息",
                        file_effects=FileEffects(EffectState.NONE),
                        clarification=ClarificationResult.unavailable(
                            "question_limit"
                        ),
                    )
                else:
                    result = await self._execute_tool(action, event_call.id)
            except (CancellationError, NativeCancellationError) as exc:
                if self.observation is not None:
                    latest, _ = self.observation.reconcile(state.execution_context())
                    state.modified_files = list(latest.modified_files)
                    state.modified_directories = list(
                        latest.modified_directories
                    )
                    state.verification = latest.verification
                    state.unknown_effects = latest.unknown_effects
                    state.evidence = latest.verification_evidence
                    state.failed_snapshot = latest.verification_failure
                    state.verification_required = latest.verification_required
                state.cleanup_failed = state.cleanup_failed or exc.cleanup_failed
                result = tool_failure(
                    (
                        ErrorCode.CLEANUP_FAILED
                        if exc.cleanup_failed
                        else ErrorCode.CANCELLED
                    ),
                    (
                        "任务已取消，资源清理未确认"
                        if exc.cleanup_failed
                        else "任务已取消，该动作未完成"
                    ),
                    file_effects=(
                        FileEffects(EffectState.UNKNOWN) if exc.cleanup_failed else None
                    ),
                )
                interrupted = True

            clarification = result.clarification
            if (
                action.tool == "ask_user"
                and type(clarification) is ClarificationResult
                and clarification.reason != "question_limit"
            ):
                state.clarification_requests += 1
            effects = self._apply_result(action, result, interrupted)
            if (
                result.ok
                and not interrupted
                and effects.state is EffectState.CONFIRMED
                and (effects.paths or effects.directory_paths)
            ):
                # 只消费注册表从真实操作账本/宿主扫描确认的变化。模型自报路径、
                # no-op 与 UNKNOWN 均不能借此重置重复读取计数。
                state.progress.note_workspace_change()
            self.publish_state()
            duration_ms = elapsed_ms(action_started)
            state.messages.append(
                self.protocol.tool_result_message(action, result, tool_call_id)
            )
            try:
                self.observer.on_tool_result(action, result, duration_ms)
                emit(self.event_sink, ToolExecutionCompleted(event_call.id, result))
                audit_ok = self.log(
                    self._tool_audit_event(
                        round_number,
                        action,
                        result,
                        duration_ms,
                    )
                )
            except BaseException:
                try:
                    self.fill_remaining_results(
                        resolved,
                        action_index + 1,
                        action_index,
                        round_number,
                        notify=False,
                    )
                except BaseException:
                    pass
                raise

            cancelled = (
                interrupted
                or self.cancellation.is_cancelled
                or (
                    result.error is not None
                    and result.error.code is ErrorCode.CANCELLED
                )
            )
            stop_task = should_stop_task(result.error, effects)
            finished = action.tool == "finish" and result.ok
            clarified = bool(
                action.tool == "ask_user"
                and result.ok
                and type(clarification) is ClarificationResult
                and clarification.status is ClarificationStatus.ANSWERED
            )
            needs_input = bool(
                action.tool == "ask_user"
                and type(clarification) is ClarificationResult
                and clarification.status
                in {ClarificationStatus.TIMED_OUT, ClarificationStatus.UNAVAILABLE}
            )
            remaining_filled = False
            if (
                not result.ok
                or stop_task
                or finished
                or clarified
                or needs_input
                or cancelled
                or not audit_ok
            ):
                remaining_audit_ok = self.fill_remaining_results(
                    resolved,
                    action_index + 1,
                    action_index,
                    round_number,
                )
                audit_ok = audit_ok and remaining_audit_ok
                remaining_filled = True
            if not audit_ok:
                return ToolBatchOutcome(ToolBatchStop.AUDIT_FAILED, result)
            if state.unknown_effects:
                return ToolBatchOutcome(ToolBatchStop.UNKNOWN_EFFECTS, result)
            if cancelled:
                return ToolBatchOutcome(ToolBatchStop.CANCELLED, result)
            if clarified:
                state.progress.note_user_answer()
                return ToolBatchOutcome(ToolBatchStop.CLARIFIED, result)
            if needs_input:
                return ToolBatchOutcome(ToolBatchStop.NEEDS_INPUT, result)
            if stop_task:
                return ToolBatchOutcome(ToolBatchStop.FATAL, result)
            if finished:
                return ToolBatchOutcome(ToolBatchStop.FINISH, result)

            try:
                decision = await self._observe_progress(action, result)
            except (CancellationError, NativeCancellationError) as exc:
                # 工具结果和工具审计已经提交；这里只终止附加的进展扫描，
                # 不能再制造第二个 tool result，也不能绕过统一 finalizer。
                state.cleanup_failed = state.cleanup_failed or exc.cleanup_failed
                if not remaining_filled:
                    remaining_audit_ok = self.fill_remaining_results(
                        resolved,
                        action_index + 1,
                        action_index,
                        round_number,
                    )
                    if not remaining_audit_ok:
                        return ToolBatchOutcome(ToolBatchStop.AUDIT_FAILED, result)
                return ToolBatchOutcome(ToolBatchStop.CANCELLED, result)
            if self.cancellation.is_cancelled:
                if not remaining_filled:
                    self.fill_remaining_results(
                        resolved,
                        action_index + 1,
                        action_index,
                        round_number,
                    )
                return ToolBatchOutcome(ToolBatchStop.CANCELLED, result)
            if decision.action is not ProgressAction.CONTINUE:
                if decision.action is ProgressAction.STOP and not remaining_filled:
                    remaining_audit_ok = self.fill_remaining_results(
                        resolved,
                        action_index + 1,
                        action_index,
                        round_number,
                    )
                    remaining_filled = True
                    if not remaining_audit_ok:
                        return ToolBatchOutcome(ToolBatchStop.AUDIT_FAILED, result)
                progress_audit_ok = self.log(
                    self._progress_audit_event(
                        round_number,
                        action_index,
                        decision,
                    )
                )
                if not progress_audit_ok:
                    if not remaining_filled:
                        self.fill_remaining_results(
                            resolved,
                            action_index + 1,
                            action_index,
                            round_number,
                        )
                    return ToolBatchOutcome(ToolBatchStop.AUDIT_FAILED, result)
                if self.cancellation.is_cancelled:
                    if not remaining_filled:
                        self.fill_remaining_results(
                            resolved,
                            action_index + 1,
                            action_index,
                            round_number,
                        )
                    return ToolBatchOutcome(ToolBatchStop.CANCELLED, result)
                if decision.action is ProgressAction.WARN:
                    progress_feedback.append(self._progress_feedback(decision))
                else:
                    return ToolBatchOutcome(
                        ToolBatchStop.PROGRESS_STOP,
                        result,
                        tuple(progress_feedback),
                        decision,
                    )
            if not result.ok:
                if (
                    result.error is None
                    or result.error.recovery is not RecoveryAction.REPLAN
                ):
                    return ToolBatchOutcome(
                        ToolBatchStop.FATAL,
                        result,
                        tuple(progress_feedback),
                    )
                return ToolBatchOutcome(
                    ToolBatchStop.REPLAN,
                    result,
                    tuple(progress_feedback),
                )
        return ToolBatchOutcome(
            ToolBatchStop.CONTINUE,
            feedback=tuple(progress_feedback),
        )

    async def _observe_progress(
        self,
        action: ToolAction,
        result: ToolResult,
    ) -> ProgressDecision:
        """只在可恢复失败或成功只读工具后生成本地观察。"""

        scope = self.verification_scope
        check = result.command_check
        information_read = bool(
            result.ok
            and isinstance(scope, VerificationScope)
            and scope.owns_check(check)
            and check is not None
            and check.kind == "information"
        )
        read_only = self._is_read_only(action.tool) or information_read
        recoverable_failure = bool(
            not result.ok
            and result.error is not None
            and result.error.recovery is RecoveryAction.REPLAN
        )
        if action.tool in {"ask_user", "finish"} or not (
            read_only or recoverable_failure
        ):
            return ProgressDecision()

        workspace_fingerprint: str | None = None
        workspace_complete = False
        if isinstance(scope, VerificationScope) and self.workspace_policy is not None:
            try:
                snapshot = await run_in_cleanup_thread(
                    scope.capture,
                    self.workspace_policy,
                )
            except (CancellationError, NativeCancellationError):
                raise
            except Exception:
                snapshot = None
            if snapshot is not None and snapshot.complete:
                workspace_fingerprint = snapshot.content_digest or snapshot.digest
                workspace_complete = True

        if self.cancellation.is_cancelled:
            return ProgressDecision()
        arguments_fingerprint = self._arguments_fingerprint(action, result, scope)
        failure_classification = self._failure_classification(result)
        check_fingerprint = None
        if (
            recoverable_failure
            and isinstance(scope, VerificationScope)
            and scope.owns_check(check)
            and check is not None
            and check.kind != "information"
        ):
            check_fingerprint = self._fingerprint(
                {"argv": check.argv, "cwd": check.cwd, "kind": check.kind}
            )
        observation = ProgressObservation(
            tool_name=action.tool,
            arguments_fingerprint=arguments_fingerprint,
            result_fingerprint=self._result_fingerprint(result),
            failure_classification=failure_classification,
            workspace_fingerprint=workspace_fingerprint,
            workspace_complete=workspace_complete,
            read_only=read_only and result.ok,
            check_fingerprint=check_fingerprint,
        )
        return self.state.progress.observe(observation)

    def _arguments_fingerprint(
        self,
        action: ToolAction,
        result: ToolResult,
        scope: object | None,
    ) -> str:
        """优先使用宿主已执行的规范参数，避免路径别名重置预算。"""

        check = result.command_check
        if (
            isinstance(scope, VerificationScope)
            and scope.owns_check(check)
            and check is not None
        ):
            return self._fingerprint(
                {"argv": check.argv, "cwd": check.cwd, "kind": check.kind}
            )
        normalized = dict(action.arguments)
        resolver = getattr(self.workspace_policy, "resolve_path", None)
        workspace = getattr(self.workspace_policy, "workspace", None)
        if callable(resolver) and workspace is not None:
            for key in ("path", "cwd"):
                value = normalized.get(key)
                if not isinstance(value, str):
                    continue
                try:
                    relative = resolver(value).relative_to(workspace).as_posix()
                except (OSError, ValueError):
                    continue
                normalized[key] = relative or "."
        pattern = normalized.get("pattern")
        if action.tool == "glob_files" and isinstance(pattern, str):
            canonical = pattern.replace("\\", "/")
            while canonical.startswith("./"):
                canonical = canonical[2:]
            normalized["pattern"] = canonical
        return self._fingerprint(normalized)

    def _is_read_only(self, tool_name: str) -> bool:
        origin = getattr(self.tools, "origin", None)
        if callable(origin):
            try:
                return getattr(origin(tool_name), "risk", None) == "read"
            except (KeyError, ValueError):
                return False
        return tool_name in _READ_ONLY_BUILTINS

    @staticmethod
    def _failure_classification(result: ToolResult) -> str | None:
        if result.ok or result.error is None:
            return None
        returncode = (
            result.command_check.returncode
            if result.command_check is not None
            else None
        )
        return ":".join(
            (
                result.error.code.value,
                result.error.recovery.value,
                "none" if returncode is None else str(returncode),
            )
        )

    @staticmethod
    def _fingerprint(value: object) -> str:
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            serialized = f"unsupported:{type(value).__name__}"
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def _result_fingerprint(cls, result: ToolResult) -> str:
        check = result.command_check
        content_digest = result.progress_output_digest
        if not (
            isinstance(content_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", content_digest)
        ):
            # 兼容尚未提供宿主摘要的受控 ToolResult。spill_sha256 覆盖完整原文；
            # 非 spill 结果则只对命令诊断中的宿主耗时噪声做规范化。
            if (
                isinstance(result.spill_sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", result.spill_sha256)
            ):
                content_digest = result.spill_sha256
            else:
                output = result.output
                if check is not None:
                    output = _DURATION_PATTERN.sub("<duration>", output)
                content_digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        payload = {
            "ok": result.ok,
            "error": result.error.code.value if result.error is not None else None,
            "recovery": (
                result.error.recovery.value if result.error is not None else None
            ),
            "returncode": check.returncode if check is not None else None,
            # 只比较完整内容身份；随机引用、展示预览和 spill 元数据不参与。
            "output_digest": content_digest,
        }
        return cls._fingerprint(payload)

    @staticmethod
    def _progress_feedback(decision: ProgressDecision) -> str:
        if decision.reason is ProgressReason.REPEATED_FAILURE:
            subject = "相同工作区状态下的同一失败"
        else:
            subject = "工作区无变化时的同一读取"
        return (
            f"进展提醒：{subject}已累计 {decision.count}/{decision.limit} 次。"
            "请基于现有证据做最小诊断并改变做法；若无法继续，请调用 finish "
            "如实说明限制，不要重复相同操作。"
        )

    @staticmethod
    def _progress_audit_event(
        round_number: int,
        call_index: int,
        decision: ProgressDecision,
    ) -> dict[str, Any]:
        assert decision.reason is not None
        return {
            "round": round_number,
            "status": (
                "progress_warning"
                if decision.action is ProgressAction.WARN
                else "progress_stop"
            ),
            "call_index": call_index,
            "reason": decision.reason.value,
            "count": decision.count,
            "limit": decision.limit,
            "summary_id": decision.summary_id,
        }

    def fill_remaining_results(
        self,
        resolved: ResolvedAction,
        start: int,
        blocked_index: int,
        round_number: int,
        *,
        notify: bool = True,
    ) -> bool:
        """先构造全部剩余协议消息，再发布可能抛异常的事件与审计。"""

        known_ids = tuple(
            call_id or f"legacy-{round_number}-{index}"
            for index, call_id in enumerate(resolved.tool_call_ids)
        )
        skipped = skipped_result(known_ids[blocked_index], known_ids)
        remaining_messages = [
            self.protocol.tool_result_message(
                resolved.actions[index],
                skipped,
                resolved.tool_call_ids[index],
            )
            for index in range(start, len(resolved.actions))
        ]
        self.state.messages.extend(remaining_messages)
        if not notify:
            return True
        audit_ok = True
        for index in range(start, len(resolved.actions)):
            emit(
                self.event_sink,
                ToolExecutionCompleted(known_ids[index], skipped),
            )
            logged = self.log(
                {
                    "round": round_number,
                    "status": "skipped",
                    "call_index": index,
                    "blocked_by_call_index": blocked_index,
                    "error": skipped.error.public_fields(),
                }
            )
            audit_ok = audit_ok and logged
        return audit_ok

    def _with_reason(self, action: ToolAction) -> ToolAction:
        if action.reason:
            return action
        definition = self.tools.describe(action.tool)
        return replace(
            action,
            reason=(
                definition.description
                if definition is not None
                else "请求执行未注册的工具。"
            ),
        )

    def _event_call(
        self,
        action: ToolAction,
        tool_call_id: str | None,
        round_number: int,
        action_index: int,
    ) -> ToolCall:
        call = ToolCall(
            tool_call_id or f"legacy-{round_number}-{action_index}",
            action.tool,
            action.arguments,
        )
        requires_approval = getattr(self.tools, "requires_approval", None)
        if callable(requires_approval) and requires_approval(action.tool):
            emit(self.event_sink, ApprovalRequested(call, action.reason))
        emit(self.event_sink, ToolExecutionStarted(call))
        return call

    async def _execute_tool(self, action: ToolAction, call_id: str) -> ToolResult:
        execute_async = getattr(self.tools, "execute_async", None)
        if callable(execute_async):
            parameters = inspect.signature(execute_async).parameters
            kwargs: dict[str, Any] = {"cancellation": self.cancellation}
            if "call_id" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                kwargs["call_id"] = call_id
            return await execute_async(action.tool, action.arguments, **kwargs)
        self.cancellation.raise_if_cancelled()
        execute = self.tools.execute
        parameters = inspect.signature(execute).parameters
        kwargs = {}
        if "call_id" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs["call_id"] = call_id
        return await run_in_cleanup_thread(
            execute,
            action.tool,
            action.arguments,
            **kwargs,
        )

    def _apply_result(
        self,
        action: ToolAction,
        result: ToolResult,
        interrupted: bool,
    ) -> FileEffects:
        state = self.state
        changed_paths = (
            tuple(
                dict.fromkeys(
                    (
                        [result.relative_path]
                        if result.relative_path is not None
                        else []
                    )
                    + list(result.modified_paths)
                )
            )
            if result.ok
            else ()
        )
        effects = result.file_effects
        state.cleanup_failed = state.cleanup_failed or (
            result.error is not None
            and result.error.code is ErrorCode.CLEANUP_FAILED
        )
        if effects is None:
            effects = (
                FileEffects(EffectState.CONFIRMED, changed_paths)
                if changed_paths
                else FileEffects(EffectState.NONE)
            )
        scope = self.verification_scope
        if (
            isinstance(scope, VerificationScope)
            and scope.unknown_effects
        ) or (
            self.observation is not None and self.observation.unknown_effects
        ):
            effects = FileEffects(
                EffectState.UNKNOWN,
                effects.paths,
                effects.directory_paths,
            )
        state.file_effects_observed = not interrupted
        candidate = result.verification_evidence
        if not (
            action.tool == "run_command"
            and isinstance(scope, VerificationScope)
            and scope.owns(candidate)
        ):
            candidate = None
        observed = apply_tool_transition(
            state.execution_context(),
            effects,
            candidate,
        )
        state.modified_files = list(observed.modified_files)
        state.modified_directories = list(observed.modified_directories)
        state.verification = observed.verification
        state.unknown_effects = observed.unknown_effects
        state.evidence = observed.verification_evidence
        state.failed_snapshot = observed.verification_failure
        state.verification_required = observed.verification_required
        if (
            effects.state is EffectState.UNKNOWN
            or effects.paths
            or effects.directory_paths
        ):
            state.validation.invalidate()
        check = result.command_check
        if (
            action.tool == "run_command"
            and isinstance(scope, VerificationScope)
            and scope.owns_check(check)
        ):
            state.validation.observe(check)
        return effects

    def _tool_audit_event(
        self,
        round_number: int,
        action: ToolAction,
        result: ToolResult,
        duration_ms: int,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "round": round_number,
            "status": "ok" if result.ok else "tool_error",
            "tool": action.tool if self.tools.contains(action.tool) else "unknown",
            "reason_chars": len(action.reason),
            "arguments": self.audit_arguments(action, result),
            "output_chars": len(result.output),
            "duration_ms": duration_ms,
        }
        if result.error is not None:
            event["error"] = result.error.public_fields()
        origin = tool_origin_event(self.tools, action.tool)
        if origin is not None:
            event["origin"] = origin
        if result.spill_reference is not None:
            event["spill"] = {
                "reference": result.spill_reference,
                "bytes": result.spill_bytes,
                "sha256": result.spill_sha256,
            }
        return event
