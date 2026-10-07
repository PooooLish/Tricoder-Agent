"""结构化记忆压缩、保存候选和任务状态之间的协调层。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from tricoder.context.manager import ContextManager
from tricoder.context.memory import (
    ConversationMemory,
    MemoryValidationError,
    assign_message_sequences,
    conversation_memory_message,
)
from tricoder.context.summarizer import (
    MemorySummaryError,
    memory_summary_failure_code,
    memory_summary_failure_label,
)
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.models import (
    MemoryConfig,
    MemoryRefreshResult,
    Message,
    SessionContext,
    TokenUsage,
    ToolDefinition,
)


class MemoryAuditFailure(RuntimeError):
    """记忆状态无法留下审计证据时安全终止本轮。"""


@dataclass(frozen=True, slots=True)
class MemoryStepInput:
    """协调器一次调用所需的只读任务快照。"""

    context: SessionContext
    fixed_messages: tuple[Message, ...]
    provider_tools: tuple[ToolDefinition, ...]
    summary_failed: bool
    warning: str


@dataclass(frozen=True, slots=True)
class MemoryStepResult:
    """一次记忆步骤允许交还 Runner 的最小状态增量。"""

    context: SessionContext
    memory_usage: TokenUsage | None
    memory_calls: int
    summary_failed: bool
    compacted: bool
    warning: str


@dataclass(slots=True)
class MemoryStepProgress:
    """异常路径也可读取的本次调用最新不可变结果。"""

    result: MemoryStepResult | None = None


class MemoryCoordinator:
    """协调摘要器和 ContextManager，不持有跨任务可变状态。"""

    def __init__(
        self,
        context_manager: ContextManager,
        memory_config: MemoryConfig,
        memory_summarizer: object | None,
        on_error: Callable[[str], None],
        log: Callable[[dict[str, object]], bool],
        *,
        audit_failure_message: str,
    ) -> None:
        self.context_manager = context_manager
        self.memory_config = memory_config
        self.memory_summarizer = memory_summarizer
        self.on_error = on_error
        self.log = log
        self.audit_failure_message = audit_failure_message

    async def build_review_candidate(
        self,
        context: SessionContext,
        cancellation: CancellationToken,
    ) -> MemoryRefreshResult:
        """从不可变快照生成完整候选；失败时不发布部分结果。"""

        if self.memory_config.persistence != "reviewed_summary":
            raise MemorySummaryError("会话记忆持久化未启用", code="disabled")
        if context.memory_pending_clear:
            raise MemorySummaryError("持久化清除尚未完成", code="pending_clear")
        memory = context.conversation_memory
        review_candidate = context.review_memory_candidate
        if not isinstance(memory, ConversationMemory) or (
            review_candidate is not None
            and not isinstance(review_candidate, ConversationMemory)
        ):
            raise MemorySummaryError("会话记忆状态无效", code="commit")
        if (
            isinstance(review_candidate, ConversationMemory)
            and review_candidate.covered_through < memory.covered_through
        ):
            raise MemorySummaryError(
                "待保存候选落后于已压缩记忆，无法恢复缺失来源",
                code="coverage",
            )
        messages, next_message_seq = assign_message_sequences(
            context.messages,
            context.next_message_seq,
        )
        previous = review_candidate or memory
        target = context.latest_completed_task_seq
        if previous.covered_through > target:
            raise MemorySummaryError("保存候选覆盖边界无效", code="coverage")
        if previous.covered_through == target:
            return MemoryRefreshResult(
                replace(
                    context,
                    messages=messages,
                    next_message_seq=next_message_seq,
                )
            )
        termination_extension = self.context_manager.extend_save_candidate_with_termination(
            previous,
            messages,
            target=target,
            summary_max_chars=self.memory_config.summary_max_chars,
        )
        if termination_extension is not None:
            previous = termination_extension
        if previous.covered_through == target:
            return MemoryRefreshResult(
                replace(
                    context,
                    messages=messages,
                    next_message_seq=next_message_seq,
                    review_memory_candidate=previous,
                )
            )
        plan = self.context_manager.plan_save_candidate(
            messages,
            covered_through=previous.covered_through,
        )
        if not plan.needs_summary or not plan.source_messages:
            raise MemorySummaryError(
                "无法从当前历史恢复未覆盖的已结束任务",
                code="coverage",
            )
        if plan.covered_through != target:
            raise MemorySummaryError(
                "保存候选覆盖目标与已结束任务水位不一致",
                code="coverage",
            )
        summarizer = self.memory_summarizer
        if summarizer is None or not callable(getattr(summarizer, "summarize", None)):
            raise MemorySummaryError("结构化记忆摘要器不可用", code="unavailable")
        batches = (plan.source_messages,)
        input_fits = getattr(summarizer, "source_input_fits", None)
        if callable(input_fits) and not input_fits(previous, plan.source_messages):
            batches = self.context_manager.split_save_source_batches(plan.source_messages)
            if len(batches) != 2 or any(
                not input_fits(previous, batch) for batch in batches
            ):
                raise MemorySummaryError(
                    "两批仍无法容纳完整保存来源",
                    code="input_limit",
                )
        committed = previous
        usage = None
        calls = 0
        for batch in batches:
            cancellation.raise_if_cancelled()
            if callable(input_fits) and not input_fits(committed, batch):
                raise MemorySummaryError(
                    "两批仍无法容纳完整保存来源",
                    code="input_limit",
                )
            batch_plan = self.context_manager.plan_save_candidate(
                batch,
                covered_through=committed.covered_through,
            )
            if not batch_plan.needs_summary:
                raise MemoryValidationError("保存批次没有完整的新任务")
            calls += 1
            summary_result = await summarizer.summarize(
                committed,
                batch,
                cancellation,
            )
            committed = self.context_manager.merge_save_candidate(
                committed,
                messages,
                batch_plan,
                summary_result.candidate,
                summary_max_chars=self.memory_config.summary_max_chars,
            )
            if summary_result.usage is not None:
                usage = (
                    summary_result.usage
                    if usage is None
                    else usage.merge(summary_result.usage)
                )
        cancellation.raise_if_cancelled()
        if committed.covered_through != target:
            raise MemorySummaryError("刷新未完整覆盖最新任务", code="coverage")
        return MemoryRefreshResult(
            replace(
                context,
                messages=messages,
                next_message_seq=next_message_seq,
                review_memory_candidate=committed,
            ),
            memory_usage=usage,
            memory_calls=calls,
        )

    @staticmethod
    def _start_step(
        request: MemoryStepInput,
        progress: MemoryStepProgress,
    ) -> MemoryStepResult:
        result = MemoryStepResult(
            context=request.context,
            memory_usage=None,
            memory_calls=0,
            summary_failed=request.summary_failed,
            compacted=False,
            warning=request.warning,
        )
        progress.result = result
        return result

    @staticmethod
    def _record(
        progress: MemoryStepProgress,
        result: MemoryStepResult,
    ) -> MemoryStepResult:
        progress.result = result
        return result

    @staticmethod
    def _merge_usage(
        current: TokenUsage | None,
        added: TokenUsage | None,
    ) -> TokenUsage | None:
        if added is None:
            return current
        return added if current is None else current.merge(added)

    @staticmethod
    def _normalize_context(context: SessionContext) -> SessionContext:
        messages, next_message_seq = assign_message_sequences(
            context.messages,
            context.next_message_seq,
        )
        return replace(
            context,
            messages=messages,
            next_message_seq=next_message_seq,
        )

    async def prepare_compaction(
        self,
        request: MemoryStepInput,
        cancellation: CancellationToken,
        *,
        progress: MemoryStepProgress,
    ) -> MemoryStepResult:
        """最多两批总结；候选校验和审计成功前不删除历史。"""

        result = self._start_step(request, progress)
        if self.memory_config.compaction != "structured":
            return result
        summarizer = self.memory_summarizer
        if summarizer is None or not callable(getattr(summarizer, "summarize", None)):
            raise MemorySummaryError("结构化记忆摘要器不可用", code="unavailable")
        for _batch in range(2):
            context = self._normalize_context(result.context)
            result = self._record(progress, replace(result, context=context))
            memory_message = conversation_memory_message(
                context.conversation_memory
            )
            fixed = [*request.fixed_messages]
            if memory_message is not None:
                fixed.append(memory_message)
            plan = self.context_manager.plan_compaction(
                context.messages,
                fixed_messages=fixed,
                tools=request.provider_tools,
                trigger_ratio=self.memory_config.trigger_ratio,
                target_ratio=self.memory_config.target_ratio,
                covered_through=context.conversation_memory.covered_through,
            )
            if not plan.needs_compaction:
                if plan.over_hard_limit:
                    raise MemorySummaryError(
                        plan.reason or "完整请求超过上下文硬上限",
                        code="budget",
                    )
                return result
            if result.summary_failed:
                if plan.over_hard_limit:
                    raise MemorySummaryError(
                        "摘要失败且完整请求超过上下文硬上限",
                        code="budget",
                    )
                return result
            result = self._record(
                progress,
                replace(result, memory_calls=result.memory_calls + 1),
            )
            try:
                summary_result = await summarizer.summarize(
                    context.conversation_memory,
                    plan.source_messages,
                    cancellation,
                )
            except CancellationError:
                raise
            except MemorySummaryError as exc:
                result = self._record(
                    progress,
                    replace(result, summary_failed=True),
                )
                self.on_error("会话记忆整理失败；原历史保持不变")
                if not self.log(
                    {
                        "status": "memory_summary_failed",
                        "failure_code": memory_summary_failure_code(exc),
                        "source_count": len(plan.source_messages),
                        "covered_through": plan.covered_through,
                    }
                ):
                    raise MemoryAuditFailure(self.audit_failure_message)
                if plan.over_hard_limit:
                    raise MemorySummaryError(
                        "摘要失败且完整请求超过上下文硬上限",
                        code="budget",
                    )
                return result
            result = self._record(
                progress,
                replace(
                    result,
                    memory_usage=self._merge_usage(
                        result.memory_usage,
                        summary_result.usage,
                    ),
                ),
            )
            try:
                committed = self.context_manager.commit_compaction(
                    context,
                    plan,
                    summary_result.candidate,
                    summary_max_chars=self.memory_config.summary_max_chars,
                )
            except MemoryValidationError as exc:
                # 候选解析成功后仍可能在合并时触发容量、来源或过期校验；
                # 统一转成安全摘要失败，确保原历史不会因底层异常被替换。
                raise MemorySummaryError(
                    "记忆摘要候选提交失败",
                    code="commit",
                ) from exc
            if not self.log(
                {
                    "status": "memory_compacted",
                    "source_count": len(plan.source_messages),
                    "retained_count": len(plan.retained_messages),
                    "revision": committed.conversation_memory.revision,
                    "covered_through": committed.conversation_memory.covered_through,
                }
            ):
                raise MemoryAuditFailure(self.audit_failure_message)
            result = self._record(
                progress,
                replace(result, context=committed, compacted=True),
            )

        context = self._normalize_context(result.context)
        result = self._record(progress, replace(result, context=context))
        memory_message = conversation_memory_message(context.conversation_memory)
        fixed = [*request.fixed_messages]
        if memory_message is not None:
            fixed.append(memory_message)
        final_plan = self.context_manager.plan_compaction(
            context.messages,
            fixed_messages=fixed,
            tools=request.provider_tools,
            trigger_ratio=self.memory_config.trigger_ratio,
            target_ratio=self.memory_config.target_ratio,
            covered_through=context.conversation_memory.covered_through,
        )
        if final_plan.over_hard_limit:
            raise MemorySummaryError("两批摘要后请求仍超过上下文硬上限", code="budget")
        return result

    async def prepare_review_candidate(
        self,
        request: MemoryStepInput,
        cancellation: CancellationToken,
        *,
        progress: MemoryStepProgress,
    ) -> MemoryStepResult:
        """正常结束时生成保存候选，同时保留近期完整对话。"""

        result = self._start_step(request, progress)
        context = request.context
        if (
            self.memory_config.persistence != "reviewed_summary"
            or context.memory_pending_clear
            or result.summary_failed
            or cancellation.is_cancelled
        ):
            return result
        context = self._normalize_context(context)
        result = self._record(progress, replace(result, context=context))
        source_count = 0
        try:
            previous = context.review_memory_candidate or context.conversation_memory
            source_count = sum(
                1
                for message in context.messages
                if message.message_seq is not None
                and previous.covered_through < message.message_seq
                <= context.latest_completed_task_seq
            )
            refreshed = await self.build_review_candidate(context, cancellation)
        except CancellationError:
            raise
        except MemorySummaryError as exc:
            reason = memory_summary_failure_code(exc)
            detail = (
                "无法完整保存，当前候选保持不变"
                if reason in {"input_limit", "coverage"}
                else "执行结果不受影响"
            )
            warning = (
                f"会话记忆候选生成失败（{memory_summary_failure_label(reason)}）；"
                f"{detail}"
            )
            result = self._record(progress, replace(result, warning=warning))
            self.on_error(warning)
            if not self.log(
                {
                    "status": "memory_review_failed",
                    "failure_code": reason,
                    "source_count": source_count,
                    "covered_through": previous.covered_through,
                }
            ):
                raise MemoryAuditFailure(self.audit_failure_message)
            return result
        except MemoryValidationError:
            reason = "commit"
            warning = (
                f"会话记忆候选生成失败（{memory_summary_failure_label(reason)}）；"
                "执行结果不受影响"
            )
            result = self._record(progress, replace(result, warning=warning))
            self.on_error(warning)
            if not self.log(
                {
                    "status": "memory_review_failed",
                    "failure_code": reason,
                    "source_count": source_count,
                    "covered_through": previous.covered_through,
                }
            ):
                raise MemoryAuditFailure(self.audit_failure_message)
            return result
        committed = refreshed.context.review_memory_candidate
        if not isinstance(committed, ConversationMemory):
            raise MemoryAuditFailure(self.audit_failure_message)
        if not self.log(
            {
                "status": "memory_review_candidate",
                "source_count": source_count,
                "revision": committed.revision,
                "covered_through": committed.covered_through,
            }
        ):
            raise MemoryAuditFailure(self.audit_failure_message)
        return self._record(
            progress,
            replace(
                result,
                context=refreshed.context,
                memory_usage=refreshed.memory_usage,
                memory_calls=refreshed.memory_calls,
            ),
        )
