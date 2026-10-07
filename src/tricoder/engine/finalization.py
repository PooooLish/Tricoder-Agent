"""单任务结果收尾、历史回退和可信状态发布。"""

from __future__ import annotations

from dataclasses import replace

from tricoder.context.manager import ContextManager
from tricoder.context.memory import (
    TASK_INCOMPLETE_NOTICE,
    TASK_TERMINATION_KIND,
    assign_message_sequences,
    is_trusted_task_termination,
)
from tricoder.core.cancellation import CancellationToken
from tricoder.core.events import EventSink, RuntimeCompleted, RuntimeFailed
from tricoder.engine.state import AgentRunState
from tricoder.engine.telemetry import emit
from tricoder.models import Message, RunResult, SessionContext, SessionTurnResult
from tricoder.task_cleanup import current_cleanup
from tricoder.workspace.verification import VerificationScope


def close_runtime_failure_context(
    context: SessionContext,
    context_manager: ContextManager,
) -> SessionContext:
    """把 Agent 返回后的宿主失败追加为单次、可验证的终止事实。

    Runtime 的最终扫描、取消提交或清理门禁可能在 Agent 已完成自身收尾后
    才否决结果。这里不伪造工具调用或业务完成，只闭合最后一个真实任务；
    如果历史本身不完整，则保留旧完成水位，避免越过缺失的工具结果。
    """

    messages, next_message_seq = assign_message_sequences(
        context.messages,
        context.next_message_seq,
    )
    task_messages = [message for message in messages if message.kind == "task"]
    if not task_messages:
        return replace(
            context,
            messages=messages,
            next_message_seq=next_message_seq,
        )
    current_task_id = task_messages[-1].task_id
    if current_task_id is None:
        return replace(
            context,
            messages=messages,
            next_message_seq=next_message_seq,
        )
    if not any(
        message.task_id == current_task_id
        and is_trusted_task_termination(message)
        for message in messages
    ):
        messages = (*messages, Message(
            "user",
            TASK_INCOMPLETE_NOTICE,
            kind=TASK_TERMINATION_KIND,
            task_id=current_task_id,
        ))
        messages, next_message_seq = assign_message_sequences(
            messages,
            next_message_seq,
        )
    boundary = context_manager.closed_task_boundary(
        messages,
        covered_through=context.latest_completed_task_seq,
        current_task_id=current_task_id,
    )
    return replace(
        context,
        messages=messages,
        next_message_seq=next_message_seq,
        latest_completed_task_seq=(
            context.latest_completed_task_seq if boundary is None else boundary
        ),
    )


class TaskFinalizer:
    """把当前 RunState 转换为公开结果；不保存数据库、不释放宿主锁。"""

    def __init__(
        self,
        state: AgentRunState,
        *,
        cancellation: CancellationToken,
        verification_scope: object | None,
        observation: object | None,
        tool_context: object | None,
        tool_protocol: str,
        context_manager: ContextManager,
        event_sink: EventSink | None,
    ) -> None:
        self.state = state
        self.cancellation = cancellation
        self.verification_scope = verification_scope
        self.observation = observation
        self.tool_context = tool_context
        self.tool_protocol = tool_protocol
        self.context_manager = context_manager
        self.event_sink = event_sink

    def publish_state(self) -> None:
        """发布已核验的文件与验证事实。"""

        self.state.publish(self.observation, self.tool_context)

    def close_task(self, result: RunResult) -> bool:
        """闭合失败事实，并只在连续历史完整时推进已结束任务水位。"""

        state = self.state
        # 专用终止标记通常在循环末尾追加，尚未获得 task_id；先统一编号，
        # 否则会误判为缺失并重复追加通用标记。
        state.normalize_history()
        if not result.ok and not any(
            message.task_id == state.current_task_id
            and is_trusted_task_termination(message)
            for message in state.messages
        ):
            state.messages.append(
                Message(
                    "user",
                    TASK_INCOMPLETE_NOTICE,
                    kind=TASK_TERMINATION_KIND,
                    task_id=state.current_task_id,
                )
            )
        normalized = state.normalize_history()
        boundary = self.context_manager.closed_task_boundary(
            normalized,
            covered_through=state.latest_completed_task_seq,
            current_task_id=state.current_task_id,
        )
        if boundary is None:
            return False
        state.latest_completed_task_seq = boundary
        return True

    def finish(
        self,
        result: RunResult,
        *,
        rollback_task: bool = False,
    ) -> SessionTurnResult:
        """完成本轮结果，同时保持失败回退和清理失败语义。"""

        state = self.state
        cleanup = current_cleanup()
        cleanup_bad = (
            result.cleanup_failed
            or state.cleanup_failed
            or bool(cleanup is not None and cleanup.failed)
        )
        if self.cancellation.is_cancelled or cleanup_bad:
            if isinstance(self.verification_scope, VerificationScope):
                self.verification_scope.revoke()
            if state.evidence is not None or state.verification_required:
                state.evidence = None
                state.verification_required = True
                state.verification = (
                    "失败" if state.failed_snapshot is not None else "待验证"
                )
        self.publish_state()
        current_complete = self.close_task(result)
        normalized_history = state.normalize_history()
        context = state.source_context
        if rollback_task and not current_complete and state.memory_compacted:
            current_index = next(
                (
                    index
                    for index, message in enumerate(normalized_history)
                    if message.kind == "task"
                    and message.task_id == state.current_task_id
                ),
                len(normalized_history),
            )
            rollback_history = normalized_history[:current_index]
            rollback_next_message_seq = state.next_message_seq
        else:
            rollback_history = context.messages
            rollback_next_message_seq = context.next_message_seq
        turn = SessionTurnResult(
            replace(
                result,
                usage=state.accumulated_usage,
                unknown_effects=state.unknown_effects,
                verification=state.verification,
                cleanup_failed=cleanup_bad,
                task_validation=state.validation.report(),
                current_verification_required=state.verification_required,
                verification_obligation=state.verification_obligation,
                pending_verification_paths=state.pending_verification_paths,
            ),
            SessionContext(
                messages=(
                    rollback_history
                    if rollback_task and not current_complete
                    else normalized_history
                ),
                persisted_summary=context.persisted_summary,
                modified_files=tuple(state.modified_files),
                modified_directories=tuple(state.modified_directories),
                verification=state.verification,
                unknown_effects=state.unknown_effects,
                verification_evidence=state.evidence,
                verification_failure=state.failed_snapshot,
                verification_required=state.verification_required,
                verification_obligation=state.verification_obligation,
                pending_verification_paths=state.pending_verification_paths,
                conversation_memory=state.conversation_memory,
                review_memory_candidate=state.review_memory_candidate,
                next_message_seq=(
                    rollback_next_message_seq
                    if rollback_task and not current_complete
                    else state.next_message_seq
                ),
                latest_completed_task_seq=state.latest_completed_task_seq,
                persisted_memory_revision=context.persisted_memory_revision,
                memory_pending_clear=context.memory_pending_clear,
            ),
            file_effects_observed=state.file_effects_observed,
            memory_usage=state.memory_usage,
            memory_calls=state.memory_calls,
            memory_warning=state.memory_warning,
        )
        if turn.result.ok:
            emit(self.event_sink, RuntimeCompleted(turn.result))
        else:
            category = "cancelled" if result.summary == "任务已取消" else "runtime"
            emit(self.event_sink, RuntimeFailed(category, result.summary))
        return turn
