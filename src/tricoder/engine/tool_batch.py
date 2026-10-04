"""串行工具批次执行、结果配对和失败后 skipped 补齐。"""

from __future__ import annotations

import inspect
import json
import time
from dataclasses import replace
from typing import Any, Callable

from tricoder.core.cancellation import (
    CancellationError,
    CancellationToken,
    NativeCancellationError,
)
from tricoder.core.events import (
    ApprovalRequested,
    EventSink,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from tricoder.engine.state import AgentRunState, ToolBatchOutcome, ToolBatchStop
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
                result = await self._execute_tool(action, event_call.id)
            except (CancellationError, NativeCancellationError) as exc:
                if self.observation is not None:
                    latest, _ = self.observation.reconcile(state.execution_context())
                    state.modified_files = list(latest.modified_files)
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

            effects = self._apply_result(action, result, interrupted)
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
            if (
                not result.ok
                or stop_task
                or finished
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
            if not audit_ok:
                return ToolBatchOutcome(ToolBatchStop.AUDIT_FAILED, result)
            if state.unknown_effects:
                return ToolBatchOutcome(ToolBatchStop.UNKNOWN_EFFECTS, result)
            if cancelled:
                return ToolBatchOutcome(ToolBatchStop.CANCELLED, result)
            if stop_task:
                return ToolBatchOutcome(ToolBatchStop.FATAL, result)
            if finished:
                return ToolBatchOutcome(ToolBatchStop.FINISH, result)
            if not result.ok:
                if (
                    result.error is None
                    or result.error.recovery is not RecoveryAction.REPLAN
                ):
                    return ToolBatchOutcome(ToolBatchStop.FATAL, result)
                return ToolBatchOutcome(ToolBatchStop.REPLAN, result)
        return ToolBatchOutcome(ToolBatchStop.CONTINUE)

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
            effects = FileEffects(EffectState.UNKNOWN, effects.paths)
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
        state.verification = observed.verification
        state.unknown_effects = observed.unknown_effects
        state.evidence = observed.verification_evidence
        state.failed_snapshot = observed.verification_failure
        state.verification_required = observed.verification_required
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
