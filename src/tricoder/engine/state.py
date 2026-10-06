"""单次 Agent 任务的可变状态所有权。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tricoder.context import assign_message_sequences, conversation_memory_message
from tricoder.context.history import complete_round_tail
from tricoder.context.memory import ConversationMemory
from tricoder.models import Message, SessionContext, TokenUsage, ToolResult


class ToolBatchStop(str, Enum):
    """一个工具批次交还主循环时的有限状态。"""

    CONTINUE = "continue"
    REPLAN = "replan"
    FINISH = "finish"
    CANCELLED = "cancelled"
    FATAL = "fatal"
    AUDIT_FAILED = "audit_failed"
    UNKNOWN_EFFECTS = "unknown_effects"


@dataclass(frozen=True, slots=True)
class ToolBatchOutcome:
    """工具批次结果；副作用和消息已经提交到同一个 RunState。"""

    stop: ToolBatchStop
    result: ToolResult | None = None


@dataclass(slots=True)
class AgentRunState:
    """只在一次 ``run_with_context`` 调用内存活的权威可变状态。

    Provider 请求视图由 :meth:`request_view` 临时装配；``messages`` 始终是
    可持久化的原始历史，结构化记忆不会被写入其中。
    """

    source_context: SessionContext
    messages: list[Message]
    history_start: int
    next_message_seq: int
    current_task_id: str | None
    conversation_memory: ConversationMemory
    review_memory_candidate: ConversationMemory | None
    latest_completed_task_seq: int
    native_missing_tool_responses: int = 0
    modified_files: list[str] = field(default_factory=list)
    modified_directories: list[str] = field(default_factory=list)
    verification: str = "未运行"
    evidence: Any = None
    failed_snapshot: Any = None
    verification_required: bool = False
    unknown_effects: bool = False
    tool_calls: int = 0
    cleanup_failed: bool = False
    file_effects_observed: bool = True
    accumulated_usage: TokenUsage | None = None
    memory_usage: TokenUsage | None = None
    memory_calls: int = 0
    memory_summary_failed: bool = False
    memory_compacted: bool = False
    memory_warning: str = ""

    @classmethod
    def start(
        cls,
        system_prompt: str,
        task: str,
        context: SessionContext,
    ) -> AgentRunState:
        """从不可变 Context 创建新的任务状态，不修改调用方对象。"""

        messages = [Message("system", system_prompt)]
        if context.persisted_summary:
            messages.append(
                Message(
                    "user",
                    f"持久化会话摘要：{context.persisted_summary}",
                    kind="persisted_summary",
                )
            )
        if context.workspace_change_notice:
            messages.append(
                Message(
                    "system",
                    context.workspace_change_notice,
                    kind="workspace_change",
                )
            )
        history_start = len(messages)
        base_history, next_sequence = assign_message_sequences(
            context.messages,
            context.next_message_seq,
        )
        messages.extend(base_history)
        messages.append(Message("user", f"用户任务：{task.strip()}", kind="task"))
        normalized, next_sequence = assign_message_sequences(
            tuple(messages[history_start:]),
            next_sequence,
        )
        messages[history_start:] = normalized
        current_task_id = normalized[-1].task_id
        return cls(
            source_context=context,
            messages=messages,
            history_start=history_start,
            next_message_seq=next_sequence,
            current_task_id=current_task_id,
            conversation_memory=context.conversation_memory,
            review_memory_candidate=context.review_memory_candidate,
            latest_completed_task_seq=context.latest_completed_task_seq,
            modified_files=list(context.modified_files),
            modified_directories=list(context.modified_directories),
            verification=context.verification,
            evidence=context.verification_evidence,
            failed_snapshot=context.verification_failure,
            verification_required=context.verification_required,
            unknown_effects=context.unknown_effects,
        )

    def normalize_history(self) -> tuple[Message, ...]:
        """只为新增历史分配一次序号，并返回原始会话历史。"""

        normalized, self.next_message_seq = assign_message_sequences(
            tuple(self.messages[self.history_start :]),
            self.next_message_seq,
        )
        self.messages[self.history_start :] = normalized
        return normalized

    def request_view(self, *, structured_memory: bool) -> list[Message]:
        """装配临时 Provider 视图，不改变原始历史。"""

        prefix = list(self.messages[: self.history_start])
        if structured_memory:
            memory_message = conversation_memory_message(self.conversation_memory)
            if memory_message is not None:
                prefix.append(memory_message)
        return [*prefix, *self.messages[self.history_start :]]

    def execution_context(self) -> SessionContext:
        """提供副作用状态机所需的最小 Context 快照。"""

        context = self.source_context
        return SessionContext(
            modified_files=tuple(self.modified_files),
            modified_directories=tuple(self.modified_directories),
            verification=self.verification,
            unknown_effects=self.unknown_effects,
            verification_evidence=self.evidence,
            verification_failure=self.failed_snapshot,
            verification_required=self.verification_required,
            conversation_memory=self.conversation_memory,
            review_memory_candidate=self.review_memory_candidate,
            next_message_seq=self.next_message_seq,
            latest_completed_task_seq=self.latest_completed_task_seq,
            persisted_memory_revision=context.persisted_memory_revision,
            memory_pending_clear=context.memory_pending_clear,
        )

    def publish(self, observation: object | None, tool_context: object | None) -> None:
        """向任务观察器发布已核验事实，不发布 Provider 临时视图。"""

        if observation is None:
            return
        journal = getattr(tool_context, "change_journal", None)
        observation.publish(
            self.execution_context(),
            effects_observed=self.file_effects_observed,
            journal_revision=(
                journal.active_revision if journal is not None else None
            ),
        )

    def current_task_has_complete_round(self, tool_protocol: str) -> bool:
        """当前任务已经持有完整工具回合时返回 ``True``。"""

        current_task_index = max(
            index
            for index, message in enumerate(self.messages)
            if message.kind == "task"
        )
        return any(
            complete_round_tail(self.messages, index, tool_protocol) is not None
            for index in range(current_task_index, len(self.messages) - 1)
        )

    def merge_usage(self, usage: TokenUsage | None) -> None:
        """累加业务 Provider 用量；缺失用量保持缺失而非伪造为零。"""

        if usage is None:
            return
        self.accumulated_usage = (
            usage
            if self.accumulated_usage is None
            else self.accumulated_usage.merge(usage)
        )

    def merge_memory_usage(self, usage: TokenUsage | None) -> None:
        """单独累加记忆摘要用量。"""

        if usage is None:
            return
        self.memory_usage = (
            usage if self.memory_usage is None else self.memory_usage.merge(usage)
        )
