"""单任务结果收尾、历史回退和可信状态发布。"""

from __future__ import annotations

from dataclasses import replace

from tricoder.core.cancellation import CancellationToken
from tricoder.core.events import EventSink, RuntimeCompleted, RuntimeFailed
from tricoder.engine.state import AgentRunState
from tricoder.engine.telemetry import emit
from tricoder.models import RunResult, SessionContext, SessionTurnResult
from tricoder.task_cleanup import current_cleanup
from tricoder.workspace.verification import VerificationScope


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
        event_sink: EventSink | None,
    ) -> None:
        self.state = state
        self.cancellation = cancellation
        self.verification_scope = verification_scope
        self.observation = observation
        self.tool_context = tool_context
        self.tool_protocol = tool_protocol
        self.event_sink = event_sink

    def publish_state(self) -> None:
        """发布已核验的文件与验证事实。"""

        self.state.publish(self.observation, self.tool_context)

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
        normalized_history = state.normalize_history()
        current_complete = state.current_task_has_complete_round(self.tool_protocol)
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
