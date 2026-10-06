"""Agent 单任务阶段编排。"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from tricoder.audit import AuditLogger
from tricoder.context.coordinator import (
    MemoryAuditFailure,
    MemoryCoordinator,
    MemoryStepInput,
    MemoryStepProgress,
    MemoryStepResult,
)
from tricoder.context.manager import ContextManager
from tricoder.context.memory import (
    ConversationMemory,
    TASK_TERMINATION_KIND,
    TASK_TERMINATION_NOTICE,
)
from tricoder.context.summarizer import MemorySummaryError
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.events import (
    ContextCompacted,
    EventSink,
    RoundStarted,
    RuntimeFailed,
)
from tricoder.engine.finalization import TaskFinalizer
from tricoder.engine.provider_request import PlanningPhase, ProviderResponseCollector
from tricoder.engine.state import AgentRunState, ToolBatchStop
from tricoder.engine.telemetry import (
    AgentObserver,
    audit_arguments,
    audit_failure_result,
    audit_usage_event,
    elapsed_ms,
    emit,
    log_event,
    notify_provider_usage,
)
from tricoder.engine.tool_batch import ToolBatchExecutor
from tricoder.models import MemoryConfig, RunResult, SessionContext, SessionTurnResult, TokenUsage
from tricoder.providers import ModelProvider, ProviderError, ProviderProtocolError
from tricoder.protocols import ActionProtocol, PROTOCOL_FEEDBACK
from tricoder.task_cleanup import run_in_cleanup_thread
from tricoder.task_observation import current_task_observation
from tricoder.workspace.verification import VerificationScope, proves_new_file_version


AUDIT_FAILURE_MESSAGE = "无法写入审计日志，运行已安全停止"
PROVIDER_FAILURE_MESSAGE = "模型请求失败，运行已安全停止"
MAX_NATIVE_MISSING_TOOL_RESPONSES = 3
NATIVE_TERMINATION_FAILURE = (
    "结束协议纠正失败：本任务累计 3 次未提交工具调用，"
    "已停止继续请求；任务结果仍需确认。"
)


class AgentRunner:
    """按准备、记忆、规划、请求、工具批次和收尾阶段执行单个任务。"""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: object,
        protocol: ActionProtocol,
        tool_protocol: str,
        context_manager: ContextManager,
        memory_config: MemoryConfig,
        memory_summarizer: object | None,
        max_rounds: int,
        plan_enabled: bool,
        audit: AuditLogger | None,
        observer: AgentObserver,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.protocol = protocol
        self.tool_protocol = tool_protocol
        self.context_manager = context_manager
        self.memory_config = memory_config
        self.memory_summarizer = memory_summarizer
        self.max_rounds = max_rounds
        self.plan_enabled = plan_enabled
        self.audit = audit
        self.observer = observer
        self.provider_collector = ProviderResponseCollector(provider)

    async def run(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken,
        event_sink: EventSink | None,
    ) -> SessionTurnResult:
        """执行一个任务；锁和 cleanup scope 继续由外层门面持有。"""

        if not task.strip():
            result = RunResult(False, "任务描述不能为空", 0)
            emit(event_sink, RuntimeFailed("input", result.summary))
            return SessionTurnResult(result, context)
        if not isinstance(context.conversation_memory, ConversationMemory):
            result = RunResult(False, "会话记忆状态无效", 0)
            emit(event_sink, RuntimeFailed("memory", result.summary))
            return SessionTurnResult(result, context)
        if context.review_memory_candidate is not None and not isinstance(
            context.review_memory_candidate,
            ConversationMemory,
        ):
            result = RunResult(False, "会话记忆保存候选状态无效", 0)
            emit(event_sink, RuntimeFailed("memory", result.summary))
            return SessionTurnResult(result, context)

        state = AgentRunState.start(self.protocol.system_prompt, task, context)
        tool_context = getattr(self.tools, "context", None)
        scope = getattr(tool_context, "verification_scope", None)
        policy = getattr(tool_context, "workspace_policy", None)
        await self._initialize_verification(state, scope, policy)
        observation = current_task_observation()
        finalizer = TaskFinalizer(
            state,
            cancellation=cancellation,
            verification_scope=scope,
            observation=observation,
            tool_context=tool_context,
            tool_protocol=self.tool_protocol,
            event_sink=event_sink,
        )
        finalizer.publish_state()
        if state.unknown_effects:
            state.verification = "待验证"
            state.evidence = None
            state.verification_required = True
            return finalizer.finish(
                RunResult(
                    False,
                    "文件影响未确认；请检查实际文件并通过 /clear 明确确认",
                    0,
                    modified_files=tuple(state.modified_files),
                    modified_directories=tuple(state.modified_directories),
                    verification="待验证",
                )
            )
        if self.audit is not None:
            try:
                self.audit.prepare()
            except OSError:
                self.observer.on_error(AUDIT_FAILURE_MESSAGE)
                return finalizer.finish(
                    RunResult(False, AUDIT_FAILURE_MESSAGE, 0),
                    rollback_task=True,
                )

        coordinator = self._memory_coordinator()
        provider_tools = self.tools.definitions if self.protocol.tools_enabled else ()
        preparation = await self._prepare_memory(
            coordinator,
            state,
            provider_tools,
            cancellation,
            finalizer,
            completed_rounds=0,
            rollback_task=True,
        )
        if preparation is not None:
            return preparation

        if self.plan_enabled:
            try:
                planning = await PlanningPhase(
                    self.provider_collector.request,
                    self.observer,
                    self._log,
                    self._audit_usage,
                ).run(
                    state.messages,
                    cancellation,
                    event_sink,
                    request_messages=state.request_view(
                        structured_memory=(
                            self.memory_config.compaction == "structured"
                        )
                    ),
                )
            except CancellationError:
                return finalizer.finish(
                    self._result(state, False, "任务已取消", 0),
                    rollback_task=True,
                )
            if planning.audit_failed:
                return finalizer.finish(
                    self._audit_failure(state, 0),
                    rollback_task=True,
                )
            state.merge_usage(planning.usage)

        for round_number in range(1, self.max_rounds + 1):
            if cancellation.is_cancelled:
                return finalizer.finish(
                    self._result(state, False, "任务已取消", round_number - 1),
                    rollback_task=True,
                )
            self.observer.on_round_start(round_number, self.max_rounds)
            emit(event_sink, RoundStarted(round_number, self.max_rounds))
            started = time.perf_counter()
            state.normalize_history()
            preparation = await self._prepare_memory(
                coordinator,
                state,
                provider_tools,
                cancellation,
                finalizer,
                completed_rounds=round_number - 1,
                rollback_task=True,
            )
            if preparation is not None:
                return preparation
            request_view = state.request_view(
                structured_memory=self.memory_config.compaction == "structured"
            )
            snapshot = self.context_manager.prepare(request_view)
            if (
                self.memory_config.compaction == "structured"
                and snapshot.history_truncated
            ):
                return finalizer.finish(
                    self._result(
                        state,
                        False,
                        "结构化记忆模式拒绝静默裁剪历史",
                        round_number - 1,
                    ),
                    rollback_task=True,
                )
            request_messages = list(snapshot.messages)
            if snapshot.history_truncated:
                emit(
                    event_sink,
                    ContextCompacted(
                        before_chars=sum(
                            message.character_budget() for message in state.messages
                        ),
                        after_chars=snapshot.character_count,
                    ),
                )
            try:
                response = await self.provider_collector.request(
                    request_messages,
                    provider_tools,
                    cancellation,
                    event_sink,
                )
            except CancellationError:
                return finalizer.finish(
                    self._result(state, False, "任务已取消", round_number),
                    rollback_task=True,
                )
            except ProviderProtocolError as exc:
                self.observer.on_error(PROTOCOL_FEEDBACK)
                if not self._log(
                    {
                        "round": round_number,
                        "status": "provider_protocol_error",
                        "error_type": type(exc).__name__,
                        "error_chars": len(str(exc)),
                        "duration_ms": elapsed_ms(started),
                    }
                ):
                    return finalizer.finish(
                        self._audit_failure(state, round_number),
                        rollback_task=True,
                    )
                state.messages.append(
                    self._feedback_message(PROTOCOL_FEEDBACK, "protocol_feedback")
                )
                continue
            except ProviderError as exc:
                self.observer.on_error(PROVIDER_FAILURE_MESSAGE)
                if not self._log(
                    {
                        "round": round_number,
                        "status": "provider_error",
                        "error_type": type(exc).__name__,
                        "error_chars": len(str(exc)),
                    }
                ):
                    return finalizer.finish(
                        self._audit_failure(state, round_number),
                        rollback_task=True,
                    )
                return finalizer.finish(
                    self._result(
                        state,
                        False,
                        PROVIDER_FAILURE_MESSAGE,
                        round_number,
                    ),
                    rollback_task=True,
                )

            state.merge_usage(response.usage)
            if response.usage is not None:
                notify_provider_usage(self.observer, round_number, response.usage)
                if not self._audit_usage(round_number, response.usage):
                    return finalizer.finish(
                        self._audit_failure(state, round_number),
                        rollback_task=True,
                    )
            resolved = self.protocol.resolve_action(response)
            state.messages.extend(resolved.assistant_messages)
            state.normalize_history()
            if response.usage is not None:
                assistant_count = len(resolved.assistant_messages)
                normalized_assistant = (
                    state.messages[-assistant_count:] if assistant_count else []
                )
                self.context_manager.record_usage(
                    response.usage,
                    [*request_messages, *normalized_assistant],
                )
            if not resolved.actions:
                feedback = resolved.feedback or self._feedback_message(
                    PROTOCOL_FEEDBACK,
                    "protocol_feedback",
                )
                native_missing_tool = (
                    self.tool_protocol == "native"
                    and resolved.audit_error_type == "ToolCallCountError"
                )
                if native_missing_tool:
                    state.native_missing_tool_responses += 1
                    correction_count = state.native_missing_tool_responses
                    will_stop = correction_count >= MAX_NATIVE_MISSING_TOOL_RESPONSES
                    if not will_stop:
                        suffix = f" 当前累计 {correction_count}/{MAX_NATIVE_MISSING_TOOL_RESPONSES}。"
                        if correction_count == MAX_NATIVE_MISSING_TOOL_RESPONSES - 1:
                            suffix = (
                                f" 当前累计 {correction_count}/{MAX_NATIVE_MISSING_TOOL_RESPONSES}；"
                                "再次发生将停止本任务。"
                            )
                        feedback = self._feedback_message(
                            f"{feedback.content or PROTOCOL_FEEDBACK}{suffix}",
                            "protocol_feedback",
                        )
                    public_error = (
                        NATIVE_TERMINATION_FAILURE
                        if will_stop
                        else feedback.content or PROTOCOL_FEEDBACK
                    )
                else:
                    correction_count = None
                    will_stop = False
                    public_error = feedback.content or PROTOCOL_FEEDBACK
                cancelled_after_response = cancellation.is_cancelled
                if cancelled_after_response:
                    public_error = "任务已取消"
                self.observer.on_error(public_error)
                if not will_stop and not cancelled_after_response:
                    state.messages.append(feedback)
                audit_event: dict[str, Any] = {
                    "round": round_number,
                    "status": "invalid_action",
                    "error_type": (
                        resolved.audit_error_type or "ActionProtocolError"
                    ),
                    "error_chars": len(public_error),
                    "duration_ms": elapsed_ms(started),
                }
                if native_missing_tool:
                    audit_event.update(
                        {
                            "reason": "native_missing_tool_call",
                            "correction_count": correction_count,
                            "correction_limit": MAX_NATIVE_MISSING_TOOL_RESPONSES,
                            "will_stop": will_stop,
                        }
                    )
                if not self._log(
                    audit_event
                ):
                    return finalizer.finish(
                        self._audit_failure(state, round_number),
                        rollback_task=True,
                    )
                # 审计可能触发同步回调，取消也可能在写日志期间从其他线程到达；
                # 审计成功后必须重新读取令牌，不能使用上方仅用于反馈的旧快照。
                if cancellation.is_cancelled:
                    return finalizer.finish(
                        self._result(state, False, "任务已取消", round_number),
                        rollback_task=True,
                    )
                if will_stop:
                    state.messages.append(
                        self._feedback_message(
                            TASK_TERMINATION_NOTICE,
                            TASK_TERMINATION_KIND,
                        )
                    )
                    return finalizer.finish(
                        self._result(
                            state,
                            False,
                            NATIVE_TERMINATION_FAILURE,
                            round_number,
                        )
                    )
                continue

            batch = ToolBatchExecutor(
                tools=self.tools,
                protocol=self.protocol,
                observer=self.observer,
                state=state,
                cancellation=cancellation,
                event_sink=event_sink,
                verification_scope=scope,
                workspace_policy=policy,
                observation=observation,
                publish_state=finalizer.publish_state,
                log=self._log,
                audit_arguments=lambda action, result: audit_arguments(
                    self.tools,
                    action,
                    result,
                ),
            )
            outcome = await batch.execute(resolved, round_number)
            if outcome.stop is ToolBatchStop.AUDIT_FAILED:
                return finalizer.finish(self._audit_failure(state, round_number))
            if outcome.stop is ToolBatchStop.UNKNOWN_EFFECTS:
                return finalizer.finish(
                    self._result(
                        state,
                        False,
                        "文件影响未确认；请检查实际文件并通过 /clear 明确确认",
                        round_number,
                    )
                )
            if outcome.stop is ToolBatchStop.CANCELLED:
                return finalizer.finish(
                    self._result(state, False, "任务已取消", round_number)
                )
            if outcome.stop is ToolBatchStop.FATAL:
                assert outcome.result is not None
                return finalizer.finish(
                    self._result(
                        state,
                        False,
                        outcome.result.output,
                        round_number,
                    )
                )
            if outcome.stop is ToolBatchStop.FINISH:
                assert outcome.result is not None
                return await self._finish_success(
                    state,
                    outcome.result.output,
                    round_number,
                    scope,
                    policy,
                    cancellation,
                    coordinator,
                    finalizer,
                )
            # REPLAN 和 CONTINUE 都由下一轮 Provider 根据完整历史继续决定。

        summary = f"达到最大轮数 {self.max_rounds}，任务已安全停止"
        self.observer.on_error(summary)
        return finalizer.finish(
            self._result(state, False, summary, self.max_rounds)
        )

    async def _initialize_verification(
        self,
        state: AgentRunState,
        scope: object | None,
        policy: object | None,
    ) -> None:
        state.verification_required = (
            state.verification_required
            or bool(state.modified_files)
            or state.evidence is not None
            or state.failed_snapshot is not None
            or state.verification
            in {"通过", "passed", "失败", "failed", "待验证"}
        )
        if isinstance(scope, VerificationScope):
            scope.begin_task()
            if isinstance(self.audit, AuditLogger):
                scope.audit_files = (self.audit.path.absolute(),)
            if state.evidence is not None or state.failed_snapshot is not None:
                current = await run_in_cleanup_thread(scope.capture, policy)
                if not scope.owns(state.evidence) or not state.evidence.is_valid_for(current):
                    state.evidence = None
                    state.verification = "待验证"
                if state.failed_snapshot is not None:
                    if proves_new_file_version(state.failed_snapshot, current):
                        state.failed_snapshot = None
                    else:
                        state.verification = "失败"
            elif state.verification_required:
                state.verification = "待验证"
        else:
            state.evidence = None
            if state.verification_required:
                state.verification = "待验证"
        state.unknown_effects = state.unknown_effects or bool(
            isinstance(scope, VerificationScope) and scope.unknown_effects
        )

    async def _prepare_memory(
        self,
        coordinator: MemoryCoordinator,
        state: AgentRunState,
        provider_tools: tuple[Any, ...] | list[Any],
        cancellation: CancellationToken,
        finalizer: TaskFinalizer,
        *,
        completed_rounds: int,
        rollback_task: bool,
    ) -> SessionTurnResult | None:
        request = self._memory_step_input(state, provider_tools)
        progress = MemoryStepProgress()
        try:
            try:
                await coordinator.prepare_compaction(
                    request,
                    cancellation,
                    progress=progress,
                )
            finally:
                if progress.result is not None:
                    self._apply_memory_step(state, progress.result)
        except CancellationError:
            return finalizer.finish(
                self._result(state, False, "任务已取消", completed_rounds),
                rollback_task=rollback_task,
            )
        except MemoryAuditFailure:
            return finalizer.finish(
                self._audit_failure(state, completed_rounds),
                rollback_task=rollback_task,
            )
        except MemorySummaryError:
            return finalizer.finish(
                self._result(
                    state,
                    False,
                    "上下文记忆整理失败，未发送模型请求",
                    completed_rounds,
                ),
                rollback_task=rollback_task,
            )
        return None

    async def _finish_success(
        self,
        state: AgentRunState,
        summary: str,
        round_number: int,
        scope: object | None,
        policy: object | None,
        cancellation: CancellationToken,
        coordinator: MemoryCoordinator,
        finalizer: TaskFinalizer,
    ) -> SessionTurnResult:
        valid = False
        if state.verification_required and isinstance(scope, VerificationScope):
            current = await run_in_cleanup_thread(scope.capture, policy)
            valid = scope.owns(state.evidence) and state.evidence.is_valid_for(current)
            if state.failed_snapshot is not None and proves_new_file_version(
                state.failed_snapshot,
                current,
            ):
                state.failed_snapshot = None
            if not valid:
                state.evidence = None
                state.verification = (
                    "失败" if state.failed_snapshot is not None else "待验证"
                )
        completed = (
            not cancellation.is_cancelled
            and not state.cleanup_failed
            and (
                not state.verification_required
                or (valid and state.failed_snapshot is None)
            )
        )
        if state.verification_required and state.verification == "待验证":
            summary = (
                f"{summary}；文件修改后尚未运行验证命令，或受覆盖文件状态证据已失效"
            )
        elif state.verification_required and state.verification == "失败":
            summary = f"{summary}；文件修改后的验证失败"
        if completed:
            completed_history = state.normalize_history()
            completed_sequences = [
                message.message_seq
                for message in completed_history
                if message.task_id == state.current_task_id
                and message.message_seq is not None
            ]
            if not completed_sequences:
                return finalizer.finish(
                    self._result(
                        state,
                        False,
                        "无法确定已完成任务的记忆覆盖边界",
                        round_number,
                    )
                )
            state.latest_completed_task_seq = max(completed_sequences)
        if completed and self.memory_config.persistence == "reviewed_summary":
            request = self._memory_step_input(state, ())
            progress = MemoryStepProgress()
            try:
                try:
                    await coordinator.prepare_review_candidate(
                        request,
                        cancellation,
                        progress=progress,
                    )
                finally:
                    if progress.result is not None:
                        self._apply_memory_step(state, progress.result)
            except CancellationError:
                return finalizer.finish(
                    self._result(state, False, "任务已取消", round_number)
                )
            except MemoryAuditFailure:
                return finalizer.finish(self._audit_failure(state, round_number))
            if state.memory_warning:
                summary = f"{summary}；{state.memory_warning}"
        return finalizer.finish(
            self._result(state, completed, summary, round_number)
        )

    def _memory_coordinator(self) -> MemoryCoordinator:
        return MemoryCoordinator(
            self.context_manager,
            self.memory_config,
            self.memory_summarizer,
            self.observer.on_error,
            self._log,
            audit_failure_message=AUDIT_FAILURE_MESSAGE,
        )

    @staticmethod
    def _memory_step_input(
        state: AgentRunState,
        provider_tools: tuple[Any, ...] | list[Any],
    ) -> MemoryStepInput:
        """从权威任务状态构造不含执行引擎引用的记忆快照。"""

        context = replace(
            state.execution_context(),
            messages=tuple(state.messages[state.history_start :]),
            persisted_summary=state.source_context.persisted_summary,
        )
        return MemoryStepInput(
            context=context,
            fixed_messages=tuple(state.messages[: state.history_start]),
            provider_tools=tuple(provider_tools),
            summary_failed=state.memory_summary_failed,
            warning=state.memory_warning,
        )

    @staticmethod
    def _apply_memory_step(
        state: AgentRunState,
        result: MemoryStepResult,
    ) -> None:
        """同步合并协调器拥有的窄字段；调用方保证每步只调用一次。"""

        state.messages[state.history_start :] = result.context.messages
        state.next_message_seq = result.context.next_message_seq
        state.conversation_memory = result.context.conversation_memory
        state.review_memory_candidate = result.context.review_memory_candidate
        state.memory_calls += result.memory_calls
        state.merge_memory_usage(result.memory_usage)
        state.memory_summary_failed = result.summary_failed
        state.memory_compacted = state.memory_compacted or result.compacted
        state.memory_warning = result.warning

    def _log(self, event: dict[str, Any]) -> bool:
        return log_event(
            self.audit,
            self.observer,
            event,
            failure_message=AUDIT_FAILURE_MESSAGE,
        )

    def _audit_usage(self, round_number: int, usage: TokenUsage) -> bool:
        return self._log(audit_usage_event(round_number, usage))

    @staticmethod
    def _feedback_message(content: str, kind: str):
        from tricoder.models import Message

        return Message("user", content, kind=kind)

    @staticmethod
    def _result(
        state: AgentRunState,
        ok: bool,
        summary: str,
        rounds: int,
    ) -> RunResult:
        return RunResult(
            ok,
            summary,
            rounds,
            state.tool_calls,
            tuple(state.modified_files),
            state.verification,
            modified_directories=tuple(state.modified_directories),
        )

    @staticmethod
    def _audit_failure(state: AgentRunState, round_number: int) -> RunResult:
        return audit_failure_result(
            AUDIT_FAILURE_MESSAGE,
            round_number,
            state.tool_calls,
            state.modified_files,
            state.verification,
            state.modified_directories,
        )
