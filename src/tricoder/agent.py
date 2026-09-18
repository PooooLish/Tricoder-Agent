"""线性消息历史驱动的 Coding Agent 循环。"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import replace
from typing import Any, Protocol, runtime_checkable

from tricoder.audit import AuditLogger
from tricoder.execution_state import (
    EffectState, ErrorCode, FileEffects, RecoveryAction, should_stop_task,
)
from tricoder.core.cancellation import CancellationError, CancellationToken, NativeCancellationError
from tricoder.task_cleanup import TaskCleanup, current_cleanup, run_in_cleanup_thread, task_cleanup_scope
from tricoder.core.events import (
    AgentEvent,
    ApprovalRequested,
    ContextCompacted,
    EventSink,
    PlanningCompleted,
    ProviderCompleted,
    RoundStarted,
    RuntimeCompleted,
    RuntimeFailed,
    TextDelta,
    ThinkingDelta,
    ToolCallCompleted,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    UsageReported,
)
from tricoder.context import (
    CONTEXT_COMPACTION_NOTICE,
    ContextBudget,
    ContextManager,
    assign_message_sequences,
    conversation_memory_message,
)
from tricoder.context.memory import ConversationMemory, MemoryValidationError
from tricoder.context.summarizer import (
    MemorySummarizer,
    MemorySummaryError,
    memory_summary_failure_code,
    memory_summary_failure_label,
)
from tricoder.extensions.models import ToolOrigin
from tricoder.models import (
    Message,
    MemoryConfig,
    ProviderResponse,
    RunResult,
    SessionContext,
    SessionTurnResult,
    TokenUsage,
    ToolAction,
    ToolCall,
    ToolDefinition,
    ToolResult,
    tool_failure,
)
from tricoder.policy import PolicyError
from tricoder.verification import VerificationScope, proves_new_file_version
from tricoder.task_observation import apply_tool_transition, current_task_observation, task_observation_scope
from tricoder.providers import ModelProvider, ProviderError, ProviderProtocolError
from tricoder.protocols import (
    COMMON_SYSTEM_PROMPT,
    LEGACY_JSON_PROMPT,
    LEGACY_SYSTEM_PROMPT,
    NATIVE_TEXT_FEEDBACK,
    PROTOCOL_FEEDBACK,
    SYSTEM_PROMPT,
    ActionProtocol,
    ResolvedAction,
    _PROTOCOLS,
    parse_action,
)
from tricoder.tools import ToolRegistry


AUDIT_FAILURE_MESSAGE = "无法写入审计日志，运行已安全停止"
PROVIDER_FAILURE_MESSAGE = "模型请求失败，运行已安全停止"
PLANNING_PROMPT = """在开始执行前，请先输出一份简短的分步执行计划。
只输出计划，不要调用工具，不要添加任何额外说明。
计划格式为 JSON：
{"steps": ["第 1 步...", "第 2 步...", "第 3 步..."]}
步骤必须具体、可执行，数量控制在 3 到 8 步。"""

# 规划阶段审计失败时的终止哨兵。
_PLAN_ABORT = object()


class _MemoryAuditFailure(RuntimeError):
    """记忆状态无法留下审计证据时，按既有安全边界终止本轮。"""


def _is_complete_tool_round(
    assistant: Message,
    tool_result: Message,
    tool_protocol: str | None = None,
) -> bool:
    """按协议校验一组 assistant 动作和关联工具结果。

    ``tool_protocol`` 为 None 时按各协议的宽松判定匹配，供历史压缩逻辑使用；
    指定协议时使用该协议的严格判定。新增协议无需再修改本函数。
    """

    if assistant.role != "assistant":
        return False
    if tool_protocol is None:
        return any(
            protocol.complete_round_loose(assistant, tool_result)
            for protocol in _PROTOCOLS.values()
        )
    return _PROTOCOLS[tool_protocol].complete_round(assistant, tool_result)


def _complete_round_tail(
    messages: list[Message],
    index: int,
    tool_protocol: str | None = None,
) -> int | None:
    """若 ``messages[index:]`` 以完整工具回合开头，返回回合结束后的下标；否则 None。

    native：一个 assistant 携带 N 个 ``tool_calls``，后跟 N 个按 id 匹配的
    ``tool`` 结果；legacy：assistant（无 tool_calls）+ 一个 user 结果。
    ``tool_protocol`` 为 None 时按宽松语义判定（legacy 不检查 kind）。
    """

    assistant = messages[index]
    if assistant.role != "assistant":
        return None
    if assistant.tool_calls:
        if tool_protocol not in {None, "native"}:
            return None
        cursor = index + 1
        for call in assistant.tool_calls:
            if cursor >= len(messages):
                return None
            result = messages[cursor]
            if (
                result.role != "tool"
                or result.kind != "tool_result"
                or result.tool_call_id != call.id
            ):
                return None
            cursor += 1
        return cursor
    if tool_protocol not in {None, "legacy_json"}:
        return None
    if index + 1 >= len(messages):
        return None
    result = messages[index + 1]
    if result.role != "user":
        return None
    if tool_protocol == "legacy_json" and result.kind != "tool_result":
        return None
    return index + 2


def compact_messages(messages: list[Message], max_chars: int) -> list[Message]:
    """字符预算兼容代理；具体回合压缩由 ``ContextManager`` 负责。"""

    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_context_chars 必须大于 0")
    manager = ContextManager(
        ContextBudget(max_chars=max_chars),
        tuple(_PROTOCOLS.values()),
    )
    return list(manager.prepare_generic(messages).messages)


def compact_session_messages(
    messages: list[Message],
    max_chars: int,
    tool_protocol: str,
) -> list[Message]:
    """字符预算兼容代理；保持旧调用点和保留顺序。"""

    manager = ContextManager(
        ContextBudget(max_chars=max_chars),
        _PROTOCOLS[tool_protocol],
    )
    return list(manager.prepare(messages).messages)


@runtime_checkable
class AgentObserver(Protocol):
    """接收 Agent 的公开运行事件，不接触隐藏推理或凭据。"""

    def on_round_start(self, round_number: int, max_rounds: int) -> None: ...

    def on_action(self, action: ToolAction) -> None: ...

    def on_tool_result(
        self,
        action: ToolAction,
        result: Any,
        duration_ms: int,
    ) -> None: ...

    def on_error(self, message: str) -> None: ...


@runtime_checkable
class ProviderUsageObserver(Protocol):
    """可选接收逐轮归一化 Provider 用量。"""

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None: ...


def _notify_provider_usage(
    observer: AgentObserver,
    round_number: int,
    usage: TokenUsage,
) -> None:
    if isinstance(observer, ProviderUsageObserver):
        observer.on_provider_usage(round_number, usage)


class NullObserver:
    """在库调用或测试中保持完全静默的默认观察者。"""

    def on_round_start(self, round_number: int, max_rounds: int) -> None:
        return None

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None:
        return None

    def on_action(self, action: ToolAction) -> None:
        return None

    def on_tool_result(self, action: ToolAction, result: Any, duration_ms: int) -> None:
        return None

    def on_error(self, message: str) -> None:
        return None


class CodingAgent:
    """在最大轮数限制内请求动作、执行工具并回填结果。"""

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        *,
        max_rounds: int = 30,
        max_context_chars: int = 80_000,
        audit: AuditLogger | None = None,
        observer: AgentObserver | None = None,
        tool_protocol: str = "native",
        plan_enabled: bool = True,
        memory_config: MemoryConfig | None = None,
        memory_summarizer: object | None = None,
    ) -> None:
        if max_rounds <= 0:
            raise ValueError("max_rounds 必须大于 0")
        if (
            not isinstance(max_context_chars, int)
            or isinstance(max_context_chars, bool)
            or max_context_chars <= 0
        ):
            raise ValueError("max_context_chars 必须大于 0")
        if tool_protocol not in {"native", "legacy_json"}:
            raise ValueError("tool_protocol 必须是 native 或 legacy_json")
        if not isinstance(plan_enabled, bool):
            raise ValueError("plan_enabled 必须是布尔值")
        self.provider = provider
        self.tools = tools
        # 非标准 registry 没有资源交接接口时，Agent 本身仍持有旧任务 pending 资源。
        self._cleanup_owner = TaskCleanup()
        self.max_rounds = max_rounds
        self.max_context_chars = max_context_chars
        self.audit = audit
        self.observer = observer or NullObserver()
        self.tool_protocol = tool_protocol
        self.plan_enabled = plan_enabled
        self.memory_config = memory_config or MemoryConfig()
        self.memory_summarizer = memory_summarizer
        if self.memory_config.compaction == "structured" and self.memory_summarizer is None:
            self.memory_summarizer = MemorySummarizer(provider, self.memory_config)
        self._protocol: ActionProtocol = _PROTOCOLS[tool_protocol]
        # 旧配置仍以字符数命名，因此同时保留字符硬上限；同值 token 上限
        # 让 Provider usage 可在字符估算明显偏低时触发完整回合压缩。
        self.context_manager = ContextManager(
            ContextBudget(
                max_tokens=max_context_chars,
                max_chars=max_context_chars,
            ),
            self._protocol,
        )

    def run(
        self,
        task: str,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ) -> RunResult:
        """保持一次性运行接口的返回类型不变。"""

        return self.run_with_context(
            task,
            SessionContext(),
            cancellation=cancellation,
            event_sink=event_sink,
        ).result

    def run_with_context(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ) -> SessionTurnResult:
        """同步兼容入口；已有事件循环中必须调用异步接口。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run_with_context_async(
                    task,
                    context,
                    cancellation=cancellation,
                    event_sink=event_sink,
                )
            )
        raise RuntimeError("同步 Agent 入口不能在已运行的事件循环中调用")

    async def run_with_context_async(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ) -> SessionTurnResult:
        """在不可变会话上下文上异步执行任务，并发布类型化事件。"""

        if self._cleanup_owner.blocks(current_cleanup()) or getattr(self.tools, "has_pending_cleanup", False):
            result = RunResult(False, "旧任务资源清理尚未确认，禁止复用执行资源", 0, cleanup_failed=True)
            self._emit(event_sink, RuntimeFailed("runtime", result.summary))
            return SessionTurnResult(result, context)
        # 每次调用使用独立子令牌：父级取消仍向下传播，而原生 Task.cancel 只终止
        # 本次调用，不能反向污染由宿主持有并可能复用的父令牌。
        call_cancellation = cancellation.create_child() if cancellation is not None else CancellationToken()
        with task_cleanup_scope(self._retain_task_cleanup, worker_owner=self._cleanup_owner), task_observation_scope():
            try:
                return await self._run_with_context_owned(
                    task, context, cancellation=call_cancellation, event_sink=event_sink,
                )
            except asyncio.CancelledError:
                # 线程 worker 可能仍停在审批等同步屏障；必须在 cleanup scope 交接前
                # 让其提交前检查看到取消，再保持调用者的原生异常身份与参数。
                call_cancellation.cancel()
                raise

    def _retain_task_cleanup(self, scope: TaskCleanup) -> None:
        retain = getattr(self.tools, "retain_cleanup", None)
        if callable(retain):
            retain(scope)
        else:
            scope.handoff_to(self._cleanup_owner)

    async def _run_with_context_owned(
        self, task: str, context: SessionContext, *,
        cancellation: CancellationToken | None = None, event_sink: EventSink | None = None,
    ) -> SessionTurnResult:

        cancellation = cancellation or CancellationToken()

        if not task.strip():
            result = RunResult(False, "任务描述不能为空", 0)
            self._emit(event_sink, RuntimeFailed("input", result.summary))
            return SessionTurnResult(result, context)
        system_prompt = self._protocol.system_prompt
        messages = [Message("system", system_prompt)]
        if context.persisted_summary:
            messages.append(
                Message(
                    "user",
                    f"持久化会话摘要：{context.persisted_summary}",
                    kind="persisted_summary",
                )
            )
        history_start = len(messages)
        base_history, base_next_message_seq = assign_message_sequences(
            context.messages,
            context.next_message_seq,
        )
        next_message_seq = base_next_message_seq
        messages.extend(base_history)
        messages.append(Message("user", f"用户任务：{task.strip()}", kind="task"))

        def normalize_history() -> tuple[Message, ...]:
            """在进入 Provider 或交付 Context 前只为新增历史分配一次序号。"""

            nonlocal next_message_seq
            normalized, next_message_seq = assign_message_sequences(
                tuple(messages[history_start:]),
                next_message_seq,
            )
            messages[history_start:] = normalized
            return normalized

        initial_history = normalize_history()
        current_task_id = initial_history[-1].task_id
        conversation_memory = context.conversation_memory
        if not isinstance(conversation_memory, ConversationMemory):
            result = RunResult(False, "会话记忆状态无效", 0)
            self._emit(event_sink, RuntimeFailed("memory", result.summary))
            return SessionTurnResult(result, context)
        tool_calls = 0
        modified_files = list(context.modified_files)
        verification = context.verification
        evidence = context.verification_evidence
        failed_snapshot = context.verification_failure
        verification_required = (context.verification_required or bool(modified_files)
                                 or evidence is not None or failed_snapshot is not None
                                 or verification in {"通过", "passed", "失败", "failed", "待验证"})
        tool_context = getattr(self.tools, "context", None)
        scope = getattr(tool_context, "verification_scope", None)
        policy = getattr(tool_context, "workspace_policy", None)
        if isinstance(scope, VerificationScope):
            scope.begin_task()
            # 只排除当前本地审计器的精确文件；普通 runtime 或项目 ignore 不受信任。
            if isinstance(self.audit, AuditLogger):
                scope.audit_files = (self.audit.path.absolute(),)
            if evidence is not None or failed_snapshot is not None:
                current = await run_in_cleanup_thread(scope.capture, policy)
                if not scope.owns(evidence) or not evidence.is_valid_for(current):
                    evidence = None
                    verification = "待验证"
                if failed_snapshot is not None:
                    if proves_new_file_version(failed_snapshot, current):
                        failed_snapshot = None
                    else:
                        verification = "失败"
            elif verification_required:
                verification = "待验证"
        else:
            evidence = None
            if verification_required:
                verification = "待验证"
        unknown_effects = context.unknown_effects or bool(
            isinstance(scope, VerificationScope) and scope.unknown_effects)
        cleanup_failed = False
        file_effects_observed = True
        accumulated_usage: TokenUsage | None = None
        memory_usage: TokenUsage | None = None
        memory_calls = 0
        memory_summary_failed = False
        memory_compacted = False
        memory_warning = ""
        observation = current_task_observation()

        def request_view() -> list[Message]:
            """把结构化记忆仅注入临时请求视图，绝不追加到原始历史。"""

            prefix = list(messages[:history_start])
            if self.memory_config.compaction == "structured":
                memory_message = conversation_memory_message(conversation_memory)
                if memory_message is not None:
                    prefix.append(memory_message)
            return [*prefix, *messages[history_start:]]

        async def prepare_structured_memory(
            provider_tools: tuple[ToolDefinition, ...] | list[ToolDefinition],
        ) -> None:
            """最多两批总结；候选校验成功前绝不删除历史。"""

            nonlocal conversation_memory, memory_usage, memory_calls
            nonlocal memory_summary_failed, memory_compacted
            if self.memory_config.compaction != "structured":
                return
            summarizer = self.memory_summarizer
            if summarizer is None or not callable(getattr(summarizer, "summarize", None)):
                raise MemorySummaryError(
                    "结构化记忆摘要器不可用",
                    code="unavailable",
                )
            for _batch in range(2):
                normalize_history()
                memory_message = conversation_memory_message(conversation_memory)
                fixed = [*messages[:history_start]]
                if memory_message is not None:
                    fixed.append(memory_message)
                plan = self.context_manager.plan_compaction(
                    tuple(messages[history_start:]),
                    fixed_messages=fixed,
                    tools=tuple(provider_tools),
                    trigger_ratio=self.memory_config.trigger_ratio,
                    target_ratio=self.memory_config.target_ratio,
                    covered_through=conversation_memory.covered_through,
                )
                if not plan.needs_compaction:
                    if plan.over_hard_limit:
                        raise MemorySummaryError(
                            plan.reason or "完整请求超过上下文硬上限",
                            code="budget",
                        )
                    return
                if memory_summary_failed:
                    if plan.over_hard_limit:
                        raise MemorySummaryError(
                            "摘要失败且完整请求超过上下文硬上限",
                            code="budget",
                        )
                    return
                memory_calls += 1
                try:
                    summary_result = await summarizer.summarize(
                        conversation_memory,
                        plan.source_messages,
                        cancellation,
                    )
                except CancellationError:
                    raise
                except MemorySummaryError as exc:
                    memory_summary_failed = True
                    self.observer.on_error("会话记忆整理失败；原历史保持不变")
                    if not self._log(
                        {
                            "status": "memory_summary_failed",
                            "failure_code": memory_summary_failure_code(exc),
                            "source_count": len(plan.source_messages),
                            "covered_through": plan.covered_through,
                        }
                    ):
                        raise _MemoryAuditFailure(AUDIT_FAILURE_MESSAGE)
                    if plan.over_hard_limit:
                        raise MemorySummaryError(
                            "摘要失败且完整请求超过上下文硬上限",
                            code="budget",
                        )
                    return
                if summary_result.usage is not None:
                    memory_usage = (
                        summary_result.usage
                        if memory_usage is None
                        else memory_usage.merge(summary_result.usage)
                    )
                transient = SessionContext(
                    messages=tuple(messages[history_start:]),
                    persisted_summary=context.persisted_summary,
                    modified_files=tuple(modified_files),
                    verification=verification,
                    unknown_effects=unknown_effects,
                    verification_evidence=evidence,
                    verification_failure=failed_snapshot,
                    verification_required=verification_required,
                    conversation_memory=conversation_memory,
                    next_message_seq=next_message_seq,
                    persisted_memory_revision=context.persisted_memory_revision,
                    memory_pending_clear=context.memory_pending_clear,
                )
                committed = self.context_manager.commit_compaction(
                    transient,
                    plan,
                    summary_result.candidate,
                    summary_max_chars=self.memory_config.summary_max_chars,
                )
                if not self._log(
                    {
                        "status": "memory_compacted",
                        "source_count": len(plan.source_messages),
                        "retained_count": len(plan.retained_messages),
                        "revision": committed.conversation_memory.revision,
                        "covered_through": committed.conversation_memory.covered_through,
                    }
                ):
                    raise _MemoryAuditFailure(AUDIT_FAILURE_MESSAGE)
                conversation_memory = committed.conversation_memory
                messages[history_start:] = committed.messages
                memory_compacted = True

            normalize_history()
            memory_message = conversation_memory_message(conversation_memory)
            fixed = [*messages[:history_start]]
            if memory_message is not None:
                fixed.append(memory_message)
            final_plan = self.context_manager.plan_compaction(
                tuple(messages[history_start:]),
                fixed_messages=fixed,
                tools=tuple(provider_tools),
                trigger_ratio=self.memory_config.trigger_ratio,
                target_ratio=self.memory_config.target_ratio,
                covered_through=conversation_memory.covered_through,
            )
            if final_plan.over_hard_limit:
                raise MemorySummaryError(
                    "两批摘要后请求仍超过上下文硬上限",
                    code="budget",
                )

        async def prepare_review_memory() -> None:
            """正常结束时只整理较早闭合任务，保留最近两项完整对话。"""

            nonlocal conversation_memory, memory_usage, memory_calls, memory_warning
            if (
                self.memory_config.persistence != "reviewed_summary"
                or context.memory_pending_clear
                or memory_summary_failed
                or cancellation.is_cancelled
            ):
                return
            normalize_history()
            plan = self.context_manager.plan_review_compaction(
                tuple(messages[history_start:]),
                covered_through=conversation_memory.covered_through,
            )
            if not plan.needs_compaction:
                return
            summarizer = self.memory_summarizer
            if summarizer is None or not callable(getattr(summarizer, "summarize", None)):
                memory_warning = "会话记忆候选未生成"
                return
            memory_calls += 1
            try:
                summary_result = await summarizer.summarize(
                    conversation_memory,
                    plan.source_messages,
                    cancellation,
                )
                transient = SessionContext(
                    messages=tuple(messages[history_start:]),
                    persisted_summary=context.persisted_summary,
                    modified_files=tuple(modified_files),
                    verification=verification,
                    unknown_effects=unknown_effects,
                    verification_evidence=evidence,
                    verification_failure=failed_snapshot,
                    verification_required=verification_required,
                    conversation_memory=conversation_memory,
                    next_message_seq=next_message_seq,
                    persisted_memory_revision=context.persisted_memory_revision,
                    memory_pending_clear=context.memory_pending_clear,
                )
                committed = self.context_manager.commit_compaction(
                    transient,
                    plan,
                    summary_result.candidate,
                    summary_max_chars=self.memory_config.summary_max_chars,
                )
            except CancellationError:
                raise
            except MemorySummaryError as exc:
                reason = memory_summary_failure_code(exc)
                memory_warning = (
                    f"会话记忆候选生成失败（{memory_summary_failure_label(reason)}）；"
                    "执行结果不受影响"
                )
                self.observer.on_error(memory_warning)
                if not self._log(
                    {
                        "status": "memory_review_failed",
                        "failure_code": reason,
                        "source_count": len(plan.source_messages),
                        "covered_through": plan.covered_through,
                    }
                ):
                    raise _MemoryAuditFailure(AUDIT_FAILURE_MESSAGE)
                return
            except MemoryValidationError:
                reason = "commit"
                memory_warning = (
                    f"会话记忆候选生成失败（{memory_summary_failure_label(reason)}）；"
                    "执行结果不受影响"
                )
                self.observer.on_error(memory_warning)
                if not self._log(
                    {
                        "status": "memory_review_failed",
                        "failure_code": reason,
                        "source_count": len(plan.source_messages),
                        "covered_through": plan.covered_through,
                    }
                ):
                    raise _MemoryAuditFailure(AUDIT_FAILURE_MESSAGE)
                return
            if not self._log(
                {
                    "status": "memory_review_candidate",
                    "source_count": len(plan.source_messages),
                    "revision": committed.conversation_memory.revision,
                    "covered_through": committed.conversation_memory.covered_through,
                }
            ):
                raise _MemoryAuditFailure(AUDIT_FAILURE_MESSAGE)
            conversation_memory = committed.conversation_memory
            messages[history_start:] = committed.messages
            if summary_result.usage is not None:
                memory_usage = (
                    summary_result.usage
                    if memory_usage is None
                    else memory_usage.merge(summary_result.usage)
                )

        def execution_context() -> SessionContext:
            return SessionContext(
                modified_files=tuple(modified_files), verification=verification,
                unknown_effects=unknown_effects, verification_evidence=evidence,
                verification_failure=failed_snapshot, verification_required=verification_required,
                conversation_memory=conversation_memory,
                next_message_seq=next_message_seq,
                persisted_memory_revision=context.persisted_memory_revision,
                memory_pending_clear=context.memory_pending_clear,
            )

        def publish_state() -> None:
            if observation is not None:
                journal = getattr(tool_context, "change_journal", None)
                observation.publish(execution_context(), effects_observed=file_effects_observed,
                    journal_revision=journal.active_revision if journal is not None else None)

        publish_state()

        def current_task_has_complete_round() -> bool:
            """只在当前任务已有完整工具回合时保留其中间状态。"""

            current_task_index = max(
                index
                for index, message in enumerate(messages)
                if message.kind == "task"
            )
            return any(
                _complete_round_tail(messages, index, self.tool_protocol) is not None
                for index in range(current_task_index, len(messages) - 1)
            )

        def turn_result(
            result: RunResult,
            *,
            rollback_task: bool = False,
        ) -> SessionTurnResult:
            nonlocal evidence, verification, verification_required
            cleanup_bad = (result.cleanup_failed or cleanup_failed or bool(
                current_cleanup() is not None and current_cleanup().failed))
            if cancellation.is_cancelled or cleanup_bad:
                if isinstance(scope, VerificationScope):
                    scope.revoke()
                if evidence is not None or verification_required:
                    evidence = None
                    verification_required = True
                    verification = "失败" if failed_snapshot is not None else "待验证"
            publish_state()
            normalized_history = normalize_history()
            current_complete = current_task_has_complete_round()
            # 兼容关闭新记忆能力时的失败语义：未开始完整工具回合的任务
            # 必须原样返回调用方上下文，不能仅因本地编号产生可见变化。
            if rollback_task and not current_complete and memory_compacted:
                current_index = next(
                    (
                        index
                        for index, message in enumerate(normalized_history)
                        if message.kind == "task" and message.task_id == current_task_id
                    ),
                    len(normalized_history),
                )
                rollback_history = normalized_history[:current_index]
                rollback_next_message_seq = next_message_seq
            else:
                rollback_history = context.messages
                rollback_next_message_seq = context.next_message_seq
            turn = SessionTurnResult(
                replace(result, usage=accumulated_usage, unknown_effects=unknown_effects,
                        verification=verification, cleanup_failed=cleanup_bad),
                SessionContext(
                    messages=(
                        rollback_history
                        if rollback_task and not current_complete
                        else normalized_history
                    ),
                    persisted_summary=context.persisted_summary,
                    modified_files=tuple(modified_files),
                    verification=verification,
                    unknown_effects=unknown_effects,
                    verification_evidence=evidence,
                    verification_failure=failed_snapshot,
                    verification_required=verification_required,
                    conversation_memory=conversation_memory,
                    next_message_seq=(
                        rollback_next_message_seq
                        if rollback_task and not current_complete
                        else next_message_seq
                    ),
                    persisted_memory_revision=context.persisted_memory_revision,
                    memory_pending_clear=context.memory_pending_clear,
                ),
                file_effects_observed=file_effects_observed,
                memory_usage=memory_usage,
                memory_calls=memory_calls,
                memory_warning=memory_warning,
            )
            if turn.result.ok:
                self._emit(event_sink, RuntimeCompleted(turn.result))
            else:
                category = "cancelled" if result.summary == "任务已取消" else "runtime"
                self._emit(event_sink, RuntimeFailed(category, result.summary))
            return turn

        if unknown_effects:
            verification = "待验证"
            evidence = None
            verification_required = True
            return turn_result(RunResult(
                False, "文件影响未确认；请检查实际文件并通过 /clear 明确确认", 0,
                modified_files=tuple(modified_files), verification="待验证",
            ))

        if self.audit is not None:
            try:
                self.audit.prepare()
            except OSError:
                self.observer.on_error(AUDIT_FAILURE_MESSAGE)
                return turn_result(
                    RunResult(False, AUDIT_FAILURE_MESSAGE, 0), rollback_task=True
                )

        provider_tools = (
            self.tools.definitions if self._protocol.tools_enabled else ()
        )
        try:
            await prepare_structured_memory(provider_tools)
        except CancellationError:
            return turn_result(
                RunResult(False, "任务已取消", 0, tool_calls, tuple(modified_files), verification),
                rollback_task=True,
            )
        except _MemoryAuditFailure:
            return turn_result(
                self._audit_failure_result(0, tool_calls, modified_files, verification),
                rollback_task=True,
            )
        except MemorySummaryError:
            return turn_result(
                RunResult(False, "上下文记忆整理失败，未发送模型请求", 0, tool_calls,
                          tuple(modified_files), verification),
                rollback_task=True,
            )

        if self.plan_enabled:
            try:
                plan_usage = await self._planning_round_async(
                    messages,
                    tool_calls,
                    modified_files,
                    verification,
                    cancellation,
                    event_sink,
                    request_messages=request_view(),
                )
            except CancellationError:
                return turn_result(
                    RunResult(
                        False,
                        "任务已取消",
                        0,
                        tool_calls,
                        tuple(modified_files),
                        verification,
                    ),
                    rollback_task=True,
                )
            if plan_usage is _PLAN_ABORT:
                return turn_result(
                    self._audit_failure_result(
                        0, tool_calls, modified_files, verification
                    ),
                    rollback_task=True,
                )
            if plan_usage is not None:
                accumulated_usage = (
                    plan_usage
                    if accumulated_usage is None
                    else accumulated_usage.merge(plan_usage)
                )

        for round_number in range(1, self.max_rounds + 1):
            if cancellation.is_cancelled:
                return turn_result(
                    RunResult(False, "任务已取消", round_number - 1, tool_calls, tuple(modified_files), verification),
                    rollback_task=True,
                )
            self.observer.on_round_start(round_number, self.max_rounds)
            self._emit(event_sink, RoundStarted(round_number, self.max_rounds))
            started = time.perf_counter()
            normalize_history()
            try:
                await prepare_structured_memory(provider_tools)
            except CancellationError:
                return turn_result(
                    RunResult(False, "任务已取消", round_number - 1, tool_calls,
                              tuple(modified_files), verification),
                    rollback_task=True,
                )
            except _MemoryAuditFailure:
                return turn_result(
                    self._audit_failure_result(
                        round_number - 1, tool_calls, modified_files, verification
                    ),
                    rollback_task=True,
                )
            except MemorySummaryError:
                return turn_result(
                    RunResult(False, "上下文记忆整理失败，未发送模型请求", round_number - 1,
                              tool_calls, tuple(modified_files), verification),
                    rollback_task=True,
                )
            assembled_messages = request_view()
            context_snapshot = self.context_manager.prepare(assembled_messages)
            if (
                self.memory_config.compaction == "structured"
                and context_snapshot.history_truncated
            ):
                return turn_result(
                    RunResult(False, "结构化记忆模式拒绝静默裁剪历史", round_number - 1,
                              tool_calls, tuple(modified_files), verification),
                    rollback_task=True,
                )
            request_messages = list(context_snapshot.messages)
            if context_snapshot.history_truncated:
                self._emit(
                    event_sink,
                    ContextCompacted(
                        before_chars=sum(message.character_budget() for message in messages),
                        after_chars=context_snapshot.character_count,
                    ),
                )
            try:
                response = await self._request_provider_async(
                    request_messages,
                    provider_tools,
                    cancellation,
                    event_sink,
                )
            except CancellationError:
                return turn_result(
                    RunResult(False, "任务已取消", round_number, tool_calls, tuple(modified_files), verification),
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
                        "duration_ms": self._elapsed_ms(started),
                    }
                ):
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        ),
                        rollback_task=True,
                    )
                messages.append(
                    Message("user", PROTOCOL_FEEDBACK, kind="protocol_feedback")
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
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        ),
                        rollback_task=True,
                    )
                return turn_result(
                    RunResult(
                        False,
                        PROVIDER_FAILURE_MESSAGE,
                        round_number,
                        tool_calls,
                        tuple(modified_files),
                        verification,
                    ),
                    rollback_task=True,
                )

            if response.usage is not None:
                accumulated_usage = (
                    response.usage
                    if accumulated_usage is None
                    else accumulated_usage.merge(response.usage)
                )
                _notify_provider_usage(self.observer, round_number, response.usage)
                if not self._audit_usage(round_number, response.usage):
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        ),
                        rollback_task=True,
                    )

            resolved = self._protocol.resolve_action(response)
            messages.extend(resolved.assistant_messages)
            normalize_history()
            if response.usage is not None:
                assistant_count = len(resolved.assistant_messages)
                normalized_assistant = (
                    messages[-assistant_count:] if assistant_count else []
                )
                self.context_manager.record_usage(
                    response.usage,
                    [*request_messages, *normalized_assistant],
                )
            if not resolved.actions:
                feedback = resolved.feedback or Message(
                    "user", PROTOCOL_FEEDBACK, kind="protocol_feedback"
                )
                self.observer.on_error(feedback.content or "")
                messages.append(feedback)
                if not self._log(
                    {
                        "round": round_number,
                        "status": "invalid_action",
                        "error_type": resolved.audit_error_type or "ActionProtocolError",
                        "error_chars": len(feedback.content or ""),
                        "duration_ms": self._elapsed_ms(started),
                    }
                ):
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        ),
                        rollback_task=True,
                    )
                continue

            # 没有依赖图，因此本批首次失败就停止后续动作；只有根结果可决定新轮次。
            for action_index, (action, tool_call_id) in enumerate(
                zip(resolved.actions, resolved.tool_call_ids)
            ):
                if cancellation.is_cancelled:
                    self._fill_remaining_results(
                        resolved, action_index, action_index, round_number, messages, event_sink,
                    )
                    return turn_result(
                        RunResult(False, "任务已取消", round_number, tool_calls, tuple(modified_files), verification)
                    )
                if not action.reason:
                    definition = self.tools.describe(action.tool)
                    action = replace(
                        action,
                        reason=(
                            definition.description
                            if definition is not None
                            else "请求执行未注册的工具。"
                        ),
                    )
                self.observer.on_action(action)
                requires_approval = getattr(self.tools, "requires_approval", None)
                if callable(requires_approval) and requires_approval(action.tool):
                    self._emit(event_sink, ApprovalRequested(
                        ToolCall(tool_call_id or f"legacy-{round_number}-{action_index}", action.tool, action.arguments),
                        action.reason,
                    ))
                event_call = ToolCall(
                    tool_call_id or f"legacy-{round_number}-{action_index}",
                    action.tool,
                    action.arguments,
                )
                self._emit(event_sink, ToolExecutionStarted(event_call))
                tool_calls += 1
                action_started = time.perf_counter()
                # 执行可能先落盘再取消；只有 observe 完成后才能确认状态已消费。
                file_effects_observed = False
                if observation is not None:
                    observation.begin_tool()
                interrupted = False
                try:
                    execute_async = getattr(self.tools, "execute_async", None)
                    if callable(execute_async):
                        execute_parameters = inspect.signature(execute_async).parameters
                        execute_kwargs: dict[str, Any] = {"cancellation": cancellation}
                        if "call_id" in execute_parameters or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in execute_parameters.values()
                        ):
                            execute_kwargs["call_id"] = event_call.id
                        result = await execute_async(
                            action.tool,
                            action.arguments,
                            **execute_kwargs,
                        )
                    else:
                        cancellation.raise_if_cancelled()
                        execute_parameters = inspect.signature(self.tools.execute).parameters
                        execute_kwargs = {}
                        if "call_id" in execute_parameters or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in execute_parameters.values()
                        ):
                            execute_kwargs["call_id"] = event_call.id
                        result = await run_in_cleanup_thread(
                            self.tools.execute,
                            action.tool,
                            action.arguments,
                            **execute_kwargs,
                        )
                except (CancellationError, NativeCancellationError) as exc:
                    if observation is not None:
                        # Registry 可能已发布结果而后处理尚未交付；先恢复新事实再合成取消。
                        latest, _ = observation.reconcile(execution_context())
                        modified_files = list(latest.modified_files)
                        verification, unknown_effects = latest.verification, latest.unknown_effects
                        evidence, failed_snapshot = latest.verification_evidence, latest.verification_failure
                        verification_required = latest.verification_required
                    cleanup_failed = cleanup_failed or exc.cleanup_failed
                    result = tool_failure(
                        ErrorCode.CLEANUP_FAILED if exc.cleanup_failed else ErrorCode.CANCELLED,
                        "任务已取消，资源清理未确认" if exc.cleanup_failed else "任务已取消，该动作未完成",
                        file_effects=FileEffects(EffectState.UNKNOWN) if exc.cleanup_failed else None,
                    )
                    # 网关未交付副作用证据，仍保留 T1 的未消费标记供宿主收尾。
                    interrupted = True
                # 只消费工具在文件安全边界内确认的规范路径，不回读模型原始参数。
                changed_paths = tuple(
                    dict.fromkeys(
                        ([result.relative_path] if result.relative_path is not None else [])
                        + list(result.modified_paths)
                    )
                ) if result.ok else ()
                effects = result.file_effects
                cleanup_failed = cleanup_failed or (
                    result.error is not None and result.error.code is ErrorCode.CLEANUP_FAILED
                )
                if effects is None:
                    effects = (FileEffects(EffectState.CONFIRMED, changed_paths)
                               if changed_paths else FileEffects(EffectState.NONE))
                if ((isinstance(scope, VerificationScope) and scope.unknown_effects)
                        or (observation is not None and observation.unknown_effects)):
                    effects = FileEffects(EffectState.UNKNOWN, effects.paths)
                file_effects_observed = not interrupted
                candidate = result.verification_evidence
                if not (action.tool == "run_command" and isinstance(scope, VerificationScope)
                        and scope.owns(candidate)):
                    candidate = None
                observed = apply_tool_transition(execution_context(), effects, candidate)
                modified_files = list(observed.modified_files)
                verification, unknown_effects = observed.verification, observed.unknown_effects
                evidence, failed_snapshot = observed.verification_evidence, observed.verification_failure
                verification_required = observed.verification_required
                # 先提交本地事实，再进入协议构造/通知；不改变 T3 的配对与首异常顺序。
                publish_state()
                duration_ms = self._elapsed_ms(action_started)
                # 协议记录不能依赖可抛异常的观察者、事件或审计。构造本身失败时
                # 直接传播，不通知完成、不重试构造，也不伪造一个完整回合。
                messages.append(
                    self._protocol.tool_result_message(action, result, tool_call_id)
                )
                try:
                    self.observer.on_tool_result(action, result, duration_ms)
                    self._emit(event_sink, ToolExecutionCompleted(event_call.id, result))
                    tool_event: dict[str, Any] = {
                        "round": round_number,
                        "status": "ok" if result.ok else "tool_error",
                        "tool": (
                            action.tool
                            if self.tools.contains(action.tool)
                            else "unknown"
                        ),
                        "reason_chars": len(action.reason),
                        "arguments": self._audit_arguments(action, result),
                        "output_chars": len(result.output),
                        "duration_ms": duration_ms,
                    }
                    if result.error is not None:
                        tool_event["error"] = result.error.public_fields()
                    origin_resolver = getattr(self.tools, "origin", None)
                    if callable(origin_resolver) and self.tools.contains(action.tool):
                        try:
                            origin = origin_resolver(action.tool)
                        except (TypeError, ValueError):
                            origin = None
                        if isinstance(origin, ToolOrigin):
                            tool_event["origin"] = {
                                "kind": origin.kind,
                                "id": origin.id,
                                "risk": origin.risk,
                            }
                    if result.spill_reference is not None:
                        tool_event["spill"] = {
                            "reference": result.spill_reference,
                            "bytes": result.spill_bytes,
                            "sha256": result.spill_sha256,
                        }
                    audit_ok = self._log(tool_event)
                except BaseException:
                    # 仅隔离结果取得后的通知边界，绝不捕获/吞掉工具核心异常。
                    # 静默补齐后重抛首个异常；二次构造失败也不能覆盖原异常。
                    try:
                        self._fill_remaining_results(
                            resolved, action_index + 1, action_index, round_number,
                            messages, event_sink, notify=False,
                        )
                    except BaseException:
                        pass
                    raise
                cancelled = (interrupted or cancellation.is_cancelled
                             or (result.error is not None and result.error.code is ErrorCode.CANCELLED))
                stop_task = should_stop_task(result.error, effects)
                finished = action.tool == "finish" and result.ok
                if not result.ok or stop_task or finished or cancelled or not audit_ok:
                    # 当前结果先落入历史；剩余只配对，不审批、不执行、不增加调用次数。
                    # 即使审计失败也补齐协议，再结束任务，不留下半个 assistant 回合。
                    remaining_audit_ok = self._fill_remaining_results(
                        resolved, action_index + 1, action_index, round_number, messages, event_sink,
                    )
                    audit_ok = audit_ok and remaining_audit_ok
                if not audit_ok:
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        )
                    )
                if unknown_effects:
                    return turn_result(RunResult(
                        False, "文件影响未确认；请检查实际文件并通过 /clear 明确确认",
                        round_number, tool_calls, tuple(modified_files), verification,
                    ))
                if cancelled or stop_task:
                    return turn_result(RunResult(
                        False, "任务已取消" if cancelled else result.output,
                        round_number, tool_calls, tuple(modified_files), verification,
                    ))
                if finished:
                    valid = False
                    if verification_required and isinstance(scope, VerificationScope):
                        current = await run_in_cleanup_thread(scope.capture, policy)
                        valid = scope.owns(evidence) and evidence.is_valid_for(current)
                        if failed_snapshot is not None and proves_new_file_version(failed_snapshot, current):
                            failed_snapshot = None
                        if not valid:
                            evidence = None
                            verification = "失败" if failed_snapshot is not None else "待验证"
                    completed = (result.ok and not cancellation.is_cancelled and not cleanup_failed
                                 and (not verification_required or (valid and failed_snapshot is None)))
                    summary = result.output
                    if result.ok and verification_required and verification == "待验证":
                        summary = f"{summary}；文件修改后尚未运行验证命令，或受覆盖文件状态证据已失效"
                    elif result.ok and verification_required and verification == "失败":
                        summary = f"{summary}；文件修改后的验证失败"
                    if completed and self.memory_config.persistence == "reviewed_summary":
                        try:
                            await prepare_review_memory()
                        except CancellationError:
                            return turn_result(RunResult(
                                False, "任务已取消", round_number, tool_calls,
                                tuple(modified_files), verification,
                            ))
                        except _MemoryAuditFailure:
                            return turn_result(
                                self._audit_failure_result(
                                    round_number, tool_calls, modified_files, verification
                                )
                            )
                        if memory_warning:
                            summary = f"{summary}；{memory_warning}"
                    return turn_result(
                        RunResult(
                            completed,
                            summary,
                            round_number,
                            tool_calls,
                            tuple(modified_files),
                            verification,
                        )
                    )
                if not result.ok:
                    # SKIPPED 的 REPLAN 只是“未执行”的元数据，绝不是新的决策源。
                    # 兼容无结构化错误的旧网关也只能停止，不能猜它是否可恢复。
                    if result.error is None or result.error.recovery is not RecoveryAction.REPLAN:
                        return turn_result(RunResult(
                            False, result.output, round_number, tool_calls,
                            tuple(modified_files), verification,
                        ))
                    break
        summary = f"达到最大轮数 {self.max_rounds}，任务已安全停止"
        self.observer.on_error(summary)
        return turn_result(
            RunResult(
                False,
                summary,
                self.max_rounds,
                tool_calls,
                tuple(modified_files),
                verification,
            )
        )

    @staticmethod
    def _skipped_result(blocked_by: str, known_call_ids: tuple[str, ...]) -> ToolResult:
        """只允许本轮已知 ID；输出用序号引用来源，绝不插入模型自由文本。"""
        if (any(not isinstance(call_id, str) or not call_id for call_id in known_call_ids)
                or len(set(known_call_ids)) != len(known_call_ids)
                or blocked_by not in known_call_ids):
            raise ValueError("跳过来源必须是本轮唯一的已知调用")
        return tool_failure(
            ErrorCode.SKIPPED,
            json.dumps({"status": "skipped", "blocked_by_call_index": known_call_ids.index(blocked_by)}),
            recovery=RecoveryAction.REPLAN,
        )

    def _fill_remaining_results(
        self, resolved: ResolvedAction, start: int, blocked_index: int,
        round_number: int, messages: list[Message], event_sink: EventSink | None,
        *, notify: bool = True,
    ) -> bool:
        """所有批次出口共用配对入口；完成事件明确 skipped，不通知执行观察者。"""
        known_ids = tuple(call_id or f"legacy-{round_number}-{index}"
                          for index, call_id in enumerate(resolved.tool_call_ids))
        skipped = self._skipped_result(known_ids[blocked_index], known_ids)
        # 整段协议先构造并记录，随后才通知；首个 skipped sink 失败也不能
        # 截断后续配对。构造失败不重试、不通知，不声称完整回合已落入历史。
        remaining_messages = [
            self._protocol.tool_result_message(
                resolved.actions[index], skipped, resolved.tool_call_ids[index],
            )
            for index in range(start, len(resolved.actions))
        ]
        messages.extend(remaining_messages)
        if not notify:
            return True
        audit_ok = True
        for index in range(start, len(resolved.actions)):
            self._emit(event_sink, ToolExecutionCompleted(known_ids[index], skipped))
            logged = self._log({
                "round": round_number, "status": "skipped", "call_index": index,
                "blocked_by_call_index": blocked_index, "error": skipped.error.public_fields(),
            })
            audit_ok = audit_ok and logged
        return audit_ok

    async def _planning_round_async(
        self,
        messages: list[Message],
        tool_calls: int,
        modified_files: list[str],
        verification: str,
        cancellation: CancellationToken,
        event_sink: EventSink | None,
        request_messages: list[Message] | None = None,
    ) -> object:
        """任务执行前的规划阶段（round 0）：生成并注入分步计划。

        返回该次请求的 TokenUsage 供外层累计；审计失败返回 _PLAN_ABORT；
        计划失败时降级为无计划执行并返回 None。
        """

        started = time.perf_counter()
        try:
            response = await self._request_provider_async(
                [*(request_messages if request_messages is not None else messages),
                 Message("user", PLANNING_PROMPT)],
                (),
                cancellation,
                event_sink,
            )
        except CancellationError:
            raise
        except ProviderError as exc:
            self.observer.on_error("规划失败，将直接执行")
            if not self._log(
                {
                    "round": 0,
                    "status": "plan_failed",
                    "error_type": type(exc).__name__,
                    "error_chars": len(str(exc)),
                    "duration_ms": self._elapsed_ms(started),
                }
            ):
                return _PLAN_ABORT
            return None

        if response.usage is not None:
            _notify_provider_usage(self.observer, 0, response.usage)
            if not self._audit_usage(0, response.usage):
                return _PLAN_ABORT

        raw = response.content or ""
        plan = self._parse_plan(raw)
        if plan is not None:
            messages.append(Message("system", f"执行计划：\n{plan}"))
            self._emit(event_sink, PlanningCompleted(plan))
            if not self._log(
                {
                    "round": 0,
                    "status": "plan",
                    "plan_chars": len(plan),
                    "plan_steps": plan.count("\n") + 1,
                    "duration_ms": self._elapsed_ms(started),
                }
            ):
                return _PLAN_ABORT
        else:
            if not self._log(
                {
                    "round": 0,
                    "status": "plan_failed",
                    "error_type": "PlanParseError",
                    "plan_chars": len(raw),
                    "duration_ms": self._elapsed_ms(started),
                }
            ):
                return _PLAN_ABORT
        return response.usage

    async def _request_provider_async(
        self,
        messages: list[Message],
        tools: tuple[ToolDefinition, ...] | list[ToolDefinition],
        cancellation: CancellationToken,
        event_sink: EventSink | None,
    ) -> ProviderResponse:
        """消费流式 Provider；旧 fake/实现通过 complete 保持兼容。"""

        cancellation.raise_if_cancelled()
        stream = getattr(self.provider, "stream", None)
        if not callable(stream):
            response = await run_in_cleanup_thread(self.provider.complete, messages, tools)
            for event in self._response_events(response):
                self._emit(event_sink, event)
            return response

        content_parts: list[str] = []
        calls: list[ToolCall] = []
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        completed = False
        async for event in stream(messages, tools, cancellation=cancellation):
            cancellation.raise_if_cancelled()
            self._emit(event_sink, event)
            if isinstance(event, TextDelta):
                content_parts.append(event.text)
            elif isinstance(event, ToolCallCompleted):
                calls.append(event.call)
            elif isinstance(event, UsageReported):
                usage = event.usage if usage is None else usage.merge(event.usage)
            elif isinstance(event, ProviderCompleted):
                finish_reason = event.finish_reason
                completed = True
        if not completed:
            raise ProviderProtocolError("模型服务流缺少完成事件")
        content = "".join(content_parts) or None
        if content is None and not calls:
            raise ProviderProtocolError("模型服务流没有可用响应")
        return ProviderResponse(content, tuple(calls), finish_reason, usage)

    @staticmethod
    def _response_events(response: ProviderResponse) -> tuple[AgentEvent, ...]:
        """把仅支持 complete 的旧实现转换成等价类型化事件。"""

        events: list[AgentEvent] = []
        if response.content:
            events.append(TextDelta(response.content))
        events.extend(ToolCallCompleted(call) for call in response.tool_calls)
        if response.usage is not None:
            events.append(UsageReported(response.usage))
        events.append(ProviderCompleted(response.finish_reason))
        return tuple(events)

    @staticmethod
    def _emit(event_sink: EventSink | None, event: AgentEvent) -> None:
        if event_sink is not None:
            event_sink(event)

    @staticmethod
    def _parse_plan(raw: str) -> str | None:
        """宽容解析模型计划：JSON steps > Markdown 列表 > 原文降级。"""
        text = raw.strip()
        if not text:
            return None
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                steps = decoded.get("steps")
                if isinstance(steps, list):
                    clean = [
                        str(step).strip() for step in steps if str(step).strip()
                    ]
                    if clean:
                        return "\n".join(
                            f"{index + 1}. {step}"
                            for index, step in enumerate(clean)
                        )
        except (json.JSONDecodeError, TypeError):
            pass
        lines = [
            line.strip().lstrip("-*").strip().lstrip("0123456789. ").strip()
            for line in text.splitlines()
            if line.strip()
        ]
        if len(lines) >= 2:
            return "\n".join(f"{index + 1}. {line}" for index, line in enumerate(lines))
        return text

    def _log(self, event: dict[str, Any]) -> bool:
        if self.audit is None:
            return True
        try:
            self.audit.log(event)
        except OSError as audit_error:
            try:
                self.observer.on_error(AUDIT_FAILURE_MESSAGE)
            except BaseException:
                # 错误提示只是二次通知，不能替换首个审计失败及其对象身份。
                raise audit_error
            return False
        return True

    def _audit_usage(self, round_number: int, usage: TokenUsage) -> bool:
        event: dict[str, Any] = {"round": round_number, "status": "provider_usage"}
        for field, value in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cached", usage.cached_tokens),
            ("cache_miss", usage.cache_miss_tokens),
        ):
            if value is not None:
                event[field] = value
        return self._log(event)

    @staticmethod
    def _audit_failure_result(
        round_number: int,
        tool_calls: int,
        modified_files: list[str],
        verification: str,
    ) -> RunResult:
        return RunResult(
            False,
            AUDIT_FAILURE_MESSAGE,
            round_number,
            tool_calls,
            tuple(modified_files),
            verification,
        )

    def _audit_arguments(
        self,
        action: ToolAction,
        result: ToolResult,
    ) -> dict[str, Any]:
        """只保留审计所需元数据，避免重复保存源码和任务摘要。"""

        arguments = action.arguments
        if not result.ok and action.tool not in {"run_command", "apply_patch"}:
            # 失败不能证明原始参数已通过策略；审计仅保留数量，不回显拒绝路径。
            return {"argument_count": len(arguments)}
        if action.tool in {"list_files", "read_file"}:
            return {"path": arguments.get("path", ".")}
        if action.tool == "read_tool_result":
            return {
                "reference": arguments.get("reference"),
                "offset": arguments.get("offset", 0),
            }
        if action.tool == "search_text":
            query = arguments.get("query", "")
            return {
                "path": arguments.get("path", "."),
                "query_chars": len(query) if isinstance(query, str) else 0,
            }
        if action.tool == "edit_file":
            old_text = arguments.get("old_text", "")
            new_text = arguments.get("new_text", "")
            return {
                "path": arguments.get("path"),
                "old_text_chars": len(old_text) if isinstance(old_text, str) else 0,
                "new_text_chars": len(new_text) if isinstance(new_text, str) else 0,
            }
        if action.tool == "create_file":
            content = arguments.get("content", "")
            return {
                "path": arguments.get("path"),
                "content_chars": len(content) if isinstance(content, str) else 0,
            }
        if action.tool == "apply_patch":
            patch_text = arguments.get("patch", "")
            return {
                "patch_chars": len(patch_text) if isinstance(patch_text, str) else 0,
                "paths": result.audit_paths,
                "file_count": len(result.audit_paths),
                "change_chars": result.change_chars,
            }
        if action.tool == "run_command":
            command = arguments.get("command", "")
            metadata = self.tools.context.command_policy.audit_metadata(
                command if isinstance(command, str) else "",
            )
            cwd = arguments.get("cwd", ".")
            if isinstance(cwd, str):
                try:
                    resolved_cwd = self.tools.context.workspace_policy.resolve_path(cwd)
                    relative_cwd = resolved_cwd.relative_to(
                        self.tools.context.workspace_policy.workspace
                    )
                    metadata["cwd"] = {
                        "is_workspace": not relative_cwd.parts,
                        "depth": len(relative_cwd.parts),
                    }
                except (PolicyError, ValueError):
                    metadata["cwd"] = {
                        "valid": False,
                        "chars": len(cwd),
                    }
            else:
                metadata["cwd"] = {"valid": False, "chars": 0}
            return metadata
        if action.tool == "finish":
            summary = arguments.get("summary", "")
            return {"summary_chars": len(summary) if isinstance(summary, str) else 0}
        return {"argument_count": len(arguments)}

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((time.perf_counter() - started) * 1000)
